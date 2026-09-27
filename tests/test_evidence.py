"""Tests for tamper-evident audit chains and per-job evidence export."""

from __future__ import annotations

import unittest
from dataclasses import replace

from cranefleet import evidence
from cranefleet.audit import (
    Audit,
    AuditEntry,
    GENESIS_DIGEST,
    compute_digest,
    verify_entry,
)
from cranefleet.console import Console, Request
from cranefleet.wiring import build_app

WEB_ROOT = "web"


def dispatch(console: Console, method: str, path: str, body: dict | None = None, query: dict | None = None):
    normalized = {key: values if isinstance(values, list) else [values] for key, values in (query or {}).items()}
    request = Request(
        method=method,
        path=path.split("?", 1)[0],
        query=normalized,
        body=body or {},
        body_valid=True,
    )
    return console.dispatch(request)


class HashChainTests(unittest.TestCase):
    def test_entries_are_sequential_and_chained(self) -> None:
        audit = Audit(100)
        first = audit.record("crane-1", "slew-start", "a")
        second = audit.record("crane-1", "slew-stop", "b")
        self.assertEqual((first.seq, second.seq), (1, 2))
        self.assertEqual(first.prev_digest, GENESIS_DIGEST)
        self.assertEqual(second.prev_digest, first.digest)
        self.assertEqual(verify_entry(first, GENESIS_DIGEST), "ok")
        self.assertEqual(verify_entry(second, first.digest), "ok")

    def test_digest_covers_every_field(self) -> None:
        audit = Audit(10)
        entry = audit.record("crane-1", "slew-start", "ok")
        tampered = replace(entry, detail="changed")
        self.assertEqual(verify_entry(tampered, GENESIS_DIGEST), "bad-digest")
        recomputed = compute_digest(
            entry.seq,
            entry.entry_id,
            entry.recorded_at,
            entry.crane_id,
            entry.action,
            entry.detail,
            GENESIS_DIGEST,
        )
        self.assertEqual(recomputed, entry.digest)

    def test_verify_chain_detects_gap_and_broken_link(self) -> None:
        audit = Audit(100)
        entries = [audit.record("crane-1", "tick", str(i)) for i in range(5)]
        # Delete the third row outright: surviving rows show a sequence gap.
        pruned = entries[:2] + entries[3:]
        audit.restore(pruned)
        kinds = {item["kind"] for item in audit.verify_chain()}
        self.assertIn("seq-gap", kinds)
        # Rewrite a digest pointer to simulate a broken link.
        broken = [replace(entries[3], prev_digest="f" * 64)]
        audit.restore(entries[:3] + broken + entries[4:])
        self.assertTrue(
            any(item["kind"] == "chain-broken" for item in audit.verify_chain())
        )

    def test_legacy_entries_restore_without_a_chain(self) -> None:
        legacy = AuditEntry(
            entry_id="old",
            recorded_at="2026-01-01T00:00:00+00:00",
            crane_id="crane-1",
            action="boot",
            detail="legacy",
        )
        audit = Audit(10)
        audit.restore([legacy])
        fresh = audit.record("crane-1", "tick", "new")
        self.assertEqual(fresh.seq, 1)
        self.assertEqual(fresh.prev_digest, GENESIS_DIGEST)

    def test_restore_continues_sequence_and_chain(self) -> None:
        audit = Audit(10)
        audit.record("crane-1", "a", "1")
        audit.record("crane-1", "b", "2")
        clone = Audit(10)
        clone.restore(audit.snapshot())
        continued = clone.record("crane-1", "c", "3")
        self.assertEqual(continued.seq, 3)
        self.assertEqual(continued.prev_digest, audit.snapshot()[-1].digest)


class EvidenceFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = build_app("memory", web_root=WEB_ROOT)
        self.console = self.app.console

    def tearDown(self) -> None:
        self.app.shutdown()

    def _run_a_job(self) -> str:
        dispatch(self.console, "POST", "/api/evidence/job/start", {"job_id": "J-1", "label": "一号吊装"})
        dispatch(self.console, "POST", "/api/zone/occupy", {"crane_id": "crane-1"})
        dispatch(self.console, "POST", "/api/slew/start")
        dispatch(self.console, "POST", "/api/wind/alarm", {"level": 14})
        dispatch(self.console, "POST", "/api/wind/clear")
        dispatch(self.console, "POST", "/api/crane/command", {"source": "cab", "action": "lower"})
        dispatch(self.console, "POST", "/api/zone/clear")
        dispatch(self.console, "POST", "/api/evidence/job/end", {"job_id": "J-1"})
        return "J-1"

    def test_clean_job_export_passes_and_reconciles_every_counter(self) -> None:
        self._run_a_job()
        status, payload = dispatch(self.console, "GET", "/api/evidence/export", query={"job": "J-1"})
        self.assertEqual(status, 200)
        bundle = payload["data"]
        self.assertTrue(bundle["passed"], bundle["report"])
        self.assertTrue(bundle["chain_intact"])
        self.assertTrue(bundle["reconciliation_binding"])
        self.assertEqual(bundle["anomalies"], [])
        names = {item["name"]: item for item in bundle["checks"]}
        self.assertEqual(names["分区占用登记"]["status"], "match")
        self.assertEqual(names["大风报警"]["status"], "match")
        self.assertEqual(names["大风回转停止"]["status"], "match")
        self.assertEqual(names["司机/遥控指令"]["status"], "match")
        self.assertIn("塔吊机群作业留痕核对单", bundle["report"])
        # Entries are exported without the job markers themselves.
        self.assertTrue(bundle["log_window"]["entry_count"] >= 6)
        self.assertTrue(bundle["persisted_key"])

    def test_missing_ledger_row_is_flagged_not_hidden(self) -> None:
        self._run_a_job()
        # Tamper with the live ledger: drop one wind-related row before export.
        entries = self.console.audit.snapshot()
        kept = [entry for entry in entries if entry.action != "wind-slew-stop"]
        self.console.audit.restore(kept)
        status, payload = dispatch(self.console, "GET", "/api/evidence/export", query={"job": "J-1"})
        bundle = payload["data"]
        self.assertFalse(bundle["passed"])
        self.assertTrue(
            any(
                item["kind"] in ("seq-gap", "chain-broken")
                for item in bundle["anomalies"]
            ),
            bundle["anomalies"],
        )
        wind_stop = next(c for c in bundle["checks"] if c["name"] == "大风回转停止")
        self.assertEqual(wind_stop["status"], "mismatch")
        self.assertEqual((wind_stop["machine"], wind_stop["ledger"]), (1, 0))

    def test_altered_entry_fails_its_digest(self) -> None:
        self._run_a_job()
        entries = self.console.audit.snapshot()
        target = next(entry for entry in entries if entry.action == "wind-alarm")
        tampered = replace(target, detail="level=0.0 seq=1")
        rewritten = [tampered if entry.seq == target.seq else entry for entry in entries]
        self.console.audit.restore(rewritten)
        _, payload = dispatch(self.console, "GET", "/api/evidence/export", query={"job": "J-1"})
        bundle = payload["data"]
        self.assertFalse(bundle["passed"])
        self.assertTrue(
            any(item["kind"] == "bad-digest" for item in bundle["anomalies"])
        )

    def test_open_job_is_marked_unbounded(self) -> None:
        dispatch(self.console, "POST", "/api/evidence/job/start", {"job_id": "J-2"})
        dispatch(self.console, "POST", "/api/zone/occupy", {"crane_id": "crane-1"})
        _, payload = dispatch(self.console, "GET", "/api/evidence/export", query={"job": "J-2"})
        bundle = payload["data"]
        self.assertFalse(bundle["reconciliation_binding"])
        self.assertTrue(any(item["kind"] == "job-open" for item in bundle["anomalies"]))

    def test_start_requires_id_and_rejects_double_open(self) -> None:
        status, _ = dispatch(self.console, "POST", "/api/evidence/job/start", {})
        self.assertEqual(status, 400)
        dispatch(self.console, "POST", "/api/evidence/job/start", {"job_id": "J-3"})
        status, payload = dispatch(
            self.console, "POST", "/api/evidence/job/start", {"job_id": "J-4"}
        )
        self.assertEqual(status, 409)
        self.assertIn("open", payload["error"])
        status, _ = dispatch(self.console, "POST", "/api/evidence/job/end", {"job_id": "J-3"})
        self.assertEqual(status, 200)
        status, _ = dispatch(self.console, "POST", "/api/evidence/job/end", {"job_id": "J-3"})
        self.assertEqual(status, 409)

    def test_export_unknown_job_and_window_validation(self) -> None:
        status, _ = dispatch(self.console, "GET", "/api/evidence/export", query={"job": "nope"})
        self.assertEqual(status, 404)
        status, _ = dispatch(self.console, "GET", "/api/evidence/export")
        self.assertEqual(status, 400)

    def test_time_window_export(self) -> None:
        dispatch(self.console, "POST", "/api/zone/occupy", {"crane_id": "crane-1"})
        _, payload = dispatch(
            self.console,
            "GET",
            "/api/evidence/export",
            query={"from": "2000-01-01T00:00:00+00:00", "to": "2999-01-01T00:00:00+00:00"},
        )
        bundle = payload["data"]
        self.assertEqual(bundle["selection"]["mode"], "window")
        self.assertFalse(bundle["reconciliation_binding"])
        self.assertTrue(bundle["chain_intact"])

    def test_reverify_exported_bundle(self) -> None:
        self._run_a_job()
        _, exported = dispatch(self.console, "GET", "/api/evidence/export", query={"job": "J-1"})
        bundle = exported["data"]
        _, payload = dispatch(
            self.console, "POST", "/api/evidence/verify", {"bundle": bundle}
        )
        self.assertTrue(payload["data"]["ok"])
        # Remove a row from the live ledger after export: re-verification must catch it.
        entries = self.console.audit.snapshot()
        self.console.audit.restore(entries[:-2])
        _, payload = dispatch(
            self.console, "POST", "/api/evidence/verify", {"bundle": bundle}
        )
        self.assertFalse(payload["data"]["ok"])
        self.assertTrue(
            any(item["kind"] == "missing-now" for item in payload["data"]["findings"])
        )


class BufferOverflowTests(unittest.TestCase):
    def test_overflow_truncating_an_open_job_fails_the_export(self) -> None:
        app = build_app("memory", web_root=WEB_ROOT)
        console = app.console
        dispatch(console, "POST", "/api/evidence/job/start", {"job_id": "J-9"})
        # The default wiring audit holds 1000 rows; overflow it after the job opens.
        for index in range(1005):
            console.audit.record("system", "tick", str(index))
        _, payload = dispatch(console, "GET", "/api/evidence/export", query={"job": "J-9"})
        bundle = payload["data"]
        self.assertFalse(bundle["passed"])
        self.assertTrue(
            any(item["kind"] == "buffer-overflow" for item in bundle["anomalies"])
        )
        app.shutdown()


if __name__ == "__main__":
    unittest.main()
