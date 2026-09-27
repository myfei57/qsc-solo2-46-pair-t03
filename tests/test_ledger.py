"""Tests for per-job verifiable ledger export and entry-by-entry audit."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from cranefleet import ledger as ledger_module
from cranefleet.audit import (
    GENESIS_HASH,
    Audit,
    AuditEntry,
    STATUS_HASH_MISMATCH,
    STATUS_OK,
)
from cranefleet.console import Console, Request
from cranefleet.wiring import build_app

WEB_ROOT = Path(__file__).resolve().parents[1] / "web"


def dispatch(console: Console, method: str, path: str, body: dict | None = None):
    request = Request(method=method, path=path, query={}, body=body or {}, body_valid=True)
    return console.dispatch(request)


class HashChainTests(unittest.TestCase):
    def test_entries_are_sequential_and_chained(self) -> None:
        audit = Audit(100)
        first = audit.record("crane-1", "slew-start", "go")
        second = audit.record("crane-1", "slew-stop", "halt")
        self.assertEqual((first.seq, second.seq), (1, 2))
        self.assertEqual(first.prev_hash, GENESIS_HASH)
        self.assertEqual(second.prev_hash, first.hash)
        self.assertEqual(first.hash, first.recomputed_hash())

    def test_job_scope_tags_entries(self) -> None:
        audit = Audit(100)
        audit.begin_job_scope("job-9")
        inside = audit.record("crane-1", "slew-start", "go")
        audit.end_job_scope()
        outside = audit.record("crane-1", "slew-stop", "halt")
        self.assertEqual(inside.job_id, "job-9")
        self.assertEqual(outside.job_id, "")

    def test_legacy_rows_are_rechained_on_restore(self) -> None:
        audit = Audit(100)
        audit.restore(
            [
                AuditEntry(entry_id="1", recorded_at="t", crane_id="crane-1", action="old-a", detail="x"),
                AuditEntry(entry_id="2", recorded_at="t", crane_id="crane-1", action="old-b", detail="y"),
            ]
        )
        entries = audit.snapshot()
        self.assertEqual([entry.seq for entry in entries], [1, 2])
        self.assertEqual(entries[0].hash, entries[0].recomputed_hash())
        self.assertEqual(entries[1].prev_hash, entries[0].hash)


class JobLedgerFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = build_app("memory", web_root=str(WEB_ROOT))
        self.console = self.app.console

    def tearDown(self) -> None:
        self.app.shutdown()

    def _run_job(self) -> tuple[str, dict]:
        status, payload = dispatch(
            self.console, "POST", "/api/jobs/start", {"title": "主梁吊装"}
        )
        self.assertEqual(status, 200, payload)
        job_id = payload["data"]["job_id"]
        # Real on-site activity during the job.
        dispatch(self.console, "POST", "/api/zone/occupy", {"crane_id": "crane-1"})
        dispatch(self.console, "POST", "/api/slew/start")
        dispatch(self.console, "POST", "/api/wind/alarm", {"level": 14})
        dispatch(self.console, "POST", "/api/cluster/survey", {"cranes": {"crane-2": {"x": 0, "y": 150}}})
        dispatch(self.console, "POST", "/api/limit/check")
        status, payload = dispatch(self.console, "POST", "/api/jobs/end")
        self.assertEqual(status, 200, payload)
        status, payload = dispatch(
            self.console, "POST", "/api/ledger/export", {"job_id": job_id}
        )
        self.assertEqual(status, 200, payload)
        return job_id, payload["data"]["materials"]

    def test_clean_export_verifies_consistent(self) -> None:
        _, materials = self._run_job()
        status, payload = dispatch(
            self.console, "POST", "/api/ledger/verify", {"materials": materials}
        )
        self.assertEqual(status, 200)
        report = payload["data"]
        self.assertEqual(report["conclusion"], ledger_module.CONCLUSION_CONSISTENT, report)
        # Every entry carries an explicit ok conclusion.
        self.assertTrue(report["entry_results"])
        self.assertTrue(all(row["status"] == STATUS_OK for row in report["entry_results"]))
        # The independent counters line up with the matching entry counts.
        metrics = {check["metric"]: check for check in report["checks"] if check.get("metric")}
        self.assertEqual(metrics["wind_alarm_seq"]["counter_delta"], 1)
        self.assertEqual(metrics["wind_alarm_seq"]["logged_entries"], 1)
        self.assertEqual(metrics["zone_registration_seq"]["counter_delta"], 1)
        self.assertEqual(metrics["limit_sequence"]["counter_delta"], 1)
        self.assertIn("核验报告", report["text"])
        self.assertIn("核验通过", report["text"])

    def test_materials_are_persisted_under_the_job(self) -> None:
        job_id, materials = self._run_job()
        raw = self.console.store.load(ledger_module.export_key(job_id))
        self.assertEqual(json.loads(raw)["bundle_hash"], materials["bundle_hash"])

    def test_missing_entry_is_caught_as_a_gap(self) -> None:
        _, materials = self._run_job()
        # Delete an interior entry (keep the boundaries).
        del materials["entries"][2]
        materials["bundle_hash"] = ledger_module.bundle_hash(materials)
        status, payload = dispatch(
            self.console, "POST", "/api/ledger/verify", {"materials": materials}
        )
        report = payload["data"]
        self.assertEqual(status, 409)
        self.assertEqual(report["conclusion"], ledger_module.CONCLUSION_INCONSISTENT)
        messages = " ".join(finding["message"] for finding in report["findings"])
        self.assertIn("缺漏", messages)

    def test_altered_entry_field_is_caught(self) -> None:
        _, materials = self._run_job()
        materials["entries"][3]["detail"] = "forged detail"
        # Recompute only the bundle hash so the bundle looks intact; the row
        # hash chain must still expose the edit.
        materials["bundle_hash"] = ledger_module.bundle_hash(materials)
        status, payload = dispatch(
            self.console, "POST", "/api/ledger/verify", {"materials": materials}
        )
        report = payload["data"]
        self.assertEqual(status, 409)
        self.assertEqual(report["conclusion"], ledger_module.CONCLUSION_INCONSISTENT)
        statuses = {finding["status"] for finding in report["findings"]}
        self.assertIn(STATUS_HASH_MISMATCH, statuses)

    def test_bundle_hash_tampering_is_caught(self) -> None:
        _, materials = self._run_job()
        materials["job"]["title"] = "改写过的作业名"
        status, payload = dispatch(
            self.console, "POST", "/api/ledger/verify", {"materials": materials}
        )
        self.assertEqual(status, 409)
        report = payload["data"]
        self.assertEqual(report["conclusion"], ledger_module.CONCLUSION_INCONSISTENT)
        self.assertTrue(
            any("整包哈希" in finding["message"] for finding in report["findings"])
        )

    def test_site_counter_mismatch_is_reported_not_hidden(self) -> None:
        _, materials = self._run_job()
        # Inflate the end fingerprint so the machinery claims one more wind
        # alarm than the ledger contains entries for.
        materials["job"]["end_fingerprint"]["wind_alarm_seq"] += 1
        materials["bundle_hash"] = ledger_module.bundle_hash(materials)
        status, payload = dispatch(
            self.console, "POST", "/api/ledger/verify", {"materials": materials}
        )
        self.assertEqual(status, 409)
        report = payload["data"]
        site_findings = [
            finding for finding in report["findings"] if finding["scope"] == "site:wind_alarm_seq"
        ]
        self.assertEqual(len(site_findings), 1)
        self.assertIn("台账与现场对不上", site_findings[0]["message"])

    def test_wrong_job_tag_is_caught(self) -> None:
        _, materials = self._run_job()
        materials["entries"][2]["job_id"] = "some-other-job"
        materials["bundle_hash"] = ledger_module.bundle_hash(materials)
        status, payload = dispatch(
            self.console, "POST", "/api/ledger/verify", {"materials": materials}
        )
        self.assertEqual(status, 409)
        scopes = {finding["scope"] for finding in payload["data"]["findings"]}
        self.assertTrue(any(scope.startswith("seq:") for scope in scopes))

    def test_open_job_can_be_exported_but_replay_after_eviction_is_inconclusive(self) -> None:
        status, _ = dispatch(self.console, "POST", "/api/jobs/start", {"title": "未收尾作业"})
        self.assertEqual(status, 200)
        dispatch(self.console, "POST", "/api/slew/start")
        status, payload = dispatch(self.console, "POST", "/api/ledger/export", {"job_id": ""})
        self.assertEqual(status, 200)
        materials = payload["data"]["materials"]
        job_end_seq = materials["log_tail"]["seq"]
        # Simulate the ring buffer rolling far past the whole job: a fresh
        # small buffer whose surviving anchor is newer than the entire job.
        small = Audit(2)
        small.reset_sequence(job_end_seq + 10)
        first = small.record("system", "boot", "much later")
        small.record("system", "tick", "latest")
        self.assertGreater(first.seq, job_end_seq)
        report = ledger_module.verify_materials(copy.deepcopy(materials), small)
        self.assertEqual(report["conclusion"], ledger_module.CONCLUSION_INCONCLUSIVE)
        self.assertTrue(
            any("覆盖" in finding["message"] for finding in report["findings"])
        )

    def test_cannot_start_two_jobs_and_end_requires_one(self) -> None:
        dispatch(self.console, "POST", "/api/jobs/start", {"title": "甲"})
        status, payload = dispatch(self.console, "POST", "/api/jobs/start", {"title": "乙"})
        self.assertEqual(status, 409)
        dispatch(self.console, "POST", "/api/jobs/end")
        status, payload = dispatch(self.console, "POST", "/api/jobs/end")
        self.assertEqual(status, 404)

    def test_job_state_survives_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = build_app(directory, web_root=str(WEB_ROOT))
            status, payload = dispatch(
                first.console, "POST", "/api/jobs/start", {"title": "跨班次作业"}
            )
            job_id = payload["data"]["job_id"]
            dispatch(first.console, "POST", "/api/slew/start")
            first.shutdown()

            second = build_app(directory, web_root=str(WEB_ROOT))
            status, payload = dispatch(second.console, "GET", "/api/jobs")
            self.assertEqual(payload["data"]["active"]["job_id"], job_id)
            # The job was still open, so the restart boot entry is tagged with
            # it and subsequent actions stay inside the same job scope.
            boot = second.console.audit.latest("boot")
            self.assertIsNotNone(boot)
            self.assertEqual(boot.job_id, job_id)
            swing = second.console.audit.record("crane-1", "slew-swing", "angle=1.0")
            self.assertEqual(swing.job_id, job_id)
            second.shutdown()


if __name__ == "__main__":
    unittest.main()
