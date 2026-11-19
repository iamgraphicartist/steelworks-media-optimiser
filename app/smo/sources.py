"""Where original videos come from: the Dropbox API (team space, shared folders,
files that are not synced) and, as a fast path, folders on this computer.

`find_source` ties it together: search every source by layer name, rank the
hits with fuzzy matching, then hash candidates (cheapest first) until one's
SHA-1 equals the layer's Figma videoHash.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional

import requests

from .hashing import HashingWriter, hash_matches, sha1_file
from .matching import VIDEO_EXTS, Candidate, normalize, rank, search_queries, score

Log = Callable[[str], None]
Progress = Callable[[float], None]

# Windows attributes that mean "the bytes are in the cloud, not on disk".
_ONLINE_ONLY = 0x400000 | 0x40000 | 0x1000


def is_online_only(path: str) -> bool:
    try:
        st = os.stat(path)
    except OSError:
        return True
    attrs = getattr(st, "st_file_attributes", 0)
    if attrs & _ONLINE_ONLY:
        return True
    if sys.platform == "darwin" and st.st_size > 0 and getattr(st, "st_blocks", 1) == 0:
        return True  # File Provider placeholder
    return False


class HashCache:
    """Remembers SHA-1s by (path, size, mtime) so repeat runs don't re-hash."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        try:
            with open(path, encoding="utf-8") as f:
                self._d = json.load(f)
        except (OSError, ValueError):
            self._d = {}

    def _key(self, p: str) -> Optional[str]:
        try:
            st = os.stat(p)
        except OSError:
            return None
        return f"{os.path.abspath(p)}|{st.st_size}|{int(st.st_mtime)}"

    def get(self, p: str) -> Optional[str]:
        k = self._key(p)
        return self._d.get(k) if k else None

    def put(self, p: str, sha: str) -> None:
        k = self._key(p)
        if not k:
            return
        with self._lock:
            self._d[k] = sha
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._d, f)
            os.replace(tmp, self.path)

    def sha1(self, p: str, progress=None) -> str:
        cached = self.get(p)
        if cached:
            return cached
        size = os.path.getsize(p) or 1
        h = sha1_file(p, (lambda n: progress(n / size)) if progress else None)
        self.put(p, h)
        return h


class Source:
    label = ""

    def search(self, layer_name: str) -> list[Candidate]:
        raise NotImplementedError

    def fetch(self, c: Candidate, workdir: str, progress: Optional[Progress] = None) -> tuple[str, str, bool]:
        """Return (local_path, sha1, is_temporary)."""
        raise NotImplementedError


class LocalFolderSource(Source):
    """A folder on this computer, e.g. the synced Steelworks Dropbox."""

    def __init__(self, root: str, cache: HashCache, label: str = "This computer"):
        self.root, self.cache, self.label = root, cache, label
        self._index: Optional[dict[str, list[str]]] = None
        self._lock = threading.Lock()

    def build_index(self) -> int:
        idx: dict[str, list[str]] = {}
        stack = [self.root]
        while stack:
            d = stack.pop()
            try:
                with os.scandir(d) as it:
                    for e in it:
                        try:
                            if e.is_dir(follow_symlinks=False):
                                if not e.name.startswith((".", "~")):
                                    stack.append(e.path)
                            elif os.path.splitext(e.name)[1].lower() in VIDEO_EXTS:
                                idx.setdefault(normalize(e.name), []).append(e.path)
                        except OSError:
                            continue
            except OSError:
                continue
        self._index = idx
        return sum(len(v) for v in idx.values())

    def search(self, layer_name: str) -> list[Candidate]:
        with self._lock:
            if self._index is None:
                self.build_index()
        assert self._index is not None
        k = normalize(layer_name)
        paths = list(self._index.get(k, []))
        if len(k) >= 4:
            for nk, ps in self._index.items():
                if nk != k and (k in nk or nk in k or score(layer_name, os.path.basename(ps[0])) >= 0.7):
                    paths.extend(ps)
        out = []
        for p in paths:
            try:
                size = os.path.getsize(p)
            except OSError:
                size = 0
            rel = os.path.relpath(p, self.root).replace(os.sep, "/")
            out.append(Candidate(os.path.basename(p), f"{self.label}/{rel}", size, "local", {"path": p}, is_online_only(p)))
        return out

    def fetch(self, c, workdir, progress=None):
        p = c.ref["path"]
        return p, self.cache.sha1(p, progress), False


# ---------------------------------------------------------------------------
# Dropbox API
# ---------------------------------------------------------------------------
API = "https://api.dropboxapi.com/2"
CONTENT = "https://content.dropboxapi.com/2"


class DropboxError(Exception):
    pass


def dropbox_desktop_root() -> Optional[str]:
    """Path of the Dropbox desktop app's business (team) folder, if installed."""
    cands = []
    if sys.platform == "win32":
        for env in ("APPDATA", "LOCALAPPDATA"):
            if os.environ.get(env):
                cands.append(os.path.join(os.environ[env], "Dropbox", "info.json"))
    else:
        cands.append(os.path.expanduser("~/.dropbox/info.json"))
    for p in cands:
        try:
            with open(p, encoding="utf-8") as f:
                info = json.load(f)
        except (OSError, ValueError):
            continue
        for k in ("business", "personal"):
            path = (info.get(k) or {}).get("path")
            if path and os.path.isdir(path):
                return path
    return None


@dataclass
class DropboxAccount:
    name: str
    email: str
    team: Optional[str]
    root_namespace_id: Optional[str]
    home_namespace_id: Optional[str]
    raw: dict = field(default_factory=dict)


class DropboxSource(Source):
    label = "Dropbox"

    def __init__(self, token_fn: Callable[[], Optional[str]], cache: HashCache,
                 local_root: Optional[str] = None, session: Optional[requests.Session] = None,
                 api: str = API, content: str = CONTENT):
        self.token_fn, self.cache, self.local_root = token_fn, cache, local_root
        self.http = session or requests.Session()
        self.api, self.content = api, content
        self.account: Optional[DropboxAccount] = None
        self._unmounted: Optional[list[dict]] = None

    # -- plumbing ---------------------------------------------------------------
    def _headers(self, ns: Optional[str] = None, root: bool = True) -> dict:
        tok = self.token_fn()
        if not tok:
            raise DropboxError("Not signed in to Dropbox.")
        h = {"Authorization": f"Bearer {tok}"}
        if ns:
            h["Dropbox-API-Path-Root"] = json.dumps({".tag": "namespace_id", "namespace_id": ns})
        elif root and self.account and self.account.root_namespace_id:
            # Team space: search from the team root so team folders are included.
            h["Dropbox-API-Path-Root"] = json.dumps({".tag": "root", "root": self.account.root_namespace_id})
        return h

    def _rpc(self, route: str, body: Optional[dict], ns: Optional[str] = None, root: bool = True) -> dict:
        h = self._headers(ns, root)
        if body is None:
            r = self.http.post(f"{self.api}/{route}", headers=h, timeout=60)
        else:
            r = self.http.post(f"{self.api}/{route}", headers=h, json=body, timeout=60)
        if r.status_code == 401:
            raise DropboxError("Dropbox sign-in has expired. Please sign in again.")
        if r.status_code >= 400:
            raise DropboxError(f"Dropbox said {r.status_code}: {r.text[:200]}")
        return r.json()

    def connect(self) -> DropboxAccount:
        j = self._rpc("users/get_current_account", None, root=False)
        ri = j.get("root_info") or {}
        team = (j.get("team") or {}).get("name")
        self.account = DropboxAccount(
            name=(j.get("name") or {}).get("display_name", ""), email=j.get("email", ""), team=team,
            root_namespace_id=ri.get("root_namespace_id"), home_namespace_id=ri.get("home_namespace_id"), raw=j)
        return self.account

    def unmounted_shared_folders(self) -> list[dict]:
        if self._unmounted is not None:
            return self._unmounted
        out, j = [], self._rpc("sharing/list_folders", {"limit": 1000})
        while True:
            out += [e for e in j.get("entries", []) if not e.get("path_lower")]
            if not j.get("cursor") or len(out) > 200:
                break
            j = self._rpc("sharing/list_folders/continue", {"cursor": j["cursor"]})
        self._unmounted = out
        return out

    def _search_ns(self, query: str, ns: Optional[str], folder_name: str = "") -> list[Candidate]:
        body = {"query": query, "options": {"path": "", "max_results": 100, "file_status": "active",
                                             "filename_only": True,
                                             "file_extensions": sorted(e.lstrip(".") for e in VIDEO_EXTS)}}
        res, j, pages = [], self._rpc("files/search_v2", body, ns=ns), 0
        while True:
            for m in j.get("matches", []):
                md = (m.get("metadata") or {}).get("metadata") or {}
                if md.get(".tag") != "file":
                    continue
                disp = md.get("path_display") or md.get("name", "")
                if folder_name:
                    disp = f"/{folder_name}{disp}"
                res.append(Candidate(md.get("name", ""), disp, int(md.get("size") or 0), "dropbox",
                                     {"id": md.get("id"), "ns": ns, "path": md.get("path_display")}))
            pages += 1
            if not j.get("has_more") or pages >= 3:
                break
            j = self._rpc("files/search/continue_v2", {"cursor": j["cursor"]}, ns=ns)
        return res

    def search(self, layer_name: str) -> list[Candidate]:
        if not self.account:
            self.connect()
        found: dict[str, Candidate] = {}
        for q in search_queries(layer_name)[:3]:
            for c in self._search_ns(q, None):
                found.setdefault(c.ref.get("id") or c.path, c)
            if any(normalize(c.name) == normalize(layer_name) for c in found.values()):
                break
        try:
            for sf in self.unmounted_shared_folders()[:50]:
                for c in self._search_ns(search_queries(layer_name)[0], sf["shared_folder_id"], sf.get("name", "")):
                    found.setdefault(c.ref.get("id") or c.path, c)
        except DropboxError:
            pass  # sharing.read scope not granted: team + mounted folders are still searched
        out = list(found.values())
        for c in out:  # prefer a synced local copy: hashing it is free
            lp = self.local_path(c)
            if lp:
                c.ref["local"] = lp
                c.online_only = is_online_only(lp)
            else:
                c.online_only = True
        return out

    def local_path(self, c: Candidate) -> Optional[str]:
        if not self.local_root or c.ref.get("ns") or not c.ref.get("path"):
            return None
        p = os.path.join(self.local_root, *c.ref["path"].strip("/").split("/"))
        return p if os.path.isfile(p) else None

    def fetch(self, c, workdir, progress=None):
        lp = c.ref.get("local")
        if lp and not is_online_only(lp):
            return lp, self.cache.sha1(lp, progress), False
        h = self._headers(c.ref.get("ns"))
        h["Dropbox-API-Arg"] = json.dumps({"path": c.ref.get("id") or c.ref.get("path")})
        os.makedirs(workdir, exist_ok=True)
        fd, tmp = tempfile.mkstemp(suffix=os.path.splitext(c.name)[1] or ".bin", dir=workdir)
        try:
            with self.http.post(f"{self.content}/files/download", headers=h, stream=True, timeout=120) as r:
                if r.status_code >= 400:
                    raise DropboxError(f"Download failed ({r.status_code}): {r.text[:200]}")
                total = int(r.headers.get("Content-Length") or c.size or 0)
                with os.fdopen(fd, "wb") as f:
                    w = HashingWriter(f)
                    for chunk in r.iter_content(1 << 20):
                        w.write(chunk)
                        if progress and total:
                            progress(min(1.0, w.bytes_written / total))
            sha = w.hexdigest()
            self.cache.put(tmp, sha)
            return tmp, sha, True
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise


# ---------------------------------------------------------------------------
@dataclass
class MatchResult:
    status: str                      # "confirmed" | "not_found" | "no_candidates"
    path: Optional[str] = None       # local file with the original bytes
    candidate: Optional[Candidate] = None
    temporary: bool = False
    tried: list[dict] = field(default_factory=list)

    def summary(self) -> dict:
        return {"status": self.status, "path": self.path,
                "source": self.candidate.path if self.candidate else None,
                "score": self.candidate.score if self.candidate else None, "tried": self.tried}


def find_source(layer_name: str, video_hash: str, sources: list[Source], workdir: str,
                max_hash: int = 6, max_bytes: int = 4_000_000_000,
                progress: Optional[Progress] = None, log: Optional[Log] = None) -> MatchResult:
    log = log or (lambda m: None)
    by_source: dict[int, Source] = {}
    cands: list[Candidate] = []
    for s in sources:
        try:
            hits = s.search(layer_name)
        except Exception as e:
            log(f"{s.label}: search failed ({e})")
            continue
        for c in hits:
            by_source[id(c)] = s
        cands += hits
    ranked = rank(layer_name, cands)
    if not ranked:
        return MatchResult("no_candidates")
    tried = []
    for c in ranked[:max_hash]:
        if c.size and c.size > max_bytes:
            tried.append({"path": c.path, "result": "too large to check"})
            continue
        src = by_source[id(c)]
        log(f"Checking {c.name} ({'cloud' if c.online_only else 'on disk'})")
        try:
            path, sha, temp = src.fetch(c, workdir, progress)
        except Exception as e:
            tried.append({"path": c.path, "result": f"error: {e}"})
            continue
        if hash_matches(sha, video_hash):
            tried.append({"path": c.path, "result": "match"})
            return MatchResult("confirmed", path, c, temp, tried)
        tried.append({"path": c.path, "result": "different file"})
        if temp:
            try:
                os.remove(path)
            except OSError:
                pass
    return MatchResult("not_found", tried=tried)
