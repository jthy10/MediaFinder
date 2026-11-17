"""Configuration.

Environment variables only provide bootstrap defaults (db path, optional seed values).
Everything the user sets up in the UI (Jellyfin server, NAS shares, sync interval) is
persisted in ``config.json`` next to the database, so it survives restarts. Credentials
inside it are encrypted (see security.SecretBox).
"""
from __future__ import annotations

import copy
import json
import os
import threading
import uuid
from dataclasses import dataclass, field

from .security import SecretBox


@dataclass
class Settings:
    db_path: str = "/data/mediafinder.db"
    config_path: str = ""          # default: config.json beside the database
    sync_on_start: bool = True
    # Folders the in-app folder browser may show / sources may point at. "*" = anywhere.
    browse_roots: list[str] = field(default_factory=lambda: ["/media", "/mnt", "/srv"])
    admin_password: str = ""       # optional; if set, the UI requires it (can't be changed in the UI)
    secret_key: str = ""           # optional; otherwise a key file is created next to config.json
    # Optional seeds, used only when no config.json exists yet:
    jellyfin_url: str = ""
    jellyfin_api_key: str = ""
    media_paths: list[str] = field(default_factory=list)
    sync_interval_minutes: int = 0


def _csv(v: str) -> list[str]:
    return [p.strip() for p in v.split(",") if p.strip()]


def load_settings() -> Settings:
    env = os.environ.get
    default_db = "/data/mediafinder.db" if os.path.isdir("/data") else "./data/mediafinder.db"
    roots = _csv(env("BROWSE_ROOTS", "")) or (["*"] if os.name == "nt" else ["/media", "/mnt", "/srv"])
    return Settings(
        db_path=env("DB_PATH") or default_db,
        config_path=env("CONFIG_PATH", ""),
        sync_on_start=env("SYNC_ON_START", "true").lower() in ("1", "true", "yes"),
        browse_roots=roots,
        admin_password=env("MEDIAFINDER_PASSWORD", ""),
        secret_key=env("MEDIAFINDER_SECRET_KEY", ""),
        jellyfin_url=env("JELLYFIN_URL", "").rstrip("/"),
        jellyfin_api_key=env("JELLYFIN_API_KEY", ""),
        media_paths=_csv(env("MEDIA_PATHS", "")),
        sync_interval_minutes=int(env("SYNC_INTERVAL_MINUTES", "0") or 0),
    )


def _defaults() -> dict:
    return {"jellyfin": None, "sources": [], "sync_interval_minutes": 0, "auth": None}


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


# (path into the config dict, key) pairs that hold secrets
def _secret_slots(data: dict):
    if data.get("jellyfin"):
        yield data["jellyfin"], "token"
    for s in data.get("sources", []):
        yield s, "password"
    if data.get("tmdb"):
        yield data["tmdb"], "api_key"


class ConfigStore:
    def __init__(self, settings: Settings):
        self.path = settings.config_path or os.path.join(
            os.path.dirname(os.path.abspath(settings.db_path)), "config.json")
        self.box = SecretBox(os.path.dirname(self.path), settings.secret_key)
        self._lock = threading.RLock()
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as f:
                self._data = {**_defaults(), **json.load(f)}
            legacy = False
            for holder, key in _secret_slots(self._data):
                raw = holder.get(key, "")
                legacy = legacy or bool(raw and not raw.startswith("enc:"))
                holder[key] = self.box.decrypt(raw)
            if legacy:  # config written by an older version: encrypt credentials now
                self.mutate(lambda d: None)
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
            out = copy.deepcopy(self._data)
            for holder, key in _secret_slots(out):
                holder[key] = self.box.encrypt(holder.get(key, ""))
            tmp = self.path + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(out, f, indent=2)
            os.replace(tmp, self.path)

    def public(self) -> dict:
        """Config safe to send to the browser: no tokens, passwords or hashes."""
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
            "tmdb": bool(d.get("tmdb") and d["tmdb"].get("api_key")),
        }
