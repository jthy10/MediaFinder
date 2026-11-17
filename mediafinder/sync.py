"""Run syncs of all configured sources, tracking status."""
from __future__ import annotations

import threading
import time

from . import db
from .config import Settings
from .sources import folder, jellyfin


class SyncManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._lock = threading.Lock()
        self.status: dict = {"running": False, "last_run": None, "results": {}, "errors": {}}

    def run(self) -> bool:
        """Run a sync. Returns False if one is already in progress."""
        if not self._lock.acquire(blocking=False):
            return False
        try:
            self.status.update(running=True, errors={})
            conn = db.connect(self.settings.db_path)
            try:
                if self.settings.jellyfin_enabled:
                    self._one(conn, "jellyfin", lambda: jellyfin.fetch_items(
                        self.settings.jellyfin_url, self.settings.jellyfin_api_key))
                if self.settings.media_paths:
                    self._one(conn, "folder", lambda: folder.scan_paths(self.settings.media_paths))
            finally:
                conn.close()
            self.status.update(last_run=time.time())
            return True
        finally:
            self.status["running"] = False
            self._lock.release()

    def _one(self, conn, source, fetch):
        try:
            self.status["results"][source] = db.replace_source(conn, source, fetch())
        except Exception as e:  # keep old data for this source if it fails
            self.status["errors"][source] = f"{type(e).__name__}: {e}"
