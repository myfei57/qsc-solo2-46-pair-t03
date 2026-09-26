"""Read-only views rendered by the console API.

Each view flattens one module's state into JSON-friendly primitives so the
frontend never reaches into the control objects directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import cluster as fleet
from . import limit as limit_module
from . import slew as slew_module
from . import zone as zone_module
from .config import config_view
from .geometry import compute_separation
from .store import store_count, store_size

if TYPE_CHECKING:
    from .console import Console


def zone_view(console: "Console") -> dict[str, object]:
    zone = console.zone
    positions = fleet.current_positions(console.cluster)
    coverage = zone_module.coverage_check(zone, positions)
    occupied = {crane_id: zone.occupied_by(crane_id) for crane_id in zone_module.occupants_snapshot(zone)}
    separation = zone_module.separation(zone, fleet.baseline_positions(console.cluster))
    return {
        "id": zone.id,
        "name": zone.name,
        "boundary": [point.to_dict() for point in zone.boundary_copy()],
        "revision": zone.durable_revision(),
        "durable": zone.is_durable(),
        "occupants": zone_module.occupants_snapshot(zone),
        "conflict": zone.is_conflict(),
        "latch": zone_module.is_latched(zone),
        "coverage": coverage.to_dict(),
        "occupied": occupied,
        "registrations": zone_module.registration_count(zone),
        "boundary_area": zone_module.boundary_area(zone),
        "occupant_count": zone.occupant_count(),
        "separation": separation.to_dict(),
        "latest_registration_seq": zone_module.latest_registration_seq(zone),
        "latch_generation": zone_module.latch_generation(zone),
        "pending_releases": [entry.to_dict() for entry in zone_module.pending_releases(zone)],
        "occupancy_version": zone_module.occupancy_version(zone),
    }


def slew_view(console: "Console") -> dict[str, object]:
    slew = console.slew
    return {
        "id": slew.id,
        "angle": slew.angle_now(),
        "moving": slew.is_moving(),
        "stopped": slew.is_stopped(),
        "wind_held": slew.held_by_wind(),
        "latch_held": slew.held_by_latch(),
        "faulted": slew.is_faulted(),
        "stop_count": slew.stop_count_now(),
        "last_stop_sequence": slew.last_stop_sequence_now(),
        "stop_reason": slew_module.stop_reason(slew, "slew"),
        "stop_sequence": slew_module.stop_sequence(slew, "slew"),
        "axis_stopped": {
            axis: slew_module.is_axis_stopped(slew, axis)
            for axis in ("slew", "trolley", "hoist")
        },
        "ramp_target": console.ramp.target_now(),
        "ramp_velocity": console.ramp.velocity_now(),
        "ramp_on_target": slew_module.ramp_reached(console.ramp),
    }


def trolley_view(console: "Console") -> dict[str, object]:
    trolley = console.trolley
    return {
        "id": trolley.id,
        "position": trolley.position_now(),
        "moving": trolley.is_moving(),
        "last_speed": console.speedometer.last_speed(),
    }


def hoist_view(console: "Console") -> dict[str, object]:
    hoist = console.hoist
    drum = console.drum
    return {
        "id": hoist.id,
        "hook_lowered": hoist.hook_lowered(),
        "brake_engaged": hoist.brake_on(),
        "moving": hoist.is_moving(),
        "drum_layers": drum.layer_count(),
        "cable_height": drum.height_from_cable(drum.wound_total()),
        "feedback_speed": console.feedback.speed_now(),
    }


def wind_view(console: "Console") -> dict[str, object]:
    wind = console.wind
    return {
        "level": wind.current_level(),
        "active": wind.is_active(),
        "latch": wind.is_latched(),
        "gust_peak": console.gust.peak(),
        "gust_samples": console.gust.samples(),
        "alarm_sequence": wind.alarm_sequence(),
        "level_history": wind.history(),
        "generation": wind.generation_now(),
    }


def limit_view(console: "Console") -> dict[str, object]:
    limit = console.limit
    axes = (limit_module.AXIS_SLEW, limit_module.AXIS_TROLLEY, limit_module.AXIS_HOIST)
    return {
        "tripped": {axis: limit.is_tripped(axis) for axis in axes},
        "stopped": {axis: limit.is_stopped(axis) for axis in axes},
        "latch": limit.is_latched(),
        "generation": limit.generation_now(),
    }


def cluster_view(console: "Console") -> dict[str, object]:
    cluster = console.cluster
    current = {crane_id: point.to_dict() for crane_id, point in fleet.current_positions(cluster).items()}
    baseline = {crane_id: point.to_dict() for crane_id, point in fleet.baseline_positions(cluster).items()}
    release_counts = {crane_id: fleet.release_count(cluster, crane_id) for crane_id in current}
    return {
        "positions": current,
        "baseline": baseline,
        "revision": fleet.revision_now(cluster),
        "baseline_revision": fleet.baseline_revision(cluster),
        "released": fleet.released_snapshot(cluster),
        "release_generation": fleet.release_generation_now(cluster),
        "release_counts": release_counts,
        "release_ledger": [record.to_dict() for record in fleet.release_ledger_now(cluster)],
        "nearest_pair": nearest_pair_view(console),
        "fleet_center": fleet.fleet_center(cluster).to_dict(),
        "spread": fleet.spread(cluster),
        "mean_spacing": fleet.grid_spacing(cluster),
        "spacing_deviation": fleet.spacing_deviation(cluster),
    }


def nearest_pair_view(console: "Console") -> dict[str, object] | None:
    pair = fleet.nearest_pair(console.cluster)
    if pair is None:
        return None
    safe = console.config.safe_separation
    separation = compute_separation(pair.line.start, pair.line.end, safe)
    return {
        "pair": pair.to_dict(),
        "center": pair.center().to_dict(),
        "clearance": fleet.pair_clearance_from_center(console.cluster, pair),
        "length": pair.line.length(),
        "safe_margin": fleet.pair_separation(pair, safe),
        "below_safe": separation.below_safe,
    }


def crane_view(console: "Console") -> dict[str, object]:
    crane = console.default_crane()
    if crane is None:
        return {}
    return {
        "id": crane.id,
        "last_command": crane.last_command().to_dict(),
        "stops_pending": crane.stops_pending(),
        "sway_angle": console.load.sway_angle(),
        "momentum": console.load.momentum(),
        "load_height": console.load.height(),
        "command_sequence": crane.command_sequence(),
        "stop_sequence": crane.stop_sequence_now(),
        "blocked_moves": crane.blocked_moves_now(),
    }


def load_view(console: "Console") -> dict[str, object]:
    return {
        "sway_angle": console.load.sway_angle(),
        "momentum": console.load.momentum(),
        "height": console.load.height(),
        "mass": console.load.mass,
    }


def state_view(console: "Console") -> dict[str, object]:
    """The aggregate snapshot served on ``/api/state``."""

    alarm_count = console.audit.count_by_action("wind-alarm") + console.audit.count_by_action(
        "zone-conflict"
    )
    latest_wind_stop = console.audit.latest("wind-slew-stop")
    return {
        "zone": zone_view(console),
        "slew": slew_view(console),
        "trolley": trolley_view(console),
        "hoist": hoist_view(console),
        "wind": wind_view(console),
        "limit": limit_view(console),
        "cluster": cluster_view(console),
        "crane": crane_view(console),
        "load": load_view(console),
        "config": config_view(console.config, console.cluster),
        "audit": console.audit.count(),
        "audit_capacity": console.audit.capacity,
        "alarms": alarm_count,
        "latest_wind_stop": latest_wind_stop.to_dict() if latest_wind_stop else None,
        "store": {"keys": store_count(console.store), "bytes": store_size(console.store)},
    }


__all__ = [
    "cluster_view",
    "crane_view",
    "hoist_view",
    "limit_view",
    "load_view",
    "nearest_pair_view",
    "slew_view",
    "state_view",
    "trolley_view",
    "wind_view",
    "zone_view",
]
