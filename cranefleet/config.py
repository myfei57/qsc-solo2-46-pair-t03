"""Site configuration persisted alongside the control snapshots."""

from __future__ import annotations

import json
from dataclasses import dataclass

from .cluster import Cluster, current_positions
from .errors import ConfigError, StoreNotFound
from .store import Store
from .wind import ALARM_THRESHOLD

CONFIG_KEY = "config/site"
DEFAULT_SAFE_SEPARATION = 12.0
DEFAULT_SITE_NAME = "华东三号工地"


@dataclass(frozen=True)
class SiteConfig:
    """Operator-editable settings for the site."""

    site_name: str = DEFAULT_SITE_NAME
    safe_separation: float = DEFAULT_SAFE_SEPARATION

    def to_dict(self) -> dict[str, object]:
        return {"site_name": self.site_name, "safe_separation": self.safe_separation}

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "SiteConfig":
        try:
            name = str(payload.get("site_name") or DEFAULT_SITE_NAME)
            separation = float(payload.get("safe_separation", DEFAULT_SAFE_SEPARATION))
        except (TypeError, ValueError) as exc:
            raise ConfigError("invalid site configuration") from exc
        if separation <= 0:
            raise ConfigError("safe_separation must be positive")
        return cls(site_name=name, safe_separation=separation)


def load_config(store: Store) -> SiteConfig:
    try:
        payload = store.load(CONFIG_KEY)
    except StoreNotFound:
        return SiteConfig()
    return SiteConfig.from_dict(json.loads(payload.decode("utf-8")))


def save_config(store: Store, config: SiteConfig) -> None:
    store.save(CONFIG_KEY, json.dumps(config.to_dict(), ensure_ascii=False).encode("utf-8"))


def config_view(config: SiteConfig, cluster: Cluster) -> dict[str, object]:
    """Render the configuration together with live fleet counters."""

    return {
        "site_name": config.site_name,
        "safe_separation": config.safe_separation,
        "alarm_threshold": ALARM_THRESHOLD,
        "surveyed_cranes": len(current_positions(cluster)),
    }
