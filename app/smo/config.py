"""Settings. Nothing secret lives in the source code.

Lookup order for each value (first hit wins):
  1. environment variable (SMO_FIGMA_CLIENT_ID, SMO_FIGMA_CLIENT_SECRET, SMO_DROPBOX_APP_KEY, SMO_PORT)
  2. the user's settings file   <data dir>/config.json      (written by the Settings screen)
  3. a studio-wide file         steelworks.config.json next to the app, or in the data dir
The Figma client secret is kept in the OS secure store once entered in Settings.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

APP_NAME = "Steelworks Media Optimiser"
DEFAULT_PORT = 47821


def data_dir() -> str:
    if sys.platform == "win32":
        root = os.environ.get("APPDATA") or os.path.expanduser("~\\AppData\\Roaming")
    elif sys.platform == "darwin":
        root = os.path.expanduser("~/Library/Application Support")
    else:
        root = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    d = os.path.join(root, APP_NAME)
    os.makedirs(d, exist_ok=True)
    return d


def work_dir() -> str:
    d = os.path.join(data_dir(), "work")
    os.makedirs(d, exist_ok=True)
    return d


def _app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _read(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


ENV = {
    "figma_client_id": "SMO_FIGMA_CLIENT_ID",
    "figma_client_secret": "SMO_FIGMA_CLIENT_SECRET",
    "dropbox_app_key": "SMO_DROPBOX_APP_KEY",
    "port": "SMO_PORT",
    "ffmpeg": "SMO_FFMPEG",
    "local_dropbox_root": "SMO_LOCAL_DROPBOX_ROOT",
}

DEFAULTS: dict[str, Any] = {
    "port": DEFAULT_PORT,
    "image_workers": max(2, (os.cpu_count() or 4) - 2),
    "video_workers": 2,
    "download_workers": 4,
    "max_candidates_to_hash": 6,
    "max_download_mb": 4000,
}


class Config:
    def __init__(self, path: str | None = None):
        self.path = path or os.path.join(data_dir(), "config.json")
        self.reload()

    def reload(self) -> None:
        studio = {}
        for p in (os.path.join(_app_dir(), "steelworks.config.json"),
                  os.path.join(data_dir(), "steelworks.config.json")):
            studio.update(_read(p))
        self._studio = studio
        self._user = _read(self.path)

    def get(self, key: str, default: Any = None) -> Any:
        env = ENV.get(key)
        if env and os.environ.get(env):
            v = os.environ[env]
            return int(v) if key == "port" else v
        for src in (self._user, self._studio):
            if src.get(key) not in (None, ""):
                return src[key]
        return DEFAULTS.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._user[key] = value
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._user, f, indent=2)
        os.replace(tmp, self.path)

    def source_of(self, key: str) -> str:
        env = ENV.get(key)
        if env and os.environ.get(env):
            return "environment"
        if self._user.get(key):
            return "settings"
        if self._studio.get(key):
            return "studio file"
        return "missing"
