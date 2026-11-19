"""Fuzzy matching of Figma layer names to source files.

Figma names a dropped video layer after the file it came from, usually minus
the extension, and people rename things ("hero_final 2", "Hero Final (1)").
We normalise both sides, score them, and only treat a candidate as *confirmed*
once its SHA-1 matches the layer's videoHash (see hashing.py).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv", ".mpg", ".mpeg", ".wmv", ".gif"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".tif", ".tiff", ".heic", ".bmp"}
MEDIA_EXTS = VIDEO_EXTS | IMAGE_EXTS

# Suffixes added by copying in Finder/Explorer/Figma that never exist on the source.
_COPY_SUFFIXES = [
    re.compile(r"\s*\(\d+\)$"),          # "name (1)"
    re.compile(r"\s+copy(\s+\d+)?$"),    # "name copy", "name copy 2"
    re.compile(r"\s+-\s+copy$"),         # "name - Copy"
    re.compile(r"\s+\d+$"),              # "name 2"  (space before number only: "city01" survives)
]
_WORD = re.compile(r"[a-z]+|\d+")


def strip_ext(name: str) -> str:
    base, ext = os.path.splitext(name.strip())
    return base if ext.lower() in MEDIA_EXTS else name.strip()


def normalize(name: str) -> str:
    """Collapse a name to lowercase alphanumerics with copy suffixes removed."""
    s = strip_ext(os.path.basename(name.replace("\\", "/"))).lower()
    changed = True
    while changed:
        changed = False
        for rx in _COPY_SUFFIXES:
            t = rx.sub("", s)
            if t != s and t:
                s, changed = t, True
    return re.sub(r"[^a-z0-9]+", "", s)


def tokens(name: str) -> list[str]:
    s = strip_ext(os.path.basename(name.replace("\\", "/")))
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", s)  # camelCase -> camel Case
    return _WORD.findall(s.lower())


def search_queries(layer_name: str) -> list[str]:
    """Queries to send to a search API, most specific first (deduplicated)."""
    base = strip_ext(layer_name)
    for rx in _COPY_SUFFIXES:
        base = rx.sub("", base) or base
    toks = tokens(base)
    out = [base.strip()]
    spaced = " ".join(toks)
    if spaced:
        out.append(spaced)
    long_toks = sorted((t for t in toks if len(t) >= 4 and not t.isdigit()), key=len, reverse=True)
    if len(long_toks) >= 2:
        out.append(" ".join(long_toks[:2]))
    if long_toks:
        out.append(long_toks[0])
    seen, res = set(), []
    for q in out:
        k = q.lower()
        if q and k not in seen:
            seen.add(k)
            res.append(q)
    return res


def score(layer_name: str, file_name: str) -> float:
    """0..1 similarity between a layer name and a file name."""
    a, b = normalize(layer_name), normalize(file_name)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ratio = SequenceMatcher(None, a, b).ratio()
    ta, tb = set(tokens(layer_name)), set(tokens(file_name))
    jac = len(ta & tb) / len(ta | tb) if ta and tb else 0.0
    contain = 0.0
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) >= 6 and short in long_:
        contain = 0.75 + 0.2 * (len(short) / len(long_))
    return round(max(contain, 0.6 * ratio + 0.4 * jac), 4)


@dataclass
class Candidate:
    name: str
    path: str                     # display path (Dropbox path or local path)
    size: int = 0
    source: str = "dropbox"       # "dropbox" | "local"
    ref: dict = field(default_factory=dict)  # source-specific handle (id, namespace, local path...)
    online_only: bool = False
    score: float = 0.0


def rank(layer_name: str, candidates: list[Candidate], min_score: float = 0.55,
         limit: int = 8) -> list[Candidate]:
    """Score, filter and order candidates. Locally available, smaller files are
    hashed first because they're cheapest to check."""
    best: dict[str, Candidate] = {}
    for c in candidates:
        c.score = score(layer_name, c.name)
        if c.score < min_score or os.path.splitext(c.name)[1].lower() not in VIDEO_EXTS:
            continue
        key = c.path.lower()
        if key not in best or (best[key].online_only and not c.online_only):
            best[key] = c
    ordered = sorted(best.values(), key=lambda c: (-round(c.score, 2), c.online_only, c.size or 1 << 62))
    return ordered[:limit]
