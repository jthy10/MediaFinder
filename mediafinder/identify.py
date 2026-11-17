"""Identify titles against TMDB (https://www.themoviedb.org/).

Folder/NAS items are named by humans, so file-name parsing gets some wrong (``scream 2022.mkv``,
a movie mistaken for a series...). With a free TMDB API key we look each one up, preferring
exact ids found in the name (``[tmdbid-603]``, ``{imdb-tt0133093}``, ``[tvdbid-81189]``) and
falling back to a title + year search. A match replaces the title/year with TMDB's canonical
ones and records the TMDB / IMDb / TVDB ids, which makes comparing against Jellyfin exact.
Responses are cached in the database so repeated syncs cost nothing.
"""
from __future__ import annotations

import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from typing import Callable

import httpx

from .compare import norm_title

BASE = "https://api.themoviedb.org/3"
CACHE_TTL = 30 * 86400
MIN_RATIO = 0.86          # title similarity needed to accept a search result
MIN_RATIO_OTHER = 0.92    # stricter when we had to guess the other media type


class TmdbError(Exception):
    pass


class TmdbAuthError(TmdbError):
    pass


class Tmdb:
    def __init__(self, api_key: str, db_path: str, client: httpx.Client | None = None):
        self.key, self.db_path = api_key.strip(), db_path
        self.client = client or httpx.Client(timeout=15)
        self._bearer = self.key.startswith("eyJ")   # v4 "read access token" vs v3 api key

    # -- http + cache ---------------------------------------------------
    def _request(self, path: str, params: dict) -> dict | None:
        headers = {"Authorization": f"Bearer {self.key}"} if self._bearer else {}
        q = dict(params) if self._bearer else {**params, "api_key": self.key}
        for attempt in range(4):
            r = self.client.get(BASE + path, params=q, headers=headers)
            if r.status_code == 401:
                raise TmdbAuthError("TMDB rejected the API key")
            if r.status_code == 404:
                return None
            if r.status_code == 429:
                time.sleep(min(float(r.headers.get("Retry-After", 2)), 10))
                continue
            r.raise_for_status()
            return r.json()
        raise TmdbError("TMDB is rate limiting requests; try again later")

    def get(self, path: str, **params) -> dict | None:
        ck = path + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))
        con = sqlite3.connect(self.db_path, timeout=30)
        try:
            row = con.execute("SELECT body, fetched FROM tmdb_cache WHERE cache_key = ?", (ck,)).fetchone()
            if row and time.time() - row[1] < CACHE_TTL:
                return json.loads(row[0])
            data = self._request(path, params)
            with con:
                con.execute("INSERT OR REPLACE INTO tmdb_cache (cache_key, body, fetched) VALUES (?,?,?)",
                            (ck, json.dumps(data), time.time()))
            return data
        finally:
            con.close()

    # -- lookups --------------------------------------------------------
    def check_key(self) -> None:
        if self._request("/configuration", {}) is None:
            raise TmdbError("Unexpected response from TMDB")

    def details(self, kind: str, tmdb_id) -> dict | None:
        d = self.get(f"/{kind}/{tmdb_id}", append_to_response="external_ids")
        if not d:
            return None
        ext = d.get("external_ids") or {}
        date = d.get("release_date") if kind == "movie" else d.get("first_air_date")
        return {
            "title": (d.get("title") if kind == "movie" else d.get("name")) or None,
            "year": int(date[:4]) if date and date[:4].isdigit() else None,
            "type": "movie" if kind == "movie" else "series",
            "tmdb_id": str(d["id"]),
            "imdb_id": ext.get("imdb_id") or d.get("imdb_id") or None,
            "tvdb_id": str(ext["tvdb_id"]) if ext.get("tvdb_id") else None,
        }

    def _search(self, kind: str, title: str, year: int | None, ratio: float) -> dict | None:
        def run(with_year: bool):
            params = {"query": title, "include_adult": "false"}
            if with_year and year:
                params["year" if kind == "movie" else "first_air_date_year"] = year
            return (self.get(f"/search/{kind}", **params) or {}).get("results", [])

        results = run(True) or (run(False) if year else [])
        best = best_match(results, title, year, ratio)
        return self.details(kind, best["id"]) if best else None

    def lookup(self, row: dict, allow_swap: bool = True) -> dict | None:
        """row: type, title, year, tmdb_id, imdb_id, tvdb_id -> canonical details, or None."""
        kind = "movie" if row["type"] == "movie" else "tv"
        if row.get("tmdb_id"):
            return self.details(kind, row["tmdb_id"])
        for ext_id, source in ((row.get("imdb_id"), "imdb_id"), (row.get("tvdb_id"), "tvdb_id")):
            if not ext_id:
                continue
            found = self.get(f"/find/{ext_id}", external_source=source) or {}
            tv_first = source == "tvdb_id" or kind == "tv"        # TVDB ids are overwhelmingly series
            order = [("tv", "tv_results"), ("movie", "movie_results")]
            for k, bucket in (order if tv_first else order[::-1]):
                if found.get(bucket):
                    return self.details(k, found[bucket][0]["id"])
        if not row.get("title"):
            return None
        hit = self._search(kind, row["title"], row.get("year"), MIN_RATIO)
        if hit is None and allow_swap:          # maybe the file name was classified as the wrong type
            hit = self._search("tv" if kind == "movie" else "movie", row["title"], row.get("year"), MIN_RATIO_OTHER)
        return hit


def best_match(results: list[dict], title: str, year: int | None, min_ratio: float) -> dict | None:
    target = norm_title(title)
    best, best_score = None, 0.0
    for r in results[:8]:
        names = [r.get("title") or r.get("name") or "", r.get("original_title") or r.get("original_name") or ""]
        ratio = max(SequenceMatcher(None, target, norm_title(n)).ratio() for n in names if n) if any(names) else 0
        date = r.get("release_date") or r.get("first_air_date") or ""
        ry = int(date[:4]) if date[:4].isdigit() else None
        if year and ry and abs(year - ry) > 1:
            continue
        score = ratio + (0.05 if year and ry == year else 0)
        if ratio >= min_ratio and score > best_score:
            best, best_score = r, score
    return best


def identify_all(conn, tmdb: Tmdb, progress: Callable[[int, int], None] | None = None,
                 workers: int = 4) -> dict:
    """Identify every folder/NAS item that hasn't been looked up yet. Updates rows in place."""
    rows = [dict(r) for r in conn.execute(
        "SELECT id, type, title, year, tmdb_id, imdb_id, tvdb_id FROM items"
        " WHERE source != 'jellyfin' AND match_state = 'name'")]
    matched = unmatched = 0

    def work(row):
        try:
            return row, tmdb.lookup(row)
        except TmdbAuthError:
            raise
        except (TmdbError, httpx.HTTPError):
            return row, "error"       # transient; leave the item for next time

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for row, hit in ex.map(work, rows):
            done += 1
            if hit == "error":
                pass
            elif hit and hit["title"]:
                conn.execute(
                    "UPDATE items SET title=?, year=?, type=?, tmdb_id=?, imdb_id=?, tvdb_id=?, match_state='tmdb' WHERE id=?",
                    (hit["title"], hit["year"], hit["type"], hit["tmdb_id"],
                     hit["imdb_id"] or row["imdb_id"], hit["tvdb_id"] or row["tvdb_id"], row["id"]))
                matched += 1
            else:
                conn.execute("UPDATE items SET match_state='unmatched' WHERE id=?", (row["id"],))
                unmatched += 1
            conn.commit()   # don't hold the write lock: worker threads write the response cache
            if progress:
                progress(done, len(rows))
    conn.commit()
    return {"checked": len(rows), "matched": matched, "unmatched": unmatched}


def match_counts(conn) -> dict:
    c = {r["match_state"]: r["n"] for r in conn.execute(
        "SELECT match_state, COUNT(*) AS n FROM items WHERE source != 'jellyfin' GROUP BY match_state")}
    return {"identified": c.get("tmdb", 0), "edited": c.get("manual", 0),
            "unmatched": c.get("unmatched", 0), "pending": c.get("name", 0)}
