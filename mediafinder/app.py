"""FastAPI app: JSON API + static dashboard."""
from __future__ import annotations

import asyncio
import csv
import io
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from . import db
from .config import Settings, load_settings
from .sync import SyncManager

STATIC = Path(__file__).parent / "static"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    sync = SyncManager(settings)

    async def periodic():
        while True:
            await asyncio.sleep(settings.sync_interval_minutes * 60)
            await asyncio.to_thread(sync.run)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        tasks = []
        if settings.sync_on_start and (settings.jellyfin_enabled or settings.media_paths):
            tasks.append(asyncio.create_task(asyncio.to_thread(sync.run)))
        if settings.sync_interval_minutes > 0:
            tasks.append(asyncio.create_task(periodic()))
        yield
        for t in tasks:
            t.cancel()

    app = FastAPI(title="MediaFinder", lifespan=lifespan)

    def conn():
        return db.connect(settings.db_path)

    @app.get("/api/search")
    def search(q: str = "", type: str | None = None, library: str | None = None,
               source: str | None = None, year: int | None = None,
               limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)):
        c = conn()
        try:
            total, items = db.search(c, q, type, library, source, year, limit, offset)
        finally:
            c.close()
        return {"total": total, "items": items}

    @app.get("/api/libraries")
    def libraries():
        c = conn()
        try:
            return db.libraries(c)
        finally:
            c.close()

    @app.get("/api/stats")
    def stats():
        c = conn()
        try:
            return db.stats(c)
        finally:
            c.close()

    @app.get("/api/config")
    def config():
        return {"jellyfin": settings.jellyfin_enabled, "media_paths": settings.media_paths}

    @app.post("/api/sync", status_code=202)
    async def start_sync():
        if sync.status["running"]:
            return {"started": False, "status": sync.status}
        asyncio.create_task(asyncio.to_thread(sync.run))
        sync.status["running"] = True
        return {"started": True}

    @app.get("/api/sync/status")
    def sync_status():
        return sync.status

    @app.get("/api/export")
    def export(format: str = Query("csv", pattern="^(csv|json|txt)$"), q: str = "",
               type: str | None = None, library: str | None = None,
               source: str | None = None, year: int | None = None,
               dedupe: bool = True):
        """Download the title list (respects the same filters as search)."""
        c = conn()
        try:
            _, items = db.search(c, q, type, library, source, year, limit=None)
        finally:
            c.close()
        if dedupe:  # same title may appear in both Jellyfin and the folder scan
            seen, unique = set(), []
            for i in items:
                k = (i["type"], i["title"].lower(), i["year"])
                if k not in seen:
                    seen.add(k)
                    unique.append(i)
            items = unique
        if format == "json":
            body = json.dumps([{k: i[k] for k in ("title", "year", "type", "library")}
                               for i in items], indent=2, ensure_ascii=False)
            media = "application/json"
        elif format == "txt":
            body = "\n".join(f"{i['title']} ({i['year']})" if i["year"] else i["title"]
                             for i in items) + "\n"
            media = "text/plain; charset=utf-8"
        else:
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["title", "year", "type", "library", "source", "path"])
            for i in items:
                w.writerow([i["title"], i["year"] or "", i["type"], i["library"],
                            i["source"], i["path"] or ""])
            body, media = buf.getvalue(), "text/csv; charset=utf-8"
        return Response(body, media_type=media, headers={
            "Content-Disposition": f'attachment; filename="mediafinder.{format}"'})

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


def app_factory() -> FastAPI:
    return create_app()
