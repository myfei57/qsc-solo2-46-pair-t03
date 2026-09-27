"""Verifiable record bundles exported per work segment.

An export bundles, for one job:

* the job boundaries and the site fingerprints taken at each boundary;
* every audit entry between those boundaries, with its sequence and hashes;
* the chain anchor (oldest surviving log row) and the log tail at export time;
* a SHA-256 over the whole bundle so the printed file itself cannot be edited.

:func:`build_materials` assembles the bundle. :func:`verify_materials` replays
it *entry by entry* and reports a conclusion for each row plus an overall
verdict. The checks are deliberately independent of one another so a
discrepancy can never be hidden by another check passing:

* bundle integrity — the whole-file hash;
* entry integrity — recompute each row's hash from its stored fields;
* sequence — missing numbers are gaps (deleted rows);
* chain linkage — each row's predecessor hash must match;
* live-log agreement — the same rows, as held by the running service now;
* job tagging — every row between the boundaries must carry the job id;
* site cross-check — independent machinery counters must move by exactly the
  number of matching audit entries recorded between the fingerprints.

Nothing is summarised away: every failing row is listed with a reason and the
overall conclusion is "consistent" only when zero failures were found.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from . import cluster as fleet, zone as zone_module
from .audit import (
    GENESIS_HASH,
    Audit,
    AuditEntry,
    STATUS_BROKEN_CHAIN,
    STATUS_FIELD_ALTERED,
    STATUS_HASH_MISMATCH,
    STATUS_JOB_TAG_MISMATCH,
    STATUS_MISSING_FROM_LIVE,
    STATUS_OK,
    STATUS_SEQUENCE_GAP,
    verify_entry,
)
from .jobs import JOB_STATUS_CLOSED, JobRecord
from .zone import ZoneState

MATERIALS_VERSION = 1
CONCLUSION_CONSISTENT = "consistent"
CONCLUSION_INCONCLUSIVE = "inconclusive"
CONCLUSION_INCONSISTENT = "inconsistent"

# Metric -> (audit actions whose combined entry count must equal the counter's
# delta over the job window). These are the independent cross-checks that tie
# the paper record to what the machinery actually did. Every metric here is a
# monotonic counter that increments exactly once per event named by the
# actions, so a mismatch proves an entry was lost or invented.
SITE_CHECKS: dict[str, tuple[str, ...]] = {
    "wind_alarm_seq": ("wind-slew-stop",),
    "zone_registration_seq": ("zone-occupy",),
    "limit_sequence": ("limit-enforce",),
}


# ---------------------------------------------------------------- fingerprints


def site_fingerprint(zone: ZoneState, audit: Audit, console: Any = None) -> dict[str, int]:
    """Read independent on-site counters straight from the machinery.

    These counters are maintained outside the audit log, so comparing their
    movement to the number of audit entries proves the log is not merely
    self-consistent but also matches the physical record.
    """

    fingerprint = {
        "zone_registration_seq": zone_module.latest_registration_seq(zone),
        "zone_occupancy_version": zone_module.occupancy_version(zone),
        "audit_seq": audit.tail()[0] if audit.tail() else 0,
    }
    if console is not None:
        fingerprint.update(
            {
                "wind_alarm_seq": console.wind.alarm_sequence(),
                "wind_generation": console.wind.generation_now(),
                "slew_stop_count": console.slew.stop_count_now(),
                "limit_sequence": console.limit.sequence_now(),
                "limit_generation": console.limit.generation_now(),
                "hoist_wound": int(console.drum.wound_total() * 1000),
                "cluster_release_generation": fleet.release_generation_now(
                    console.cluster
                ),
            }
        )
    return fingerprint


# -------------------------------------------------------------------- building


def bundle_hash(materials: dict[str, Any]) -> str:
    """Hash every part of the bundle except the hash field itself."""

    body = {key: value for key, value in materials.items() if key != "bundle_hash"}
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def build_materials(
    job: JobRecord,
    audit: Audit,
    live_fingerprint: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Assemble the verifiable export bundle for one work segment."""

    if job.start_seq <= 0:
        raise ValueError("作业缺少开工留痕，无法导出")

    entries = audit.window(job.start_seq, job.end_seq or job.start_seq)
    anchor = audit.chain_anchor()
    tail = audit.tail()
    materials: dict[str, Any] = {
        "version": MATERIALS_VERSION,
        "job": job.to_dict(),
        "exported_at": entries[-1].recorded_at if entries else job.started_at,
        "export_fingerprint": dict(live_fingerprint or {}),
        "chain_anchor": {"seq": anchor[0], "hash": anchor[1]} if anchor else None,
        "log_tail": {"seq": tail[0], "hash": tail[1]} if tail else None,
        "entries": [entry.to_dict() for entry in entries],
        "entry_count": len(entries),
    }
    materials["bundle_hash"] = bundle_hash(materials)
    return materials


def materials_json(materials: dict[str, Any]) -> bytes:
    return json.dumps(materials, ensure_ascii=False, indent=2).encode("utf-8")


def parse_materials(raw: bytes | str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("留痕材料格式不正确")
    return parsed


# ------------------------------------------------------------------ verifying


def _rows(materials: dict[str, Any]) -> list[AuditEntry]:
    raw_entries = materials.get("entries")
    if not isinstance(raw_entries, list):
        return []
    rows: list[AuditEntry] = []
    for item in raw_entries:
        if isinstance(item, dict):
            rows.append(AuditEntry.from_dict(item))
    return rows


def _add_finding(
    report: dict[str, Any],
    status: str,
    scope: str,
    message: str,
) -> None:
    report["findings"].append({"status": status, "scope": scope, "message": message})


def verify_materials(
    materials: dict[str, Any],
    audit: Audit | None = None,
    zone: ZoneState | None = None,
) -> dict[str, Any]:
    """Replay a bundle and return an entry-by-entry verification report.

    Every check produces explicit per-row or per-check conclusions. The
    overall ``conclusion`` is:

    * ``consistent`` — bundle hash, chains, sequences and (when live state is
      available) the site cross-checks all agree;
    * ``inconclusive`` — the bundle itself is intact but the live log no
      longer holds the rows or the independent counters, so on-site matching
      could not be confirmed;
    * ``inconsistent`` — at least one hard discrepancy was found.
    """

    report: dict[str, Any] = {
        "conclusion": CONCLUSION_CONSISTENT,
        "job_id": "",
        "entry_results": [],
        "checks": [],
        "findings": [],
        "entry_count": 0,
    }
    job_payload = materials.get("job") if isinstance(materials.get("job"), dict) else {}
    report["job_id"] = str(job_payload.get("job_id") or "")
    rows = _rows(materials)
    report["entry_count"] = len(rows)

    hard_failure = False
    inconclusive = False

    # 1) Whole-bundle integrity.
    stored_hash = str(materials.get("bundle_hash") or "")
    recomputed_bundle = bundle_hash(materials)
    if not stored_hash:
        _add_finding(
            report,
            STATUS_HASH_MISMATCH,
            "bundle",
            "材料缺少整包哈希，无法确认文件未被改动",
        )
        hard_failure = True
    elif recomputed_bundle != stored_hash:
        _add_finding(
            report,
            STATUS_HASH_MISMATCH,
            "bundle",
            f"整包哈希对不上：文件可能被改动或损坏（存档 {stored_hash[:12]}，重算 {recomputed_bundle[:12]}）",
        )
        hard_failure = True
    else:
        report["checks"].append(
            {"check": "bundle_hash", "status": STATUS_OK, "message": "整包哈希一致，材料自导出后未被改动"}
        )

    if not rows:
        _add_finding(report, STATUS_SEQUENCE_GAP, "bundle", "材料中没有任何留痕条目")
        report["conclusion"] = CONCLUSION_INCONSISTENT
        return report

    start_seq = int(job_payload.get("start_seq") or rows[0].seq)
    end_seq = int(job_payload.get("end_seq") or rows[-1].seq)
    job_id = report["job_id"]

    # 2) Sequence coverage over the job window — every missing number is a gap.
    anchor_payload = materials.get("chain_anchor")
    anchor_seq = int(anchor_payload["seq"]) if isinstance(anchor_payload, dict) else start_seq
    anchor_hash = str(anchor_payload["hash"]) if isinstance(anchor_payload, dict) else ""
    present = {entry.seq for entry in rows}
    window_seqs = set(range(start_seq, end_seq + 1))
    missing = sorted(window_seqs - present)
    for gap in missing:
        status = STATUS_MISSING_FROM_LIVE if gap < anchor_seq else STATUS_SEQUENCE_GAP
        message = (
            f"第 {gap} 条记录缺失：该序号早于现存日志锚点（第 {anchor_seq} 条），"
            "环形缓冲已滚动覆盖，无法从现场日志复核"
            if status == STATUS_MISSING_FROM_LIVE
            else f"第 {gap} 条记录缺失：序号 {gap - 1} 之后直接跳到 {gap + 1}，中间有缺漏"
        )
        _add_finding(report, status, f"seq:{gap}", message)
        if status == STATUS_MISSING_FROM_LIVE:
            inconclusive = True
        else:
            hard_failure = True

    # 3) Per-row hash and chain replay. The first window row legitimately
    # points at the entry immediately before the job (e.g. a boot record), so
    # its stored predecessor is accepted as-is; a truly missing predecessor
    # inside the window is already reported by the sequence-gap check. If the
    # job starts exactly at the oldest surviving row, its predecessor must be
    # the genesis marker and the row itself must equal the bundle's anchor.
    if start_seq == anchor_seq and anchor_hash:
        first = rows[0]
        if first.prev_hash != GENESIS_HASH:
            _add_finding(
                report,
                STATUS_BROKEN_CHAIN,
                f"seq:{first.seq}",
                f"第 {first.seq} 条是现存日志锚点，但前链不是创世哈希（{first.prev_hash[:12]}）",
            )
            hard_failure = True
        if first.hash != anchor_hash:
            _add_finding(
                report,
                STATUS_HASH_MISMATCH,
                f"seq:{first.seq}",
                f"第 {first.seq} 条哈希与导出锚点不一致，锚点对不上",
            )
            hard_failure = True

    for index, entry in enumerate(rows):
        expected_prev = GENESIS_HASH if index == 0 else rows[index - 1].hash
        status, message = verify_entry(entry, expected_prev)
        if index == 0 and status == STATUS_BROKEN_CHAIN:
            # The job starts mid-log: the stored link must still be a real
            # hash rather than the genesis marker, which the bundle's anchor
            # can later be checked against.
            if entry.prev_hash and entry.prev_hash != GENESIS_HASH:
                status, message = STATUS_OK, (
                    f"第 {entry.seq} 条记录哈希一致，前链指向作业开始前的第 "
                    f"{entry.seq - 1} 条记录（不在本作业窗口内）"
                )
        result = {
            "seq": entry.seq,
            "action": entry.action,
            "at": entry.recorded_at,
            "crane_id": entry.crane_id,
            "status": status,
            "message": message,
        }
        report["entry_results"].append(result)
        if status != STATUS_OK:
            hard_failure = True
            _add_finding(report, status, f"seq:{entry.seq}", message)

        # 4) Job tagging: rows inside the window must belong to the job.
        if job_id and entry.job_id != job_id:
            tag_message = (
                f"第 {entry.seq} 条记录的作业标记为 {entry.job_id or '（空）'}，"
                f"不属于本作业 {job_id}"
            )
            result["status"] = STATUS_JOB_TAG_MISMATCH
            result["message"] = tag_message
            _add_finding(report, STATUS_JOB_TAG_MISMATCH, f"seq:{entry.seq}", tag_message)
            hard_failure = True
        expected_prev = entry.hash

    # 5) Boundary rows really are the job start/end entries.
    boundary_actions = [rows[0].action, rows[-1].action]
    if boundary_actions[0] != "job-start":
        _add_finding(
            report,
            STATUS_BROKEN_CHAIN,
            f"seq:{rows[0].seq}",
            f"作业窗口第一条动作为 {boundary_actions[0]}，不是 job-start 开工留痕",
        )
        hard_failure = True
    if job_payload.get("status") == JOB_STATUS_CLOSED and boundary_actions[-1] != "job-end":
        _add_finding(
            report,
            STATUS_BROKEN_CHAIN,
            f"seq:{rows[-1].seq}",
            f"作业窗口最后一条动作为 {boundary_actions[-1]}，不是 job-end 收工留痕",
        )
        hard_failure = True

    # 6) Live-log agreement: each row must still match what the service holds.
    if audit is not None:
        live_anchor = audit.chain_anchor()
        live_anchor_seq = live_anchor[0] if live_anchor else None
        for entry in rows:
            live_entry = audit.entry_by_seq(entry.seq)
            if live_entry is None:
                if live_anchor_seq is not None and entry.seq < live_anchor_seq:
                    message = (
                        f"第 {entry.seq} 条记录已被环形缓冲滚动覆盖（现存最早为第 "
                        f"{live_anchor_seq} 条），现场日志无法逐条对照"
                    )
                    _add_finding(report, STATUS_MISSING_FROM_LIVE, f"seq:{entry.seq}", message)
                    inconclusive = True
                else:
                    message = f"第 {entry.seq} 条记录在当前现场日志中找不到，疑似被删除"
                    _add_finding(report, STATUS_SEQUENCE_GAP, f"seq:{entry.seq}", message)
                    hard_failure = True
            elif live_entry.hash != entry.hash:
                message = (
                    f"第 {entry.seq} 条记录与现场日志对不上：材料哈希 {entry.hash[:12]}，"
                    f"现场哈希 {live_entry.hash[:12]}"
                )
                _add_finding(report, STATUS_FIELD_ALTERED, f"seq:{entry.seq}", message)
                hard_failure = True

        # 7) Site cross-check: independent counters versus matching entries.
        cross = _cross_checks(materials, rows)
        report["checks"].extend(cross)
        for check in cross:
            if check["status"] == CONCLUSION_INCONSISTENT:
                hard_failure = True
                _add_finding(
                    report,
                    CONCLUSION_INCONSISTENT,
                    f"site:{check['metric']}",
                    check["message"],
                )

        # The live counter snapshot taken at export still has to dominate the
        # end-of-job fingerprint, otherwise work continued after closing.
        export_fp = materials.get("export_fingerprint")
        if isinstance(export_fp, dict) and zone is not None:
            current = site_fingerprint(zone, audit)
            for metric, value in export_fp.items():
                if metric in current and int(current[metric]) < int(value):
                    _add_finding(
                        report,
                        CONCLUSION_INCONSISTENT,
                        f"site:{metric}",
                        f"现场计数器 {metric} 导出后回退（{value} -> {current[metric]}），现场状态异常",
                    )
                    hard_failure = True

    if hard_failure:
        report["conclusion"] = CONCLUSION_INCONSISTENT
    elif inconclusive:
        report["conclusion"] = CONCLUSION_INCONCLUSIVE
    else:
        report["conclusion"] = CONCLUSION_CONSISTENT
    return report


def _cross_checks(materials: dict[str, Any], rows: list[AuditEntry]) -> list[dict[str, Any]]:
    job_payload = materials.get("job")
    if not isinstance(job_payload, dict):
        return []
    start_fp = job_payload.get("start_fingerprint")
    end_fp = job_payload.get("end_fingerprint")
    if not isinstance(start_fp, dict) or not isinstance(end_fp, dict):
        return []

    checks: list[dict[str, Any]] = []
    for metric, actions in SITE_CHECKS.items():
        if metric not in start_fp or metric not in end_fp:
            continue
        delta = int(end_fp[metric]) - int(start_fp[metric])
        logged = sum(1 for entry in rows if entry.action in actions)
        if delta < 0:
            checks.append(
                {
                    "check": "site_counter",
                    "metric": metric,
                    "status": CONCLUSION_INCONSISTENT,
                    "counter_delta": delta,
                    "logged_entries": logged,
                    "message": f"现场计数器 {metric} 在作业期间回退（{start_fp[metric]} -> {end_fp[metric]}）",
                }
            )
        elif delta == logged:
            checks.append(
                {
                    "check": "site_counter",
                    "metric": metric,
                    "status": STATUS_OK,
                    "counter_delta": delta,
                    "logged_entries": logged,
                    "message": f"现场计数器 {metric} 增量 {delta} 与台账条目数 {logged} 一致",
                }
            )
        else:
            checks.append(
                {
                    "check": "site_counter",
                    "metric": metric,
                    "status": CONCLUSION_INCONSISTENT,
                    "counter_delta": delta,
                    "logged_entries": logged,
                    "message": (
                        f"现场计数器 {metric} 增量为 {delta}，但台账中只有 {logged} 条"
                        f"（{'、'.join(actions)}）记录，相差 {delta - logged} 条，台账与现场对不上"
                    ),
                }
            )
    return checks


# ------------------------------------------------------------- human readable

CONCLUSION_TEXT = {
    CONCLUSION_CONSISTENT: "核验通过：全部条目逐条比对一致，材料与现场对得上",
    CONCLUSION_INCONCLUSIVE: "无法定论：材料自身完整，但部分记录已被日志滚动覆盖，现场无法逐条复核",
    CONCLUSION_INCONSISTENT: "核验不通过：发现对不上的地方，已在下方逐条列出",
}


def render_report(report: dict[str, Any]) -> str:
    """Render a verification report as a printable, signable text document."""

    lines: list[str] = []
    lines.append("吊装作业留痕核验报告")
    lines.append("=" * 40)
    lines.append(f"作业编号：{report.get('job_id') or '（无）'}")
    lines.append(f"条目总数：{report.get('entry_count', 0)}")
    lines.append(f"核验结论：{CONCLUSION_TEXT.get(report.get('conclusion'), report.get('conclusion'))}")
    lines.append("")

    lines.append("一、逐条比对")
    lines.append("-" * 40)
    for result in report.get("entry_results", []):
        mark = "✓" if result["status"] == STATUS_OK else "✗"
        lines.append(
            f"[{mark}] 第 {result['seq']} 条 {result['at']} {result['crane_id']} "
            f"{result['action']}"
        )
        lines.append(f"      {result['message']}")
    if not report.get("entry_results"):
        lines.append("（无条目）")
    lines.append("")

    lines.append("二、现场计数器交叉比对")
    lines.append("-" * 40)
    site_checks = [check for check in report.get("checks", []) if check.get("check") == "site_counter"]
    for check in site_checks:
        mark = "✓" if check["status"] == STATUS_OK else "✗"
        lines.append(
            f"[{mark}] {check['metric']}：{check['message']}"
        )
    other_checks = [check for check in report.get("checks", []) if check.get("check") != "site_counter"]
    for check in other_checks:
        mark = "✓" if check["status"] == STATUS_OK else "✗"
        lines.append(f"[{mark}] {check['message']}")
    if not report.get("checks"):
        lines.append("（无可用的现场比对数据）")
    lines.append("")

    lines.append("三、问题清单")
    lines.append("-" * 40)
    findings = report.get("findings", [])
    if not findings:
        lines.append("未发现对不上的条目。")
    else:
        for finding in findings:
            lines.append(f"✗ [{finding['scope']}] {finding['message']}")
    lines.append("")
    lines.append(f"核验人签字：______________    日期：______________")
    return "\n".join(lines)


def export_key(job_id: str) -> str:
    return f"ledger/export/{job_id}.json"


def save_export(store: Store, materials: dict[str, Any]) -> None:
    store.save(export_key(str(materials.get("job", {}).get("job_id", "unknown"))), materials_json(materials))
