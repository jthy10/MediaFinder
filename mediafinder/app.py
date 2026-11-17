"""FastAPI app: JSON API + static dashboard."""
from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import ipaddress
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import compare as cmp
from . import db, identify
from .config import ConfigStore, Settings, load_settings, make_source
from .security import MIN_PASSWORD, SESSION_DAYS, Auth
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


class TmdbKey(BaseModel):
    api_key: str


class ItemEdit(BaseModel):
    title: str | None = None
    year: int | None = None
    type: str | None = None
    tmdb_id: str | None = None
    imdb_id: str | None = None
    tvdb_id: str | None = None
    ignore: bool = False


class Login(BaseModel):
    password: str


class NewPassword(BaseModel):
    current: str = ""
    new: str = ""        # empty = remove the password


PUBLIC_API = {"/api/health", "/api/auth/status", "/api/auth/login", "/api/auth/logout"}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    store = ConfigStore(settings)
    sync = SyncManager(settings, store)
    auth = Auth(store, store.box, settings.admin_password)

    async def periodic():
        while True:
            await asyncio.sleep(30)
            mins = store.snapshot()["sync_interval_minutes"]
            last = sync.last_run or 0
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

    @app.middleware("http")
    async def require_login(request: Request, call_next):
        p = request.url.path
        if (p.startswith("/api/") and p not in PUBLIC_API and auth.required
                and not auth.valid_token(request.cookies.get("mf_session"))):
            return JSONResponse({"detail": "Not signed in"}, status_code=401)
        return await call_next(request)

    def conn():
        return db.connect(settings.db_path)

    def set_session(request: Request, response: Response):
        secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
        response.set_cookie("mf_session", auth.make_token(), max_age=SESSION_DAYS * 86400,
                            httponly=True, samesite="strict", secure=secure)

    # ---------- auth ----------
    @app.get("/api/health")
    def health():
        return {"ok": True}

    @app.get("/api/auth/status")
    def auth_status(request: Request):
        return {"required": auth.required,
                "authenticated": (not auth.required) or auth.valid_token(request.cookies.get("mf_session")),
                "locked_by_env": bool(settings.admin_password)}

    @app.post("/api/auth/login")
    def login(body: Login, request: Request, response: Response):
        client = request.client.host if request.client else "?"
        if auth.throttled(client):
            raise HTTPException(429, "Too many attempts - wait a minute and try again")
        if not auth.required or not auth.verify(body.password):
            auth.record_failure(client)
            raise HTTPException(401, "Wrong password")
        set_session(request, response)
        return {"ok": True}

    @app.post("/api/auth/logout")
    def logout(response: Response):
        response.delete_cookie("mf_session")
        return {"ok": True}

    @app.post("/api/auth/password")
    def change_password(body: NewPassword, request: Request, response: Response):
        if settings.admin_password:
            raise HTTPException(400, "The password is set by MEDIAFINDER_PASSWORD and can't be changed here")
        if auth.required and not auth.verify(body.current):
            raise HTTPException(401, "Current password is wrong")
        if body.new and len(body.new) < MIN_PASSWORD:
            raise HTTPException(400, f"Use at least {MIN_PASSWORD} characters")
        auth.set_password(body.new)
        if body.new:
            set_session(request, response)
        return {"required": bool(body.new)}

    # ---------- folder access scoping ----------
    def allowed_roots() -> list[str]:
        if "*" in settings.browse_roots:
            return ["*"]
        return [os.path.realpath(r) for r in settings.browse_roots if os.path.isdir(r)]

    def inside(path: str, roots: list[str]) -> bool:
        if roots == ["*"]:
            return True
        try:
            return any(os.path.commonpath([path, r]) == r for r in roots)
        except ValueError:  # different drives
            return False

    def check_path(path: str) -> str:
        """Resolve symlinks / '..' and make sure the path is inside an allowed root."""
        real = os.path.realpath(path)
        if inside(real, allowed_roots()):
            return real
        raise HTTPException(403, "That folder is outside the allowed locations "
                                 f"({', '.join(settings.browse_roots)}). Mount it there or set BROWSE_ROOTS.")

    def check_cidr(cidr: str) -> str:
        try:
            net = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            raise HTTPException(400, "Enter a range like 192.168.1.0/24")
        if not net.is_private:
            raise HTTPException(400, "Only private (LAN) ranges can be scanned")
        return cidr

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
            body = json.dumps([{k: i[k] for k in ("title", "year", "type", "library", "tmdb_id", "imdb_id", "tvdb_id")}
                               for i in items], indent=2, ensure_ascii=False)
            media = "application/json"
        elif format == "txt":
            body = "\n".join(f"{i['title']} ({i['year']})" if i["year"] else i["title"]
                             for i in items) + "\n"
            media = "text/plain; charset=utf-8"
        else:
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["title", "year", "type", "library", "source", "path", "tmdb_id", "imdb_id", "tvdb_id"])
            for i in items:
                w.writerow([i["title"], i["year"] or "", i["type"], i["library"],
                            i["source"], i["path"] or "", i["tmdb_id"] or "", i["imdb_id"] or "",
                            i["tvdb_id"] or ""])
            body, media = buf.getvalue(), "text/csv; charset=utf-8"
        return Response(body, media_type=media, headers={
            "Content-Disposition": f'attachment; filename="mediafinder.{format}"'})

    # ---------- compare ----------
    def resolve_side(spec: str, all_items: list[dict]) -> tuple[list[dict], str]:
        """spec: '@jellyfin', '@folders' (every NAS/folder source) or comma-separated source labels."""
        if spec == "@jellyfin":
            return [i for i in all_items if i["source"] == "jellyfin"], "Jellyfin"
        if spec == "@folders":
            return [i for i in all_items if i["source"] != "jellyfin"], "NAS / folders"
        labels = {s for s in spec.split(",") if s}
        return [i for i in all_items if i["source"] in labels], ", ".join(sorted(labels)) or "(none)"

    @app.get("/api/compare")
    def compare_sources(a: str = "@folders", b: str = "@jellyfin", type: str | None = None,
                        format: str = Query("json", pattern="^(json|csv)$")):
        """Titles present in A but not B, in B but not A, and how many match."""
        c = conn()
        try:
            _, everything = db.search(c, limit=None)
        finally:
            c.close()
        if type:
            everything = [i for i in everything if i["type"] == type]
        side_a, label_a = resolve_side(a or "@folders", everything)
        side_b, label_b = resolve_side(b or "@jellyfin", everything)
        r = cmp.compare(side_a, side_b)
        if format == "csv":
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["status", "title", "year", "type", "library", "source", "path", "tmdb_id", "imdb_id", "tvdb_id"])
            for status, rows in ((f"only in {label_a}", r["only_a"]), (f"only in {label_b}", r["only_b"])):
                for i in rows:
                    w.writerow([status, i["title"], i["year"] or "", i["type"], i["library"], i["source"],
                                i["path"] or "", i["tmdb_id"] or "", i["imdb_id"] or "", i["tvdb_id"] or ""])
            return Response(buf.getvalue(), media_type="text/csv; charset=utf-8", headers={
                "Content-Disposition": 'attachment; filename="mediafinder-compare.csv"'})
        return {"a_label": label_a, "b_label": label_b, "a_total": len(side_a), "b_total": len(side_b), **r}

    # ---------- sync ----------
    @app.post("/api/sync", status_code=202)
    async def start_sync():
        if sync.running:
            return {"started": False}
        sync.running = True   # visible to status polls immediately
        asyncio.create_task(asyncio.to_thread(sync.run))
        return {"started": True}

    @app.get("/api/sync/status")
    def sync_status():
        return sync.snapshot()

    # ---------- configuration ----------
    @app.get("/api/config")
    def get_config():
        c = conn()
        try:
            counts = identify.match_counts(c)
        finally:
            c.close()
        return {**store.public(), "identify": counts}

    @app.put("/api/config/interval")
    def set_interval(body: Interval):
        mins = max(0, min(body.minutes, 7 * 24 * 60))
        store.mutate(lambda d: d.__setitem__("sync_interval_minutes", mins))
        return {"sync_interval_minutes": mins}

    # ---------- TMDB ----------
    @app.post("/api/tmdb")
    def tmdb_connect(body: TmdbKey):
        key = body.api_key.strip()
        if not key:
            raise HTTPException(400, "Paste your TMDB API key")
        try:
            identify.Tmdb(key, settings.db_path).check_key()
        except identify.TmdbAuthError:
            raise HTTPException(401, "TMDB rejected that key")
        except (identify.TmdbError, httpx.HTTPError) as e:
            raise HTTPException(502, f"Could not reach TMDB: {e}")
        store.mutate(lambda d: d.__setitem__("tmdb", {"api_key": key}))
        return {"ok": True}

    @app.delete("/api/tmdb")
    def tmdb_disconnect():
        store.mutate(lambda d: d.__setitem__("tmdb", None))
        return {"ok": True}

    @app.post("/api/identify/retry")
    def identify_retry():
        """Give items TMDB couldn't match another chance on the next sync."""
        c = conn()
        try:
            with c:
                n = c.execute("UPDATE items SET match_state='name' WHERE match_state='unmatched'").rowcount
        finally:
            c.close()
        return {"requeued": n}

    # ---------- manual corrections ----------
    def base_key(key: str) -> str:
        return re.sub(r"#\d+$", "", key)   # strip the suffix we add to duplicate keys

    def reset_item(c, source: str, key: str) -> None:
        with c:
            c.execute("DELETE FROM overrides WHERE source=? AND key=?", (source, key))
            c.execute("UPDATE items SET title=raw_title, year=raw_year, match_state='name',"
                      " tmdb_id=NULL, imdb_id=NULL, tvdb_id=NULL WHERE source=?"
                      " AND (key=? OR substr(key, 1, length(?) + 1) = ? || '#')",
                      (source, key, key, key))

    @app.put("/api/items/{item_id}")
    def edit_item(item_id: int, body: ItemEdit):
        """Correct a title (or hide it). Kept across syncs. Not available for Jellyfin items."""
        c = conn()
        try:
            row = c.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if not row:
                raise HTTPException(404, "No such item")
            if row["source"] == "jellyfin":
                raise HTTPException(400, "Jellyfin items are identified by Jellyfin - fix them in Jellyfin")
            key = base_key(row["key"])
            if body.ignore:
                with c:
                    c.execute("INSERT OR REPLACE INTO overrides (source,key,title,year,type,ignored)"
                              " VALUES (?,?,?,?,?,1)", (row["source"], key, row["title"], row["year"], row["type"]))
                    c.execute("DELETE FROM items WHERE id=?", (item_id,))
                return {"ok": True, "hidden": True}
            title = (body.title or row["title"]).strip()
            if not title:
                raise HTTPException(400, "Title can't be empty")
            if body.type and body.type not in ("movie", "series", "album", "other"):
                raise HTTPException(400, "Unknown type")
            if body.year is not None and not 1870 <= body.year <= 2100:
                raise HTTPException(400, "Year looks wrong")
            vals = (title, body.year, body.type or row["type"], body.tmdb_id or None, body.imdb_id or None,
                    body.tvdb_id or None)
            with c:
                c.execute("INSERT OR REPLACE INTO overrides (source,key,title,year,type,tmdb_id,imdb_id,tvdb_id,ignored)"
                          " VALUES (?,?,?,?,?,?,?,?,0)", (row["source"], key, *vals))
                c.execute("UPDATE items SET title=?, year=?, type=?, tmdb_id=?, imdb_id=?, tvdb_id=?,"
                          " match_state='manual' WHERE id=?", (*vals, item_id))
            return {"ok": True}
        finally:
            c.close()

    @app.get("/api/overrides")
    def list_overrides():
        c = conn()
        try:
            rows = c.execute("SELECT rowid AS id, source, key, title, year, type, ignored FROM overrides"
                             " ORDER BY ignored DESC, title COLLATE NOCASE").fetchall()
        finally:
            c.close()
        return [{**dict(r), "path": r["key"].split("\x1f", 1)[-1]} for r in rows]

    @app.delete("/api/overrides/{override_id}")
    def delete_override(override_id: int):
        c = conn()
        try:
            r = c.execute("SELECT source, key FROM overrides WHERE rowid=?", (override_id,)).fetchone()
            if not r:
                raise HTTPException(404, "No such correction")
            reset_item(c, r["source"], r["key"])
        finally:
            c.close()
        return {"ok": True}

    @app.delete("/api/items/{item_id}/override")
    def reset_one(item_id: int):
        c = conn()
        try:
            row = c.execute("SELECT source, key FROM items WHERE id=?", (item_id,)).fetchone()
            if not row:
                raise HTTPException(404, "No such item")
            reset_item(c, row["source"], base_key(row["key"]))
        finally:
            c.close()
        return {"ok": True}

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
            real = check_path(body.path)
            if not os.path.isdir(real):
                raise HTTPException(400, f"Folder not found inside the container: {body.path}")
            src = make_source("local", path=real, label=body.label, mode=body.mode, hint=body.hint)
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
                for s in discovery.discover_jellyfin_scan(check_cidr(cidr)):
                    found.setdefault(s["url"], s)
            except ValueError as e:
                raise HTTPException(400, str(e))
        return {"servers": list(found.values())}

    @app.get("/api/discover/smb")
    def discover_smb(cidr: str):
        try:
            return {"hosts": discovery.discover_smb(check_cidr(cidr))}
        except ValueError as e:
            raise HTTPException(400, str(e))

    def smb_call(creds: SmbCreds, fn):
        try:
            session = smb.Session(creds.host, creds.username, creds.password, creds.domain)
        except Exception as e:
            raise HTTPException(502, f"Could not connect to {creds.host}: {e}")
        try:
            return fn(session)
        except Exception as e:
            raise HTTPException(502, f"SMB error: {e}")
        finally:
            session.close()

    @app.post("/api/smb/shares")
    def smb_shares(body: SmbCreds):
        smb_call(body, lambda s: None)   # proves the address and login work (SMB 2/3, signing, encryption)
        try:
            return {"shares": smb.list_shares(body.host, body.username, body.password, body.domain), "note": ""}
        except Exception:
            return {"shares": [], "note": "Connected, but this server doesn't list its shares automatically. "
                                         "Type the share name below."}

    @app.post("/api/smb/browse")
    def smb_browse(body: SmbBrowse):
        path = "/" + body.path.strip("/")
        dirs = smb_call(body, lambda s: s.list_dirs(body.share, path))
        parent = None if path == "/" else "/" + "/".join(path.strip("/").split("/")[:-1])
        join = lambda n: ("" if path == "/" else path) + "/" + n
        return {"path": path, "parent": parent, "dirs": [{"name": d, "path": join(d)} for d in dirs]}

    @app.get("/api/fs/browse")
    def fs_browse(path: str = ""):
        """Browse folders visible inside the container (e.g. a mounted NAS share)."""
        roots = allowed_roots()
        if not path:
            if roots == ["*"]:
                path = "/media" if os.path.isdir("/media") else os.path.abspath(os.sep)
            elif not roots:
                raise HTTPException(400, "No browsable folders exist. Mount your media at /media "
                                         "(or set BROWSE_ROOTS), or connect to your NAS over SMB instead.")
            elif len(roots) == 1:
                path = roots[0]
            else:  # several roots: show them as the top level
                return {"path": "", "parent": None,
                        "dirs": [{"name": r, "path": r.replace(os.sep, "/")} for r in roots]}
        real = check_path(path)
        try:
            names = sorted(d for d in os.listdir(real)
                           if os.path.isdir(os.path.join(real, d)) and not folder.skip_dir(d, top=True))
        except OSError as e:
            raise HTTPException(400, f"Cannot read {path}: {e.strerror or e}")
        norm = real.replace(os.sep, "/")
        up = os.path.dirname(real)
        if up == real:
            parent = None
        elif inside(up, roots):
            parent = up.replace(os.sep, "/")
        else:
            parent = "" if len(roots) > 1 else None   # "" = back to the list of roots
        return {"path": norm, "parent": parent,
                "dirs": [{"name": n, "path": f"{norm.rstrip('/')}/{n}"} for n in names]}

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


def app_factory() -> FastAPI:
    return create_app()
