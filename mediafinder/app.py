"""FastAPI app: JSON API + static dashboard."""
from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db
from .config import ConfigStore, Settings, load_settings, make_source
from .sources import discovery, folder, jellyfin, smb
from .sync import SyncManager

STATIC = Path(__file__).parent / "static"


class JellyfinConnect(BaseModel):
    url: str
    api_key: str = ""
    username: str = ""
    password: str = ""


class SmbCreds(BaseModel):
    host: str
    username: str = ""
    password: str = ""
    domain: str = ""


class SmbBrowse(SmbCreds):
    share: str
    path: str = "/"


class NewSource(BaseModel):
    type: str                       # 'local' | 'smb'
    path: str = "/"
    label: str = ""
    mode: str = "root"              # root | library
    hint: str = "auto"              # auto | movie | episode
    host: str = ""
    share: str = ""
    username: str = ""
    password: str = ""
    domain: str = ""


class Interval(BaseModel):
    minutes: int


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    store = ConfigStore(settings)
    sync = SyncManager(settings, store)

    async def periodic():
        while True:
            await asyncio.sleep(30)
            mins = store.snapshot()["sync_interval_minutes"]
            last = sync.status["last_run"] or 0
            if mins > 0 and time.time() - last >= mins * 60 and sync.has_sources():
                await asyncio.to_thread(sync.run)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        tasks = [asyncio.create_task(periodic())]
        if settings.sync_on_start and sync.has_sources():
            tasks.append(asyncio.create_task(asyncio.to_thread(sync.run)))
        yield
        for t in tasks:
            t.cancel()

    app = FastAPI(title="MediaFinder", lifespan=lifespan)

    def conn():
        return db.connect(settings.db_path)

    # ---------- search / export ----------
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
        if dedupe:  # same title may appear in several sources
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

    # ---------- sync ----------
    @app.post("/api/sync", status_code=202)
    async def start_sync():
        if sync.status["running"]:
            return {"started": False, "status": sync.status}
        sync.status["running"] = True
        asyncio.create_task(asyncio.to_thread(sync.run))
        return {"started": True}

    @app.get("/api/sync/status")
    def sync_status():
        return sync.status

    # ---------- configuration ----------
    @app.get("/api/config")
    def get_config():
        return store.public()

    @app.put("/api/config/interval")
    def set_interval(body: Interval):
        mins = max(0, min(body.minutes, 7 * 24 * 60))
        store.mutate(lambda d: d.__setitem__("sync_interval_minutes", mins))
        return {"sync_interval_minutes": mins}

    @app.post("/api/jellyfin/connect")
    def jellyfin_connect(body: JellyfinConnect):
        """Test the server + credentials and, if they work, save them."""
        url = jellyfin.normalize_url(body.url)
        try:
            info = jellyfin.server_info(url)
            if body.api_key:
                token, user_id = body.api_key.strip(), None
            elif body.username:
                auth = jellyfin.authenticate(url, body.username, body.password)
                token, user_id = auth["token"], auth["user_id"]
            else:
                raise HTTPException(400, "Provide an API key or a username and password")
            with httpx.Client(timeout=15) as c:  # prove the token can read libraries
                jellyfin._folders(c, url, jellyfin.auth_headers(token), user_id)
        except HTTPException:
            raise
        except PermissionError as e:
            raise HTTPException(401, str(e))
        except (httpx.HTTPError, ValueError) as e:
            raise HTTPException(502, f"Could not reach a Jellyfin server at {url}: {e}")
        cfg = {"url": url, "token": token, "user_id": user_id, "name": info["name"]}
        store.mutate(lambda d: d.__setitem__("jellyfin", cfg))
        return {"url": url, "name": info["name"], "version": info["version"]}

    @app.delete("/api/jellyfin")
    def jellyfin_disconnect():
        store.mutate(lambda d: d.__setitem__("jellyfin", None))
        return {"ok": True}

    @app.post("/api/sources")
    def add_source(body: NewSource):
        if body.type == "smb":
            if not (body.host and body.share):
                raise HTTPException(400, "host and share are required")
            src = make_source("smb", host=body.host, share=body.share, path=body.path,
                              username=body.username, password=body.password,
                              domain=body.domain, label=body.label, mode=body.mode, hint=body.hint)
        elif body.type == "local":
            if not os.path.isdir(body.path):
                raise HTTPException(400, f"Folder not found inside the container: {body.path}")
            src = make_source("local", path=body.path, label=body.label, mode=body.mode, hint=body.hint)
        else:
            raise HTTPException(400, "type must be 'local' or 'smb'")

        def add(d):
            labels = {s["label"] for s in d["sources"]}
            base, n = src["label"], 2
            while src["label"] in labels or src["label"] == "jellyfin":
                src["label"] = f"{base} ({n})"
                n += 1
            d["sources"].append(src)
        store.mutate(add)
        return {"id": src["id"], "label": src["label"]}

    @app.delete("/api/sources/{source_id}")
    def remove_source(source_id: str):
        store.mutate(lambda d: d.__setitem__(
            "sources", [s for s in d["sources"] if s["id"] != source_id]))
        return {"ok": True}

    # ---------- discovery & browsing ----------
    @app.get("/api/discover/subnets")
    def subnets():
        return {"subnets": discovery.local_subnets()}

    @app.get("/api/discover/jellyfin")
    def discover_jellyfin(cidr: str = ""):
        """UDP broadcast always; additionally scan a subnet if one is given."""
        found = {s["url"]: s for s in discovery.discover_jellyfin_udp()}
        if cidr:
            try:
                for s in discovery.discover_jellyfin_scan(cidr):
                    found.setdefault(s["url"], s)
            except ValueError as e:
                raise HTTPException(400, str(e))
        return {"servers": list(found.values())}

    @app.get("/api/discover/smb")
    def discover_smb(cidr: str):
        try:
            return {"hosts": discovery.discover_smb(cidr)}
        except ValueError as e:
            raise HTTPException(400, str(e))

    def smb_call(creds: SmbCreds, fn):
        try:
            c = smb.connect(creds.host, creds.username, creds.password, creds.domain)
        except Exception as e:
            raise HTTPException(502, f"Could not connect to {creds.host}: {e}")
        try:
            return fn(c)
        except Exception as e:
            raise HTTPException(502, f"SMB error: {e}")
        finally:
            c.close()

    @app.post("/api/smb/shares")
    def smb_shares(body: SmbCreds):
        return {"shares": smb_call(body, smb.list_shares)}

    @app.post("/api/smb/browse")
    def smb_browse(body: SmbBrowse):
        path = "/" + body.path.strip("/")
        dirs = smb_call(body, lambda c: smb.list_dirs(c, body.share, path))
        parent = None if path == "/" else "/" + "/".join(path.strip("/").split("/")[:-1])
        return {"path": path, "parent": parent, "dirs": dirs}

    @app.get("/api/fs/browse")
    def fs_browse(path: str = ""):
        """Browse folders visible inside the container (e.g. a mounted NAS share)."""
        if not path:
            path = "/media" if os.path.isdir("/media") else os.path.abspath(os.sep)
        path = os.path.abspath(path)
        try:
            dirs = sorted(d for d in os.listdir(path)
                          if os.path.isdir(os.path.join(path, d)) and not folder.skip_dir(d, top=True))
        except OSError as e:
            raise HTTPException(400, f"Cannot read {path}: {e.strerror or e}")
        parent = os.path.dirname(path)
        return {"path": path.replace(os.sep, "/"), "parent": parent if parent != path else None,
                "dirs": dirs}

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


def app_factory() -> FastAPI:
    return create_app()
