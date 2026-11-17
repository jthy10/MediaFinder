"""SQLite storage with FTS5 search."""
from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass
from typing import Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,          -- 'jellyfin' | 'folder'
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


def connect(path: str) -> sqlite3.Connection:
    if path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def replace_source(conn: sqlite3.Connection, source: str, items: Iterable[Item]) -> int:
    """Swap out everything from one source atomically, then rebuild the index."""
    rows = [
        (i.source, i.library, i.type, i.title, i.year, i.path, i.overview, i.genres)
        for i in items
    ]
    with conn:
        conn.execute("DELETE FROM items WHERE source = ?", (source,))
        conn.executemany(
            "INSERT INTO items (source, library, type, title, year, path, overview, genres)"
            " VALUES (?,?,?,?,?,?,?,?)",
            rows,
        )
        conn.execute("INSERT INTO items_fts(items_fts) VALUES('rebuild')")
    return len(rows)


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
