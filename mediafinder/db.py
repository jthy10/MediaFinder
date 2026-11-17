"""SQLite storage with FTS5 search, incremental updates and schema migrations."""
from __future__ import annotations

import os
import re
import sqlite3
import time
from dataclasses import dataclass
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


def replace_source(conn: sqlite3.Connection, source: str, items: Iterable[Item]) -> dict:
    """Bring one source up to date, touching only what changed (the FTS triggers keep the
    search index in step). Returns counts of added / updated / removed / total."""
    new: dict[str, Item] = {}
    for it in items:
        k, n = it.key, 2
        while k in new:             # two items with an identical key: keep both
            k, n = f"{it.key}#{n}", n + 1
        new[k] = it
    old = {r["key"]: r for r in conn.execute(
        "SELECT id, key, library, type, title, year, path, overview, genres FROM items WHERE source = ?",
        (source,))}
    added = updated = 0
    with conn:
        for k, it in new.items():
            row = old.get(k)
            if row is None:
                conn.execute(
                    "INSERT INTO items (source, library, type, title, year, path, overview, genres, key)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (it.source, it.library, it.type, it.title, it.year, it.path, it.overview, it.genres, k))
                added += 1
            elif (row["library"], row["type"], row["title"], row["year"], row["overview"], row["genres"]) != (
                    it.library, it.type, it.title, it.year, it.overview, it.genres):
                conn.execute(
                    "UPDATE items SET library=?, type=?, title=?, year=?, overview=?, genres=? WHERE id=?",
                    (it.library, it.type, it.title, it.year, it.overview, it.genres, row["id"]))
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
