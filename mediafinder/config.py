"""Configuration.

Environment variables only provide bootstrap defaults (db path, optional seed values).
Everything the user sets up in the UI (Jellyfin server, NAS shares, sync interval) is
persisted in ``config.json`` next to the database, so it survives container restarts.
"""
from __future__ import annotations

import copy
import json
import os
import threading
import uuid
from dataclasses import dataclass, field


@dataclass
class Settings:
    db_path: str = "/data/mediafinder.db"
    config_path: str = ""          # default: config.json beside the database
    sync_on_start: bool = True
    # Optional seeds, used only when no config.json exists yet:
    jellyfin_url: str = ""
    jellyfin_api_key: str = ""
    media_paths: list[str] = field(default_factory=list)
    sync_interval_minutes: int = 0


def load_settings() -> Settings:
    env = os.environ.get
    return Settings(
        db_path=env("DB_PATH") or ("/data/mediafinder.db" if os.path.isdir("/data") else "./data/mediafinder.db"),
        config_path=env("CONFIG_PATH", ""),
        sync_on_start=env("SYNC_ON_START", "true").lower() in ("1", "true", "yes"),
        jellyfin_url=env("JELLYFIN_URL", "").rstrip("/"),
        jellyfin_api_key=env("JELLYFIN_API_KEY", ""),
        media_paths=[p.strip() for p in env("MEDIA_PATHS", "").split(",") if p.strip()],
        sync_interval_minutes=int(env("SYNC_INTERVAL_MINUTES", "0") or 0),
    )


def _defaults() -> dict:
    return {"jellyfin": None, "sources": [], "sync_interval_minutes": 0}


def make_source(kind: str, **kw) -> dict:
    """Build a folder source. kind = 'local' (path inside container) or 'smb' (NAS share)."""
    src = {"id": uuid.uuid4().hex[:8], "type": kind,
           "mode": kw.get("mode", "root"),    # root = subfolders are libraries; library = folder is one library
           "hint": kw.get("hint", "auto")}    # auto | movie | episode
    if kind == "smb":
        src.update(host=kw["host"], share=kw["share"], path="/" + kw.get("path", "").strip("/"),
                   username=kw.get("username", ""), password=kw.get("password", ""),
                   domain=kw.get("domain", ""))
        default = f"smb://{src['host']}/{src['share']}" + ("" if src["path"] == "/" else src["path"])
    else:
        src["path"] = kw["path"]
        default = kw["path"]
    src["label"] = (kw.get("label") or default).strip()
    return src


class ConfigStore:
    def __init__(self, settings: Settings):
        self.path = settings.config_path or os.path.join(
            os.path.dirname(os.path.abspath(settings.db_path)), "config.json")
        self._lock = threading.RLock()
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as f:
                self._data = {**_defaults(), **json.load(f)}
        else:
            self._data = self._seed(settings)

    @staticmethod
    def _seed(s: Settings) -> dict:
        data = _defaults()
        if s.jellyfin_url and s.jellyfin_api_key:
            data["jellyfin"] = {"url": s.jellyfin_url, "token": s.jellyfin_api_key,
                                "user_id": None, "name": "Jellyfin"}
        data["sources"] = [make_source("local", path=p, mode="root") for p in s.media_paths]
        data["sync_interval_minutes"] = s.sync_interval_minutes
        return data

    def snapshot(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._data)

    def mutate(self, fn) -> None:
        with self._lock:
            fn(self._data)
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2)
            try:
                os.chmod(tmp, 0o600)  # contains credentials
            except OSError:
                pass
            os.replace(tmp, self.path)

    def public(self) -> dict:
        """Config safe to send to the browser: no tokens or passwords."""
        d = self.snapshot()
        jf = d["jellyfin"]
        out_sources = []
        for s in d["sources"]:
            s = dict(s)
            s["has_password"] = bool(s.pop("password", ""))
            out_sources.append(s)
        return {
            "jellyfin": {"url": jf["url"], "name": jf.get("name")} if jf else None,
            "sources": out_sources,
            "sync_interval_minutes": d["sync_interval_minutes"],
        }
