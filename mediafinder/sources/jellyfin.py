"""Read libraries from a Jellyfin server via its REST API."""
from __future__ import annotations

import httpx

from ..db import Item

TYPE_MAP = {"Movie": "movie", "Series": "series", "MusicAlbum": "album"}
PAGE = 500
CLIENT_INFO = 'Client="MediaFinder", Device="MediaFinder", DeviceId="mediafinder", Version="0.2"'


def auth_headers(token: str | None = None) -> dict:
    """Standard `Authorization: MediaBrowser ...` header (the legacy X-Emby-* headers are
    removed in newer Jellyfin releases)."""
    return {"Authorization": "MediaBrowser " + (f'Token="{token}", ' if token else "") + CLIENT_INFO}


def normalize_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    return url


def server_info(url: str, client: httpx.Client | None = None) -> dict:
    """Public, unauthenticated server info (also proves the URL is a Jellyfin server)."""
    own = client is None
    client = client or httpx.Client(timeout=10)
    try:
        r = client.get(f"{url}/System/Info/Public")
        r.raise_for_status()
        d = r.json()
        if "ServerName" not in d:
            raise ValueError("Not a Jellyfin server")
        return {"name": d["ServerName"], "version": d.get("Version"), "id": d.get("Id")}
    finally:
        if own:
            client.close()


def authenticate(url: str, username: str, password: str,
                 client: httpx.Client | None = None) -> dict:
    """Log in with a Jellyfin account; returns an access token we store instead of the password."""
    own = client is None
    client = client or httpx.Client(timeout=15)
    try:
        r = client.post(f"{url}/Users/AuthenticateByName",
                        headers=auth_headers(),
                        json={"Username": username, "Pw": password})
        if r.status_code == 401:
            raise PermissionError("Wrong Jellyfin username or password")
        r.raise_for_status()
        d = r.json()
        return {"token": d["AccessToken"], "user_id": d["User"]["Id"]}
    finally:
        if own:
            client.close()


def _folders(client, url, headers, user_id) -> list[dict]:
    r = client.get(f"{url}/Library/VirtualFolders", headers=headers)
    if r.status_code in (401, 403) and user_id:  # non-admin account: use its own library views
        r = client.get(f"{url}/UserViews", headers=headers, params={"userId": user_id})
        r.raise_for_status()
        return [{"Name": v["Name"], "ItemId": v["Id"]} for v in r.json().get("Items", [])]
    if r.status_code in (401, 403):
        raise PermissionError("Jellyfin rejected the API key / token")
    r.raise_for_status()
    return r.json()


def fetch_items(url: str, token: str, user_id: str | None = None,
                client: httpx.Client | None = None, progress=None) -> list[Item]:
    own = client is None
    client = client or httpx.Client(timeout=30)
    headers = auth_headers(token)
    try:
        items: list[Item] = []
        for folder in _folders(client, url, headers, user_id):
            name, parent = folder.get("Name", "Unknown"), folder.get("ItemId")
            if not parent:
                continue
            start = 0
            while True:
                params = {
                    "ParentId": parent, "Recursive": "true",
                    "IncludeItemTypes": ",".join(TYPE_MAP),
                    "Fields": "Path,Overview,Genres,ProductionYear",
                    "StartIndex": start, "Limit": PAGE,
                }
                if user_id:
                    params["UserId"] = user_id
                resp = client.get(f"{url}/Items", headers=headers, params=params)
                resp.raise_for_status()
                data = resp.json()
                batch = data.get("Items", [])
                for it in batch:
                    items.append(Item(
                        source="jellyfin", library=name,
                        type=TYPE_MAP.get(it.get("Type"), "other"),
                        title=it.get("Name") or "Untitled",
                        year=it.get("ProductionYear"), path=it.get("Path"),
                        overview=it.get("Overview"),
                        genres=", ".join(it.get("Genres") or []) or None,
                    ))
                start += len(batch)
                if progress:
                    progress(len(items))
                if not batch or start >= data.get("TotalRecordCount", 0):
                    break
        return items
    finally:
        if own:
            client.close()
