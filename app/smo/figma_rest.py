"""Read-only Figma REST calls used to identify the signed-in user and the deck."""
from __future__ import annotations

import re
from typing import Optional

import requests

API = "https://api.figma.com/v1"
_URL = re.compile(r"figma\.com/(?:file|design|slides|deck|proto|board)/([A-Za-z0-9]{10,})")


def parse_file_key(url_or_key: str) -> Optional[str]:
    s = (url_or_key or "").strip()
    m = _URL.search(s)
    if m:
        return m.group(1)
    return s if re.fullmatch(r"[A-Za-z0-9]{15,}", s) else None


class FigmaRest:
    def __init__(self, token_fn, session: Optional[requests.Session] = None):
        self.token_fn = token_fn
        self.http = session or requests.Session()

    def _get(self, path: str, **params) -> dict:
        tok = self.token_fn()
        if not tok:
            raise PermissionError("Not signed in to Figma.")
        r = self.http.get(API + path, headers={"Authorization": f"Bearer {tok}"}, params=params, timeout=30)
        if r.status_code == 403:
            raise PermissionError("Figma says this account can't open that file.")
        if r.status_code == 404:
            raise LookupError("Figma couldn't find that file. Check the link.")
        r.raise_for_status()
        return r.json()

    def me(self) -> dict:
        return self._get("/me")

    def file_meta(self, key: str) -> dict:
        """Name, thumbnail and last-modified for a file (works for Slides decks)."""
        try:
            j = self._get(f"/files/{key}/meta")
            f = j.get("file", j)
            return {"name": f.get("name"), "thumbnail": f.get("thumbnail_url") or f.get("thumbnailUrl"),
                    "lastModified": f.get("last_touched_at") or f.get("lastModified"),
                    "editorType": f.get("editorType") or f.get("editor_type")}
        except (requests.HTTPError, LookupError):
            j = self._get(f"/files/{key}", depth=1)
            return {"name": j.get("name"), "thumbnail": j.get("thumbnailUrl"),
                    "lastModified": j.get("lastModified"), "editorType": j.get("editorType")}
