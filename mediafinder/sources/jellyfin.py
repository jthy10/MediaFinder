"""Read libraries from a Jellyfin server via its REST API."""
from __future__ import annotations

import httpx

from ..db import Item

TYPE_MAP = {"Movie": "movie", "Series": "series", "MusicAlbum": "album"}
PAGE = 500


def fetch_items(url: str, api_key: str, client: httpx.Client | None = None) -> list[Item]:
    own = client is None
    client = client or httpx.Client(timeout=30)
    headers = {"X-Emby-Token": api_key}
    try:
        folders = client.get(f"{url}/Library/VirtualFolders", headers=headers)
        folders.raise_for_status()
        items: list[Item] = []
        for folder in folders.json():
            name, parent = folder.get("Name", "Unknown"), folder.get("ItemId")
            if not parent:
                continue
            start = 0
            while True:
                resp = client.get(
                    f"{url}/Items",
                    headers=headers,
                    params={
                        "ParentId": parent,
                        "Recursive": "true",
                        "IncludeItemTypes": ",".join(TYPE_MAP),
                        "Fields": "Path,Overview,Genres,ProductionYear",
                        "StartIndex": start,
                        "Limit": PAGE,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                batch = data.get("Items", [])
                for it in batch:
                    items.append(Item(
                        source="jellyfin",
                        library=name,
                        type=TYPE_MAP.get(it.get("Type"), "other"),
                        title=it.get("Name") or "Untitled",
                        year=it.get("ProductionYear"),
                        path=it.get("Path"),
                        overview=it.get("Overview"),
                        genres=", ".join(it.get("Genres") or []) or None,
                    ))
                start += len(batch)
                if not batch or start >= data.get("TotalRecordCount", 0):
                    break
        return items
    finally:
        if own:
            client.close()
