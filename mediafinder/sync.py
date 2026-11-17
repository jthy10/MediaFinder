"""Run syncs of all configured sources, tracking status."""
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
        self.status: dict = {"running": False, "last_run": None, "results": {}, "errors": {}}

    def has_sources(self) -> bool:
        c = self.store.snapshot()
        return bool(c["jellyfin"] or c["sources"])

    def run(self) -> bool:
        """Run a sync. Returns False if one is already in progress."""
        if not self._lock.acquire(blocking=False):
            return False
        try:
            self.status.update(running=True, errors={}, results={})
            cfg = self.store.snapshot()
            jobs = {}
            if (jf := cfg["jellyfin"]):
                jobs["jellyfin"] = lambda: jellyfin.fetch_items(jf["url"], jf["token"], jf.get("user_id"))
            for s in cfg["sources"]:
                scan = smb.scan_source if s["type"] == "smb" else folder.scan_source
                jobs[s["label"]] = lambda s=s, scan=scan: scan(s)
            conn = db.connect(self.settings.db_path)
            try:
                db.prune_sources(conn, list(jobs))  # forget sources the user removed
                for key, fetch in jobs.items():
                    try:
                        self.status["results"][key] = db.replace_source(conn, key, fetch())
                    except Exception as e:  # keep the old data for a source that fails
                        self.status["errors"][key] = f"{type(e).__name__}: {e}"
            finally:
                conn.close()
            self.status["last_run"] = time.time()
            return True
        finally:
            self.status["running"] = False
            self._lock.release()
