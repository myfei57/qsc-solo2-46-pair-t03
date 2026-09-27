"""Assembles the control service and manages its boot snapshots."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from . import cluster as fleet
from . import config as site_config
from . import slew as slew_module
from . import zone as zone_module
from .audit import Audit, AuditEntry
from .cluster import Cluster
from .console import Console
from .crane import Crane
from .errors import StoreNotFound
from .evidence import JobBook
from .geometry import Point
from .hoist import Hoist
from .limit import Limit
from .slew import Slew, SpeedFeedback
from .store import FileStore, MemoryStore, Store, decode_snapshot, encode_snapshot
from .trolley import Trolley
from .wind import Wind
from .zone import ZoneState

AUDIT_KEY = "audit/entries"
MEMORY_DIRECTORIES = {"", "memory", ":memory:"}


@dataclass(frozen=True)
class App:
    """The assembled service plus everything needed to shut it down cleanly."""

    console: Console
    store: Store
    audit: Audit
    snapshot_path: Path | None

    def shutdown(self) -> None:
        """Flush the durable state and write the boot snapshot."""

        self.audit.record("system", "shutdown", "control service stopping")
        flush_audit(self.store, self.audit)
        site_config.save_config(self.store, self.console.config)
        if self.snapshot_path is not None:
            self.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            self.snapshot_path.write_bytes(encode_snapshot(self.store.snapshot()))


def default_positions() -> dict[str, Point]:
    return {"crane-1": Point(0.0, 0.0), "crane-2": Point(80.0, 0.0)}


def default_boundary() -> list[Point]:
    return [Point(-10.0, -10.0), Point(110.0, -10.0), Point(110.0, 110.0), Point(-10.0, 110.0)]


def build_app(data_dir: str = "data", web_root: str = "web") -> App:
    """Create the store, the components and the console for one run."""

    if data_dir in MEMORY_DIRECTORIES:
        store: Store = MemoryStore()
        snapshot_path: Path | None = None
    else:
        store = FileStore(data_dir)
        snapshot_path = Path(data_dir) / "snapshot.json"
        restore_boot(store, snapshot_path)

    cluster = Cluster(default_positions())
    zone = ZoneState("zone-1", "吊装作业区", default_boundary())
    zone_module.restore(zone, store)
    fleet.restore_baseline(cluster, store)

    slew = Slew("crane-1")
    trolley = Trolley("crane-1")
    hoist = Hoist("crane-1")
    wind = Wind()
    limit = Limit()
    audit = Audit(1000)
    restore_audit(store, audit)
    job_book = JobBook.load(store)
    feedback = SpeedFeedback("crane-1")
    slew_module.restore_feedback(feedback, store)
    cranes = {"crane-1": Crane("crane-1")}
    config = site_config.load_config(store)

    console = Console(
        store=store,
        cluster=cluster,
        zone=zone,
        slew=slew,
        trolley=trolley,
        hoist=hoist,
        wind=wind,
        limit=limit,
        audit=audit,
        cranes=cranes,
        feedback=feedback,
        config=config,
        web_root=web_root,
        job_book=job_book,
    )
    audit.record("system", "boot", "control service started")
    return App(console=console, store=store, audit=audit, snapshot_path=snapshot_path)


def restore_boot(store: Store, snapshot_path: Path) -> None:
    try:
        payload = snapshot_path.read_bytes()
    except FileNotFoundError:
        return
    store.restore(decode_snapshot(payload))


def restore_audit(store: Store, audit: Audit) -> None:
    try:
        payload = store.load(AUDIT_KEY)
    except StoreNotFound:
        return
    entries = [AuditEntry.from_dict(item) for item in json.loads(payload.decode("utf-8"))]
    audit.restore(entries)


def flush_audit(store: Store, audit: Audit) -> None:
    payload = json.dumps(
        [entry.to_dict() for entry in audit.snapshot()], ensure_ascii=False
    ).encode("utf-8")
    store.save(AUDIT_KEY, payload)
