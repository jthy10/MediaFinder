"""Identify titles from file paths (local folders or SMB shares).

Like Jellyfin, identification relies on naming conventions such as
``Movie Name (2010)/Movie Name (2010).mkv`` and ``Show/Season 01/S01E02.mkv``.
We use `guessit`, which understands those and many messier release-style names.

A source has a *mode*:
  root     the folder contains library folders (``Movies/``, ``TV/`` ...); each becomes a library
  library  the folder itself is one library (e.g. you picked ``/volume1/Movies``)
and an optional *hint* (auto | movie | episode) to force how files are interpreted.
"""
from __future__ import annotations

import os
from typing import Iterable

from guessit import guessit

from ..db import Item

VIDEO_EXT = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".ts", ".mpg", ".mpeg",
             ".webm", ".flv", ".iso", ".m2ts"}
NESTED_SKIP = {"extras", "trailers", "featurettes", "behind the scenes", "deleted scenes",
               "interviews", "samples", "sample"}
ALWAYS_SKIP = {"#recycle", "$recycle.bin", "system volume information", "lost+found"}


def skip_dir(name: str, top: bool = False) -> bool:
    n = name.lower()
    if n.startswith(("@", ".")) or n in ALWAYS_SKIP:  # Synology @eaDir, hidden dirs, recycle bins
        return True
    return (not top) and n in NESTED_SKIP


def _is_season_dir(name: str) -> bool:
    n = name.lower()
    return n.startswith(("season", "specials", "series ")) or (n[:1] == "s" and n[1:].isdigit())


def items_from_files(source: str, root_name: str, root_display: str, files: Iterable[str],
                     mode: str = "root", hint: str = "auto") -> list[Item]:
    """files: paths relative to the root, '/'-separated."""
    items: dict[tuple, Item] = {}
    opts = {"type": hint} if hint in ("movie", "episode") else {}
    root_display = root_display.rstrip("/")
    for rel in files:
        parts = [p for p in rel.split("/") if p]
        if not parts or parts[-1].startswith("._"):
            continue
        if os.path.splitext(parts[-1])[1].lower() not in VIDEO_EXT:
            continue
        nested = mode == "root" and len(parts) > 1
        library, sub = (parts[0], parts[1:]) if nested else (root_name, parts)
        g = guessit("/".join(sub), opts)
        title = g.get("title")
        if not title:
            continue
        series = g.get("type") == "episode"
        key = (library, series, str(title).lower(), g.get("year"))
        if key in items:
            continue
        if series:
            dirs = parts[:-1]
            floor = 1 if nested else 0   # never pop the library folder itself
            while len(dirs) > floor and _is_season_dir(dirs[-1]):
                dirs.pop()
            path = "/".join([root_display, *dirs])
        else:
            path = "/".join([root_display, *parts])
        items[key] = Item(source=source, library=library,
                          type="series" if series else "movie",
                          title=str(title), year=g.get("year"), path=path)
    return list(items.values())


def walk_local(root: str) -> Iterable[str]:
    if not os.path.isdir(root):
        raise FileNotFoundError(f"Folder not found: {root}")
    for dirpath, dirs, files in os.walk(root, followlinks=True):
        dirs[:] = [d for d in dirs if not skip_dir(d)]
        rel_dir = os.path.relpath(dirpath, root)
        for f in files:
            yield f if rel_dir == "." else "/".join([*rel_dir.split(os.sep), f])


def scan_source(src: dict) -> list[Item]:
    root = os.path.abspath(src["path"])
    root_name = os.path.basename(root.rstrip("/\\")) or root
    return items_from_files(src["label"], root_name, root.replace(os.sep, "/"),
                            walk_local(root), src.get("mode", "root"), src.get("hint", "auto"))
