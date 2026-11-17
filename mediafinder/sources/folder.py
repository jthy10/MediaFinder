"""Scan media folders (e.g. a NAS mount) and identify titles from file names.

Like Jellyfin, identification relies on naming conventions such as
``Movie Name (2010)/Movie Name (2010).mkv`` and ``Show/Season 01/S01E02.mkv``.
We use `guessit`, which understands those and many messier release-style names.
"""
from __future__ import annotations

import os

from guessit import guessit

from ..db import Item

VIDEO_EXT = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".ts", ".mpg", ".mpeg",
             ".webm", ".flv", ".iso", ".m2ts"}


def _identify(rel_path: str) -> dict:
    # Passing the relative path lets guessit use parent folders (show/season names).
    return dict(guessit(rel_path))


def scan_root(root: str) -> list[Item]:
    items: dict[tuple, Item] = {}
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        raise FileNotFoundError(f"Media path not found: {root}")
    for dirpath, _dirs, files in os.walk(root, followlinks=True):
        rel_dir = os.path.relpath(dirpath, root)
        library = root.rstrip("/\\").split(os.sep)[-1] if rel_dir == "." else rel_dir.split(os.sep)[0]
        for name in files:
            if os.path.splitext(name)[1].lower() not in VIDEO_EXT:
                continue
            rel = os.path.normpath(os.path.join(rel_dir, name)) if rel_dir != "." else name
            if rel_dir != "." and rel.count(os.sep) >= 1:
                guess_input = rel.split(os.sep, 1)[1]  # drop the library folder name
            else:
                guess_input = rel
            g = _identify(guess_input.replace(os.sep, "/"))
            title = g.get("title")
            if not title:
                continue
            is_series = g.get("type") == "episode"
            key = (library, "series" if is_series else "movie", str(title).lower(), g.get("year"))
            if key in items:
                continue
            show_dir = dirpath if not is_series else _show_dir(dirpath, root)
            items[key] = Item(
                source="folder",
                library=library,
                type="series" if is_series else "movie",
                title=str(title),
                year=g.get("year"),
                path=show_dir if is_series else os.path.join(dirpath, name),
            )
    return list(items.values())


def _show_dir(dirpath: str, root: str) -> str:
    """Walk up from an episode's folder, past 'Season N' folders, to the show folder."""
    cur = dirpath
    while cur != root and os.path.basename(cur).lower().startswith(("season", "specials", "s0", "s1")):
        cur = os.path.dirname(cur)
    return cur


def scan_paths(paths: list[str]) -> list[Item]:
    """Each top-level subfolder of a path is a library; loose files go in the root's own library."""
    out: list[Item] = []
    for p in paths:
        out.extend(scan_root(p))
    return out
