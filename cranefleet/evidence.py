"""Per-job evidence export with line-by-line verification.

A *job* is one stretch of site work, bracketed by ``job-start`` / ``job-end``
audit entries. When the book is closed (or any time window is selected) the
entries of that stretch can be exported as a self-contained evidence bundle.
Export never copies rows blindly:

1. Every entry is replayed against its SHA-256 chain digest. A field changed
   after the fact fails its digest; a deleted row shows up as a sequence gap;
   a ring-buffer overflow that dropped part of the job is reported explicitly.
2. The log is reconciled against the machine's own counters, which were
   snapshotted when the job opened and closed (occupancy registrations, latch
   releases, wind-alarm sequence, wind stop count, command sequence, survey
   revisions, limit arbitrations). Where the field ledger and the hardware-side
   count disagree, that check is marked ``mismatch`` and the two numbers are
   both printed -- the bundle never papers over the difference.

The bundle carries an overall ``passed`` that is true only when the chain is
intact and every reconciliation matches. A human-readable ``report`` is
included so the exported file can be read on site without the service.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Sequence

from .audit import AuditEntry, GENESIS_DIGEST, verify_entry
from .errors import StoreError, StoreNotFound

if TYPE_CHECKING:
    from .console import Console

EVIDENCE_VERSION = 1
JOBS_KEY = "evidence/jobs"
JOB_OPEN = "job-start"
JOB_CLOSE = "job-end"


# --------------------------------------------------------------------- jobs


@dataclass(frozen=True)
class JobSegment:
    """One declared stretch of work with the counters at its two edges."""

    job_id: str
    label: str
    opened_at: str
    opened_seq: int
    open_counters: dict[str, int]
    closed_at: str = ""
    closed_seq: int = 0
    close_counters: dict[str, int] = field(default_factory=dict)
    digest: str = ""

    @property
    def is_closed(self) -> bool:
        return bool(self.closed_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "label": self.label,
            "opened_at": self.opened_at,
            "opened_seq": self.opened_seq,
            "open_counters": self.open_counters,
            "closed_at": self.closed_at,
            "closed_seq": self.closed_seq,
            "close_counters": self.close_counters,
            "digest": self.digest,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "JobSegment":
        return cls(
            job_id=str(payload["job_id"]),
            label=str(payload.get("label") or ""),
            opened_at=str(payload["opened_at"]),
            opened_seq=int(payload.get("opened_seq") or 0),
            open_counters={k: int(v) for k, v in (payload.get("open_counters") or {}).items()},
            closed_at=str(payload.get("closed_at") or ""),
            closed_seq=int(payload.get("closed_seq") or 0),
            close_counters={k: int(v) for k, v in (payload.get("close_counters") or {}).items()},
            digest=str(payload.get("digest") or ""),
        )


class JobBook:
    """Persisted index of declared jobs (the ledger itself stays in audit)."""

    def __init__(self) -> None:
        self._jobs: dict[str, JobSegment] = {}

    def open_jobs(self) -> list[JobSegment]:
        return [job for job in self._jobs.values() if not job.is_closed]

    def get(self, job_id: str) -> JobSegment | None:
        return self._jobs.get(job_id)

    def add(self, job: JobSegment) -> None:
        self._jobs[job.job_id] = job

    def list(self) -> list[JobSegment]:
        return [self._jobs[key] for key in sorted(self._jobs)]

    def save(self, store: Any) -> None:
        payload = json.dumps(
            [job.to_dict() for job in self.list()], ensure_ascii=False
        ).encode("utf-8")
        store.save(JOBS_KEY, payload)

    @classmethod
    def load(cls, store: Any) -> "JobBook":
        book = cls()
        try:
            payload = store.load(JOBS_KEY)
        except StoreNotFound:
            return book
        for item in json.loads(payload.decode("utf-8")):
            job = JobSegment.from_dict(item)
            book.add(job)
        return book


# --------------------------------------------------------------- counters


def counter_snapshot(console: "Console") -> dict[str, int]:
    """Read every monotonic machine counter used for reconciliation."""

    from . import zone as zone_module

    return {
        "occupancy_registrations": zone_module.registration_count(console.zone),
        "latch_releases": len(zone_module.pending_releases(console.zone)),
        "fleet_releases": len(console.cluster.release_ledger),
        "wind_alarms": console.wind.alarm_sequence(),
        "wind_slew_stops": console.slew.stop_count_now(),
        "crane_commands": console.default_crane().command_sequence()
        if console.default_crane() is not None
        else 0,
        "survey_revisions": console.cluster.revision,
        "limit_arbitrations": console.limit.sequence,
    }


# Each counter maps to the audit action(s) that must be recorded exactly once
# when that counter advances.
COUNTER_CHECKS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("occupancy_registrations", "分区占用登记", ("zone-occupy",)),
    ("latch_releases", "放闩（分区解除）", ("zone-release",)),
    ("wind_alarms", "大风报警", ("wind-alarm",)),
    ("wind_slew_stops", "大风回转停止", ("wind-slew-stop",)),
    ("crane_commands", "司机/遥控指令", ("crane-command",)),
    ("survey_revisions", "机群测量放线", ("cluster-survey",)),
    ("limit_arbitrations", "限位仲裁停机", ("limit-enforce",)),
)


# ------------------------------------------------------------- chain check


@dataclass(frozen=True)
class ChainFinding:
    seq: int
    entry_id: str
    kind: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "id": self.entry_id,
            "kind": self.kind,
            "detail": self.detail,
        }


def verify_window(
    entries: Sequence[AuditEntry],
    anchor_digest: str = GENESIS_DIGEST,
    anchor_seq: int = 0,
) -> list[ChainFinding]:
    """Replay the hash chain for one window of the ledger.

    ``anchor_digest`` is the digest of the row immediately before the window
    (the job's opening marker for per-job exports, or the genesis digest when
    the window starts at the very first row). The first exported entry must
    link to it, which also proves nothing was inserted between marker and job.
    """

    findings: list[ChainFinding] = []
    prev_digest = anchor_digest
    expected_seq: int | None = anchor_seq + 1 if anchor_seq else None
    for entry in entries:
        if not entry.digest:
            findings.append(
                ChainFinding(entry.seq, entry.entry_id, "legacy-row", "旧版无哈希条目，无法校验")
            )
            continue
        status = verify_entry(entry, prev_digest)
        if status == "bad-digest":
            findings.append(
                ChainFinding(entry.seq, entry.entry_id, "bad-digest", "条目内容与哈希不符，疑似被改动")
            )
        elif status == "chain-broken":
            findings.append(
                ChainFinding(entry.seq, entry.entry_id, "chain-broken", "前序哈希对不上，链接断裂")
            )
        if expected_seq is not None and entry.seq != expected_seq:
            if entry.seq > expected_seq:
                findings.append(
                    ChainFinding(
                        entry.seq,
                        entry.entry_id,
                        "seq-gap",
                        f"缺少序号 {expected_seq}–{entry.seq - 1}（共 {entry.seq - expected_seq} 条）",
                    )
                )
            else:
                findings.append(
                    ChainFinding(entry.seq, entry.entry_id, "seq-order", "序号倒退或重复")
                )
        prev_digest = entry.digest
        expected_seq = entry.seq + 1
    return findings


# ---------------------------------------------------------- reconciliation


@dataclass(frozen=True)
class CheckItem:
    """One ledger-vs-machine comparison with an explicit conclusion."""

    name: str
    ledger_count: int
    machine_count: int
    status: str  # match | mismatch
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ledger": self.ledger_count,
            "machine": self.machine_count,
            "status": self.status,
            "detail": self.detail,
        }


def _count_actions(entries: Sequence[AuditEntry], actions: tuple[str, ...]) -> int:
    return sum(1 for entry in entries if entry.action in actions)


def reconcile_job(
    job: JobSegment, entries: Sequence[AuditEntry]
) -> list[CheckItem]:
    """Compare each counter delta between the job edges with the job's rows."""

    checks: list[CheckItem] = []
    for counter, label, actions in COUNTER_CHECKS:
        start = job.open_counters.get(counter, 0)
        end = job.close_counters.get(counter, start)
        machine_delta = max(0, end - start)
        ledger = _count_actions(entries, actions)
        if machine_delta == ledger:
            checks.append(
                CheckItem(
                    label,
                    ledger,
                    machine_delta,
                    "match",
                    f"现场计数 {start}→{end}，台账 {ledger} 条，一致",
                )
            )
        else:
            checks.append(
                CheckItem(
                    label,
                    ledger,
                    machine_delta,
                    "mismatch",
                    f"现场计数 {start}→{end}（差 {machine_delta}），台账仅 {ledger} 条，对不上",
                )
            )
    return checks


def reconcile_live(console: "Console", entries: Sequence[AuditEntry]) -> list[CheckItem]:
    """Best-effort reconciliation for an ad-hoc window without job edges.

    Cumulative counters are compared with the whole log so the numbers can
    disagree simply because earlier rows were dropped by the ring buffer; the
    result is reported but never decides the bundle's verdict.
    """

    counters = counter_snapshot(console)
    checks: list[CheckItem] = []
    for counter, label, actions in COUNTER_CHECKS:
        machine = counters.get(counter, 0)
        ledger = _count_actions(entries, actions)
        status = "match" if machine == ledger else "mismatch"
        detail = f"现场累计 {machine}，所选窗口台账 {ledger} 条（无作业边界，仅供参考）"
        checks.append(CheckItem(label, ledger, machine, status, detail))
    return checks


# --------------------------------------------------------------- bundling


def _anchor_for(
    all_entries: Sequence[AuditEntry], window: Sequence[AuditEntry]
) -> tuple[str, int]:
    """Find the digest/seq of the ledger row just before the window."""

    if not window:
        return GENESIS_DIGEST, 0
    first_seq = window[0].seq
    for entry in all_entries:
        if entry.seq == first_seq - 1:
            return entry.digest, entry.seq
    # The predecessor is no longer held by the ring buffer: the first row's
    # own prev_digest is the only anchor available, used without trusting it.
    return window[0].prev_digest, first_seq - 1


def _window_for_job(
    all_entries: Sequence[AuditEntry], job: JobSegment
) -> tuple[list[AuditEntry], bool]:
    """Return the rows strictly inside the job edges.

    The second value is true when the buffer can no longer hold the whole job:
    its opening marker (or rows that followed it) have been pushed out.
    """

    window = [
        entry
        for entry in all_entries
        if entry.seq > job.opened_seq and (not job.closed_seq or entry.seq <= job.closed_seq)
    ]
    if not all_entries:
        return window, job.opened_seq > 0
    opening_held = all_entries[0].seq <= job.opened_seq
    closing_held = not job.closed_seq or all_entries[-1].seq >= job.closed_seq
    truncated = not (opening_held and closing_held)
    return window, truncated


def build_bundle(
    console: "Console",
    *,
    job: JobSegment | None = None,
    start_at: str = "",
    end_at: str = "",
    crane_id: str = "",
) -> dict[str, Any]:
    """Assemble the verified evidence bundle for a job or a time window."""

    all_entries = console.audit.snapshot()
    generated_at = datetime.now(timezone.utc).isoformat()

    if job is not None:
        window, truncated = _window_for_job(all_entries, job)
        selection = {
            "mode": "job",
            "job_id": job.job_id,
            "label": job.label,
            "opened_at": job.opened_at,
            "closed_at": job.closed_at,
            "bounded": job.is_closed,
        }
    else:
        window = [
            entry
            for entry in all_entries
            if (not start_at or entry.recorded_at >= start_at)
            and (not end_at or entry.recorded_at <= end_at)
        ]
        truncated = bool(all_entries and start_at) and all_entries[0].recorded_at > start_at
        selection = {
            "mode": "window",
            "start_at": start_at,
            "end_at": end_at,
            "bounded": False,
        }
    if crane_id:
        window = [entry for entry in window if entry.crane_id == crane_id]
        selection["crane_id"] = crane_id

    anchor_digest, anchor_seq = _anchor_for(all_entries, window)
    findings = verify_window(window, anchor_digest, anchor_seq)
    checks = (
        reconcile_job(job, window)
        if job is not None and job.is_closed
        else reconcile_live(console, window)
    )

    anomalies: list[dict[str, Any]] = [finding.to_dict() for finding in findings]
    chain_ok = not findings
    overflow = truncated
    if overflow:
        anomalies.append(
            {
                "seq": 0,
                "id": "",
                "kind": "buffer-overflow",
                "detail": "环形台账容量不足，所选时段开头记录已被覆盖，导出内容不完整",
            }
        )
    if job is not None and not job.is_closed:
        anomalies.append(
            {
                "seq": job.closed_seq,
                "id": "",
                "kind": "job-open",
                "detail": "作业尚未收工，缺少结束边界，对账结论仅反映截至当前",
            }
        )

    mismatch_actions = [item.to_dict() for item in checks if item.status == "mismatch"]
    reconciliation_binding = job is not None and job.is_closed
    passed = chain_ok and not overflow and (
        not reconciliation_binding or not mismatch_actions
    )
    if passed:
        if reconciliation_binding:
            conclusion = "通过：台账哈希链完整无缺漏，且与现场计数器逐条一致。"
        else:
            conclusion = "通过：所选窗口台账哈希链完整无缺漏（无完整作业边界，未做强制对账）。"
    else:
        conclusion = "不通过：存在对不上的条目，详见 anomalies 与 checks，不得作为完整留痕归档。"

    bundle: dict[str, Any] = {
        "schema": "cranefleet-evidence/v1",
        "version": EVIDENCE_VERSION,
        "generated_at": generated_at,
        "site": console.config.site_name,
        "selection": selection,
        "log_window": {
            "head_seq": console.audit.head_seq(),
            "tail_seq": console.audit.tail_seq(),
            "entry_count": len(window),
            "capacity": console.audit.capacity,
        },
        "chain_intact": chain_ok,
        "reconciliation_binding": reconciliation_binding,
        "checks": [item.to_dict() for item in checks],
        "anomalies": anomalies,
        "passed": passed,
        "conclusion": conclusion,
        "entries": [entry.to_dict() for entry in window],
    }
    bundle["report"] = render_report(bundle)
    return bundle


# --------------------------------------------------------------- verify


def verify_bundle(console: "Console", bundle: dict[str, Any]) -> dict[str, Any]:
    """Re-check an exported bundle against the ledger held right now.

    Every entry in the bundle must still be present in the live ledger with the
    same digest, in the same order; missing or altered rows are reported.
    """

    raw_entries = bundle.get("entries") or []
    try:
        entries = [AuditEntry.from_dict(item) for item in raw_entries]
    except (KeyError, TypeError, ValueError):
        return {"ok": False, "findings": [{"kind": "malformed", "detail": "导出文件格式损坏"}]}

    by_seq = {entry.seq: entry for entry in console.audit.snapshot() if entry.seq}
    findings: list[dict[str, Any]] = []
    prev_seq = 0
    for entry in entries:
        live = by_seq.get(entry.seq)
        if live is None:
            findings.append(
                {
                    "seq": entry.seq,
                    "id": entry.entry_id,
                    "kind": "missing-now",
                    "detail": "导出后该条目已不在台账中（被覆盖或删除）",
                }
            )
            continue
        if live.digest != entry.digest:
            findings.append(
                {
                    "seq": entry.seq,
                    "id": entry.entry_id,
                    "kind": "changed-after-export",
                    "detail": "当前台账中的同序号条目与导出件不一致",
                }
            )
        if prev_seq and entry.seq != prev_seq + 1:
            findings.append(
                {
                    "seq": entry.seq,
                    "id": entry.entry_id,
                    "kind": "reorder",
                    "detail": "导出件内部序号不连续",
                }
            )
        prev_seq = entry.seq

    chain_findings = verify_window(
        entries,
        anchor_digest=entries[0].prev_digest if entries else GENESIS_DIGEST,
        anchor_seq=(entries[0].seq - 1) if entries else 0,
    )
    for finding in chain_findings:
        findings.append(finding.to_dict())

    return {"ok": not findings, "findings": findings, "entry_count": len(entries)}


# --------------------------------------------------------------- report


_STATUS_TEXT = {"match": "一致", "mismatch": "不符"}
_KIND_TEXT = {
    "bad-digest": "哈希不符",
    "chain-broken": "链接断裂",
    "seq-gap": "序号缺漏",
    "seq-order": "序号异常",
    "legacy-row": "旧版条目",
    "buffer-overflow": "记录被覆盖",
    "job-open": "作业未收工",
}


def render_report(bundle: dict[str, Any]) -> str:
    """Plain-text Chinese report readable without the service."""

    selection = bundle["selection"]
    window = bundle["log_window"]
    lines = [
        "塔吊机群作业留痕核对单",
        "=" * 40,
        f"工地：{bundle['site']}",
        f"导出时间(UTC)：{bundle['generated_at']}",
    ]
    if selection["mode"] == "job":
        lines.append(f"作业段：{selection['job_id']} {selection['label']}".rstrip())
        lines.append(f"开工(UTC)：{selection['opened_at']}")
        lines.append(f"收工(UTC)：{selection['closed_at'] or '未收工'}")
    else:
        lines.append(f"时间窗(UTC)：{selection['start_at'] or '起始'} ~ {selection['end_at'] or '至今'}")
    lines += [
        f"台账序号范围：{window['head_seq']}–{window['tail_seq']}，"
        f"本材料逐条收录 {window['entry_count']} 条",
        "",
        "一、逐条对账（现场计数器 vs 台账）",
    ]
    for check in bundle["checks"]:
        lines.append(
            f"  [{'对' if check['status'] == 'match' else '错'}] {check['name']}："
            f"现场 {check['machine']}，台账 {check['ledger']} —— "
            f"{_STATUS_TEXT.get(check['status'], check['status'])}"
        )
        if check["status"] == "mismatch":
            lines.append(f"      {check['detail']}")
    lines += ["", "二、哈希链与完整性检查"]
    if bundle["anomalies"]:
        for anomaly in bundle["anomalies"]:
            label = _KIND_TEXT.get(anomaly["kind"], anomaly["kind"])
            seq = f"序号{anomaly['seq']} " if anomaly.get("seq") else ""
            lines.append(f"  [×] {label} {seq}{anomaly['detail']}")
    else:
        lines.append("  [√] 全部条目哈希一致、序号连续，无缺漏无改动。")
    lines += ["", f"核对结论：{'通过' if bundle['passed'] else '不通过'}", bundle["conclusion"]]
    return "\n".join(lines)


def save_bundle(store: Any, job_id: str, bundle: dict[str, Any]) -> str:
    """Persist an exported bundle and return its store key."""

    key = f"evidence/bundle/{job_id or 'window'}-{bundle['generated_at'].replace(':', '')}"
    try:
        store.save(key, json.dumps(bundle, ensure_ascii=False).encode("utf-8"))
    except (OSError, StoreError) as exc:  # pragma: no cover - defensive
        raise StoreError(f"cannot persist evidence for {job_id}") from exc
    return key
