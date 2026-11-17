"""SQLite storage with FTS5 search, incremental updates and schema migrations."""
from __future__ import annotations

import os
import re
import sqlite3
import time
from dataclasses import dataclass, replace
from typing import Iterable

# v1 schema (kept as-is so existing databases upgrade cleanly)
BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,          -- 'jellyfin' | label of a folder/NAS source
    library TEXT NOT NULL,
    type TEXT NOT NULL,            -- movie | series | album | other
    title TEXT NOT NULL,
    year INTEGER,
    path TEXT,
    overview TEXT,
    genres TEXT
);
CREATE INDEX IF NOT EXISTS idx_items_source ON items(source);
CREATE INDEX IF NOT EXISTS idx_items_title ON items(title COLLATE NOCASE);
CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
    title, overview, genres, content='items', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);
"""

# Each migration runs once, in order. PRAGMA user_version records how many have run.
MIGRATIONS = [
    # 1 -> 2: stable per-item key, keep the FTS index in sync with triggers (incremental
    #         updates instead of full rebuilds), and remember the outcome of each sync.
    """
    ALTER TABLE items ADD COLUMN key TEXT;
    UPDATE items SET key = library || char(31) || COALESCE(path, title || char(31) || COALESCE(year, '') || char(31) || type);
    CREATE INDEX IF NOT EXISTS idx_items_source_key ON items(source, key);
    CREATE TRIGGER IF NOT EXISTS items_ai AFTER INSERT ON items BEGIN
        INSERT INTO items_fts(rowid, title, overview, genres) VALUES (new.id, new.title, new.overview, new.genres);
    END;
    CREATE TRIGGER IF NOT EXISTS items_ad AFTER DELETE ON items BEGIN
        INSERT INTO items_fts(items_fts, rowid, title, overview, genres) VALUES ('delete', old.id, old.title, old.overview, old.genres);
    END;
    CREATE TRIGGER IF NOT EXISTS items_au AFTER UPDATE ON items BEGIN
        INSERT INTO items_fts(items_fts, rowid, title, overview, genres) VALUES ('delete', old.id, old.title, old.overview, old.genres);
        INSERT INTO items_fts(rowid, title, overview, genres) VALUES (new.id, new.title, new.overview, new.genres);
    END;
    INSERT INTO items_fts(items_fts) VALUES ('rebuild');
    CREATE TABLE IF NOT EXISTS sync_log (
        source TEXT PRIMARY KEY, finished REAL, total INTEGER, added INTEGER,
        updated INTEGER, removed INTEGER, error TEXT
    );
    """,
    # 2 -> 3: external ids, how an item was identified, the as-scanned title (so identification
    #         and manual edits can be undone), user corrections and a TMDB response cache.
    """
    ALTER TABLE items ADD COLUMN tmdb_id TEXT;
    ALTER TABLE items ADD COLUMN imdb_id TEXT;
    ALTER TABLE items ADD COLUMN tvdb_id TEXT;
    ALTER TABLE items ADD COLUMN match_state TEXT NOT NULL DEFAULT 'name';
    ALTER TABLE items ADD COLUMN raw_title TEXT;
    ALTER TABLE items ADD COLUMN raw_year INTEGER;
    UPDATE items SET raw_title = title, raw_year = year;
    UPDATE items SET match_state = 'jellyfin' WHERE source = 'jellyfin';
    CREATE TABLE IF NOT EXISTS overrides (
        source TEXT NOT NULL, key TEXT NOT NULL, title TEXT, year INTEGER, type TEXT,
        tmdb_id TEXT, imdb_id TEXT, tvdb_id TEXT, ignored INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (source, key)
    );
    CREATE TABLE IF NOT EXISTS tmdb_cache (cache_key TEXT PRIMARY KEY, body TEXT, fetched REAL);
    """,
]


@dataclass
class Item:
    source: str
    library: str
    type: str
    title: str
    year: int | None = None
    path: str | None = None
    overview: str | None = None
    genres: str | None = None
    tmdb_id: str | None = None
    imdb_id: str | None = None
    tvdb_id: str | None = None
    match_state: str = "name"     # name (from file names) | tmdb | manual | unmatched | jellyfin
    raw_title: str | None = None  # as scanned, before TMDB / manual changes
    raw_year: int | None = None

    def __post_init__(self):
        if self.raw_title is None:
            self.raw_title, self.raw_year = self.title, self.year

    @property
    def key(self) -> str:
        """Identity of an item within its source, stable across re-scans."""
        return f"{self.library}\x1f" + (self.path or f"{self.title}\x1f{self.year or ''}\x1f{self.type}")


def connect(path: str) -> sqlite3.Connection:
    if path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")   # readers aren't blocked while a sync writes
        conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(BASE_SCHEMA)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for n, script in enumerate(MIGRATIONS[version:], start=version + 1):
        conn.executescript(script)
        conn.execute(f"PRAGMA user_version = {n}")
    conn.commit()
    return conn


FIELDS = ("library", "type", "title", "year", "overview", "genres", "tmdb_id", "imdb_id", "tvdb_id",
          "match_state", "raw_title", "raw_year")


def _apply_override(it: Item, ov: dict) -> Item:
    return replace(it, title=ov["title"] or it.title, year=ov["year"] if ov["year"] is not None else it.year,
                   type=ov["type"] or it.type, tmdb_id=ov["tmdb_id"] or it.tmdb_id,
                   imdb_id=ov["imdb_id"] or it.imdb_id, tvdb_id=ov["tvdb_id"] or it.tvdb_id,
                   match_state="manual")


def replace_source(conn: sqlite3.Connection, source: str, items: Iterable[Item]) -> dict:
    """Bring one source up to date, touching only what changed (the FTS triggers keep the
    search index in step). Returns counts of added / updated / removed / total.

    User corrections (the ``overrides`` table) are applied on the way in, and titles that were
    already identified via TMDB are left alone as long as the file name hasn't changed."""
    overrides = {r["key"]: dict(r) for r in conn.execute("SELECT * FROM overrides WHERE source = ?", (source,))}
    new: dict[str, Item] = {}
    for it in items:
        ov = overrides.get(it.key)
        if ov:
            if ov["ignored"]:
                continue
            it = _apply_override(it, ov)
        k, n = it.key, 2
        while k in new:             # two items with an identical key: keep both
            k, n = f"{it.key}#{n}", n + 1
        new[k] = it
    old = {r["key"]: r for r in conn.execute(
        f"SELECT id, key, {', '.join(FIELDS)} FROM items WHERE source = ?", (source,))}
    added = updated = 0
    cols = ", ".join(FIELDS)
    marks = ",".join("?" * len(FIELDS))
    with conn:
        for k, it in new.items():
            row = old.get(k)
            values = tuple(getattr(it, f) for f in FIELDS)
            if row is None:
                conn.execute(f"INSERT INTO items (source, path, key, {cols}) VALUES (?,?,?,{marks})",
                             (it.source, it.path, k, *values))
                added += 1
                continue
            if (row["match_state"] in ("tmdb", "unmatched") and it.match_state == "name"
                    and (row["raw_title"], row["raw_year"]) == (it.raw_title, it.raw_year)):
                continue            # already identified; the file name hasn't changed
            if tuple(row[f] for f in FIELDS) != values:
                sets = ", ".join(f"{f}=?" for f in FIELDS)
                conn.execute(f"UPDATE items SET {sets} WHERE id=?", (*values, row["id"]))
                updated += 1
        gone = [(r["id"],) for k, r in old.items() if k not in new]
        conn.executemany("DELETE FROM items WHERE id = ?", gone)
    return {"total": len(new), "added": added, "updated": updated, "removed": len(gone)}


def prune_sources(conn: sqlite3.Connection, keep: list[str]) -> None:
    """Forget sources that are no longer configured."""
    marks = ",".join("?" * len(keep))
    with conn:
        conn.execute(f"DELETE FROM items WHERE source NOT IN ({marks})" if keep else "DELETE FROM items", keep)
        conn.execute(f"DELETE FROM sync_log WHERE source NOT IN ({marks})" if keep else "DELETE FROM sync_log", keep)


def log_sync(conn: sqlite3.Connection, source: str, stats: dict | None, error: str | None) -> None:
    s = stats or {}
    with conn:
        conn.execute(
            "INSERT INTO sync_log (source, finished, total, added, updated, removed, error) VALUES (?,?,?,?,?,?,?)"
            " ON CONFLICT(source) DO UPDATE SET finished=excluded.finished, total=excluded.total,"
            " added=excluded.added, updated=excluded.updated, removed=excluded.removed, error=excluded.error",
            (source, time.time(), s.get("total"), s.get("added"), s.get("updated"), s.get("removed"), error))


def sync_log(conn: sqlite3.Connection) -> dict[str, dict]:
    return {r["source"]: dict(r) for r in conn.execute("SELECT * FROM sync_log")}


def _fts_query(q: str) -> str:
    terms = re.findall(r"\w+", q, flags=re.UNICODE)
    return " ".join(f'"{t}"*' for t in terms)


def _where(q, type_, library, source, year):
    clauses, params = [], []
    if q and (fq := _fts_query(q)):
        clauses.append("items.id IN (SELECT rowid FROM items_fts WHERE items_fts MATCH ?)")
        params.append(fq)
    for col, val in (("type", type_), ("library", library), ("source", source), ("year", year)):
        if val not in (None, ""):
            clauses.append(f"items.{col} = ?")
            params.append(val)
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def search(conn, q="", type_=None, library=None, source=None, year=None,
           limit=50, offset=0) -> tuple[int, list[dict]]:
    where, params = _where(q, type_, library, source, year)
    total = conn.execute(f"SELECT COUNT(*) FROM items{where}", params).fetchone()[0]
    sql = (f"SELECT * FROM items{where} ORDER BY title COLLATE NOCASE, year"
           " LIMIT ? OFFSET ?" if limit is not None else
           f"SELECT * FROM items{where} ORDER BY title COLLATE NOCASE, year")
    args = params + ([limit, offset] if limit is not None else [])
    return total, [dict(r) for r in conn.execute(sql, args)]


def libraries(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT source, library, type, COUNT(*) AS count FROM items"
        " GROUP BY source, library, type ORDER BY source, library")]


def stats(conn) -> dict:
    by_type = {r["type"]: r["count"] for r in conn.execute(
        "SELECT type, COUNT(*) AS count FROM items GROUP BY type")}
    by_source = {r["source"]: r["count"] for r in conn.execute(
        "SELECT source, COUNT(*) AS count FROM items GROUP BY source")}
    return {"total": sum(by_type.values()), "by_type": by_type, "by_source": by_source}
