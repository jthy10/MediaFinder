"""Configuration, read from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class Settings:
    jellyfin_url: str = ""
    jellyfin_api_key: str = ""
    # Root folders to scan. Every top-level subfolder of a root is a "library"
    # (e.g. /media/Movies, /media/TV). Comma separated.
    media_paths: list[str] = field(default_factory=list)
    db_path: str = "/data/mediafinder.db"
    sync_interval_minutes: int = 0  # 0 = only sync on demand / at startup
    sync_on_start: bool = True

    @property
    def jellyfin_enabled(self) -> bool:
        return bool(self.jellyfin_url and self.jellyfin_api_key)


def load_settings() -> Settings:
    env = os.environ.get
    paths = [p.strip() for p in env("MEDIA_PATHS", "").split(",") if p.strip()]
    return Settings(
        jellyfin_url=env("JELLYFIN_URL", "").rstrip("/"),
        jellyfin_api_key=env("JELLYFIN_API_KEY", ""),
        media_paths=paths,
        db_path=env("DB_PATH", "/data/mediafinder.db"),
        sync_interval_minutes=int(env("SYNC_INTERVAL_MINUTES", "0") or 0),
        sync_on_start=env("SYNC_ON_START", "true").lower() in ("1", "true", "yes"),
    )
