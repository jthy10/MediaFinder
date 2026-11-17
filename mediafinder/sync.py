"""Run syncs of all configured sources, tracking live progress and the last outcome of each."""
from __future__ import annotations

import threading
import time

from . import db
from .config import ConfigStore, Settings
from .sources import folder, jellyfin, smb


class SyncManager:
    def __init__(self, settings: Settings, store: ConfigStore):
        self.settings, self.store = settings, store
        self._lock = threading.Lock()
        self.running = False
        self.last_run: float | None = None
        self.live: dict[str, dict] = {}   # per-source state during / after the current run

    def has_sources(self) -> bool:
        c = self.store.snapshot()
        return bool(c["jellyfin"] or c["sources"])

    def source_keys(self) -> list[str]:
        c = self.store.snapshot()
        return (["jellyfin"] if c["jellyfin"] else []) + [s["label"] for s in c["sources"]]

    def snapshot(self) -> dict:
        """Live progress merged with the persisted outcome of the previous runs."""
        conn = db.connect(self.settings.db_path)
        try:
            log = db.sync_log(conn)
        finally:
            conn.close()
        sources = {}
        for key in self.source_keys():
            last = log.get(key)
            entry = {"state": "idle", "files": None, "total": None, "added": None, "removed": None,
                     "error": None, "finished": None}
            if last:
                entry.update(state="error" if last["error"] else "done", total=last["total"],
                             added=last["added"], removed=last["removed"],
                             error=last["error"], finished=last["finished"])
            entry.update(self.live.get(key, {}))
            sources[key] = entry
        return {"running": self.running, "last_run": self.last_run, "sources": sources,
                "errors": {k: v["error"] for k, v in sources.items() if v["error"]}}

    def run(self) -> bool:
        """Run a sync. Returns False if one is already in progress."""
        if not self._lock.acquire(blocking=False):
            return False
        try:
            self.running = True
            cfg = self.store.snapshot()
            jobs = {}
            if (jf := cfg["jellyfin"]):
                jobs["jellyfin"] = lambda cb: jellyfin.fetch_items(
                    jf["url"], jf["token"], jf.get("user_id"), progress=cb)
            for s in cfg["sources"]:
                scan = smb.scan_source if s["type"] == "smb" else folder.scan_source
                jobs[s["label"]] = lambda cb, s=s, scan=scan: scan(s, cb)
            self.live = {k: {"state": "pending", "files": None, "error": None} for k in jobs}
            conn = db.connect(self.settings.db_path)
            try:
                db.prune_sources(conn, list(jobs))  # forget sources the user removed
                for key, fetch in jobs.items():
                    entry = self.live[key]
                    entry["state"] = "running"
                    try:
                        items = fetch(lambda n, e=entry: e.__setitem__("files", n))
                        stats = db.replace_source(conn, key, items)
                        entry.update(state="done", error=None, finished=time.time(), **stats)
                        db.log_sync(conn, key, stats, None)
                    except Exception as e:  # keep the old data for a source that fails
                        msg = f"{type(e).__name__}: {e}"
                        entry.update(state="error", error=msg, finished=time.time())
                        db.log_sync(conn, key, None, msg)
            finally:
                conn.close()
            self.last_run = time.time()
            return True
        finally:
            self.running = False
            self._lock.release()
