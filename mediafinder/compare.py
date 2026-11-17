"""Compare what's in one set of sources against another (e.g. NAS files vs. Jellyfin).

Items are matched by external id first (TMDB / IMDb / TVDB, from `[tvdbid-123]`-style tags,
Jellyfin's provider ids or TMDB lookups) and otherwise by normalised title + year.
"""
from __future__ import annotations

import re
import unicodedata
from collections import defaultdict

ID_KINDS = ("tmdb_id", "imdb_id", "tvdb_id")


def norm_title(title: str) -> str:
    s = unicodedata.normalize("NFKD", title or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    s = re.sub(r"^(the|a|an) ", "", s)
    return s.replace(" ", "")


def _years_compatible(a, b) -> bool:
    return a is None or b is None or abs(int(a) - int(b)) <= 1


def _id_relation(a: dict, b: dict) -> str:
    """'same' if any external id matches, 'different' if ids of one kind clash, else 'unknown'."""
    clash = False
    for k in ID_KINDS:
        x, y = a.get(k), b.get(k)
        if x and y:
            if str(x).lower() == str(y).lower():
                return "same"
            clash = True
    return "different" if clash else "unknown"


def compare(side_a: list[dict], side_b: list[dict]) -> dict:
    """Return items only in A, only in B, and how many matched (and how: id vs title)."""
    by_id: dict[tuple, list[int]] = defaultdict(list)
    by_title: dict[tuple, list[int]] = defaultdict(list)
    for i, it in enumerate(side_b):
        for k in ID_KINDS:
            if it.get(k):
                by_id[(it["type"], k, str(it[k]).lower())].append(i)
        by_title[(it["type"], norm_title(it["title"]))].append(i)

    matched_b: set[int] = set()
    only_a, by_id_count, by_title_count = [], 0, 0
    for it in side_a:
        found = None
        for k in ID_KINDS:                                   # 1. exact id match
            if it.get(k):
                for j in by_id.get((it["type"], k, str(it[k]).lower()), []):
                    found = j
                    break
            if found is not None:
                break
        if found is not None:
            by_id_count += 1
        else:                                                # 2. title + year (unless ids disagree)
            for j in by_title.get((it["type"], norm_title(it["title"])), []):
                cand = side_b[j]
                if _id_relation(it, cand) == "different":
                    continue
                if _years_compatible(it.get("year"), cand.get("year")):
                    found = j
                    by_title_count += 1
                    break
        if found is None:
            only_a.append(it)
        else:
            matched_b.add(found)
    only_b = [it for j, it in enumerate(side_b) if j not in matched_b]
    return {"only_a": only_a, "only_b": only_b, "both": len(side_a) - len(only_a),
            "matched_by_id": by_id_count, "matched_by_title": by_title_count}
