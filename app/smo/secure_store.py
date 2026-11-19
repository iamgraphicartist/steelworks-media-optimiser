"""Small secure store for OAuth tokens and the Figma client secret.

Windows: DPAPI (CryptProtectData, tied to the signed-in Windows user).
macOS:   the login Keychain via /usr/bin/security.
Other:   a 0600 file in the data dir.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys

from .config import data_dir

SERVICE = "com.steelworks.media-optimiser"


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    _crypt32 = ctypes.windll.crypt32
    _kernel32 = ctypes.windll.kernel32

    def _blob(data: bytes) -> _BLOB:
        buf = ctypes.create_string_buffer(data, len(data))
        return _BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))

    def _dpapi(data: bytes, protect: bool) -> bytes:
        src, out = _blob(data), _BLOB()
        fn = _crypt32.CryptProtectData if protect else _crypt32.CryptUnprotectData
        ok = fn(ctypes.byref(src), None, None, None, None, 0x1, ctypes.byref(out))  # UI_FORBIDDEN
        if not ok:
            raise OSError("DPAPI call failed")
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            _kernel32.LocalFree(out.pbData)


class SecureStore:
    def __init__(self, path: str | None = None):
        self.path = path or os.path.join(data_dir(), "secrets.bin")

    # --- raw blob persistence -------------------------------------------------
    def _load(self) -> dict:
        if sys.platform == "darwin":
            r = subprocess.run(["/usr/bin/security", "find-generic-password", "-s", SERVICE, "-a", "store", "-w"],
                               capture_output=True, text=True)
            return json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else {}
        try:
            with open(self.path, "rb") as f:
                raw = f.read()
        except OSError:
            return {}
        try:
            if sys.platform == "win32":
                raw = _dpapi(base64.b64decode(raw), protect=False)
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _save(self, d: dict) -> None:
        payload = json.dumps(d).encode("utf-8")
        if sys.platform == "darwin":
            subprocess.run(["/usr/bin/security", "add-generic-password", "-U", "-s", SERVICE, "-a", "store",
                            "-w", payload.decode("utf-8")], check=True, capture_output=True)
            return
        if sys.platform == "win32":
            payload = base64.b64encode(_dpapi(payload, protect=True))
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
        os.replace(tmp, self.path)

    # --- API --------------------------------------------------------------------
    def get(self, key: str, default=None):
        return self._load().get(key, default)

    def set(self, key: str, value) -> None:
        d = self._load()
        d[key] = value
        self._save(d)

    def delete(self, key: str) -> None:
        d = self._load()
        if key in d:
            del d[key]
            self._save(d)
