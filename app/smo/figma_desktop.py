"""Add the plugin to the Figma desktop app without the "Import plugin from manifest" dialog.

Figma desktop keeps development plugins in its settings.json, under
"localFileExtensions": one entry for the manifest plus one each for the code
and UI files, linked by id. Figma rewrites settings.json when it quits, so the
file is only edited while Figma is closed. A backup is kept next to it.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from typing import Optional

PLUGIN_NAME = "Steelworks Media Optimiser"
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def settings_path() -> Optional[str]:
    if sys.platform == "win32":
        p = os.path.join(os.environ.get("APPDATA", ""), "Figma", "settings.json")
    elif sys.platform == "darwin":
        p = os.path.expanduser("~/Library/Application Support/Figma/settings.json")
    else:
        return None
    return p if os.path.isfile(p) else None


def _norm(p: str) -> str:
    return os.path.normcase(os.path.normpath(p))


def is_registered(manifest: str, path: Optional[str] = None) -> bool:
    path = path or settings_path()
    if not path:
        return False
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return False
    return any(e.get("fileMetadata", {}).get("type") == "manifest" and _norm(e.get("manifestPath", "")) == _norm(manifest)
               for e in data.get("localFileExtensions") or [])


def register(manifest: str, path: Optional[str] = None) -> bool:
    """Append the plugin to Figma's list. Returns True if something was added."""
    path = path or settings_path()
    if not path:
        raise FileNotFoundError("Figma desktop isn't installed (or hasn't been opened yet).")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    exts = data.setdefault("localFileExtensions", [])
    # Drop stale entries for this plugin (e.g. an older install location that no longer exists).
    stale = {e["id"] for e in exts if e.get("fileMetadata", {}).get("type") == "manifest"
             and e.get("lastKnownName") == PLUGIN_NAME and not os.path.isfile(e.get("manifestPath", ""))}
    if stale:
        exts[:] = [e for e in exts if e.get("id") not in stale and e.get("fileMetadata", {}).get("manifestFileId") not in stale]
    if any(e.get("fileMetadata", {}).get("type") == "manifest" and _norm(e.get("manifestPath", "")) == _norm(manifest) for e in exts):
        if stale:
            _write(path, data)
        return False
    with open(manifest, encoding="utf-8") as f:
        m = json.load(f)
    base = os.path.dirname(os.path.abspath(manifest))
    code = os.path.normpath(os.path.join(base, m["main"]))
    ui = os.path.normpath(os.path.join(base, m["ui"])) if isinstance(m.get("ui"), str) else None
    nid = max([e.get("id", 0) for e in exts] + [0]) + 1
    mid, cid, uid = nid, nid + 1, (nid + 2 if ui else None)
    exts.append({"id": mid, "manifestPath": os.path.abspath(manifest), "lastKnownName": m.get("name", PLUGIN_NAME),
                 "lastKnownPluginId": m.get("id"),
                 "fileMetadata": {"type": "manifest", "codeFileId": cid, "uiFileIds": [uid] if uid else None},
                 "cachedContainsWidget": False})
    exts.append({"id": cid, "manifestPath": code, "fileMetadata": {"type": "code", "manifestFileId": mid}})
    if ui:
        exts.append({"id": uid, "manifestPath": ui, "fileMetadata": {"type": "ui", "manifestFileId": mid}})
    _write(path, data)
    return True


def _write(path: str, data: dict) -> None:
    backup = path + ".before-steelworks"
    if not os.path.exists(backup):
        shutil.copy2(path, backup)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


# ----------------------------------------------------------- the Figma process
def is_running() -> bool:
    try:
        if sys.platform == "win32":
            out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Figma.exe", "/NH"], capture_output=True, text=True,
                                 creationflags=_NO_WINDOW).stdout
            return "Figma.exe" in out
        if sys.platform == "darwin":
            return subprocess.run(["pgrep", "-x", "Figma"], capture_output=True).returncode == 0
    except OSError:
        pass
    return False


def quit_figma(timeout: float = 20) -> bool:
    """Ask Figma to quit normally (like closing its window) and wait for it."""
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/IM", "Figma.exe"], capture_output=True, creationflags=_NO_WINDOW)
        elif sys.platform == "darwin":
            subprocess.run(["osascript", "-e", 'quit app "Figma"'], capture_output=True)
    except OSError:
        return False
    end = time.time() + timeout
    while time.time() < end:
        if not is_running():
            time.sleep(1.0)        # let it finish writing settings.json
            return True
        time.sleep(0.5)
    return False


def launch_figma() -> None:
    try:
        if sys.platform == "win32":
            exe = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Figma", "Figma.exe")
            if os.path.isfile(exe):
                subprocess.Popen([exe], creationflags=_NO_WINDOW)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-a", "Figma"])
    except OSError:
        pass


# ------------------------------------------------- open a deck, start the plugin
def desktop_link(url: str) -> Optional[str]:
    """https://www.figma.com/slides/KEY/Name -> figma://slides/KEY/Name (opens in the desktop app)."""
    import re
    m = re.search(r"figma\.com/((?:file|design|slides|deck|proto|board)/[A-Za-z0-9]{10,}[^?#\s]*)", url or "")
    return f"figma://{m.group(1)}" if m else None


def open_deck(url: str) -> bool:
    """Open a share link in the Figma desktop app. Returns False if Figma desktop isn't there."""
    link = desktop_link(url)
    if not link or not settings_path():
        return False
    try:
        if sys.platform == "win32":
            os.startfile(link)  # type: ignore[attr-defined]   # handled by Figma's figma:// protocol
        elif sys.platform == "darwin":
            subprocess.run(["open", link], check=True)
        else:
            return False
        return True
    except OSError:
        return False


_PS_START = r"""
$ErrorActionPreference = 'Stop'
$p = Get-Process -Name Figma -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
if (-not $p) { exit 2 }
$w = New-Object -ComObject WScript.Shell
if (-not $w.AppActivate($p.Id)) { exit 3 }
Start-Sleep -Milliseconds 400
$w.SendKeys('{ESC}')
Start-Sleep -Milliseconds 200
$w.SendKeys('^/')
Start-Sleep -Milliseconds 700
$w.SendKeys('__NAME__')
Start-Sleep -Milliseconds 1500
$w.SendKeys('{ENTER}')
"""

_OSA_START = """
tell application "Figma" to activate
delay 0.4
tell application "System Events"
  key code 53
  delay 0.2
  keystroke "/" using command down
  delay 0.7
  keystroke "__NAME__"
  delay 1.5
  key code 36
end tell
"""


def start_plugin(name: str = PLUGIN_NAME) -> tuple[bool, str]:
    """Run the plugin in the front Figma window via Quick actions (Ctrl/Cmd + /),
    the same as a person typing its name. Returns (ok, message)."""
    safe = "".join(ch for ch in name if ch.isalnum() or ch == " ")
    try:
        if sys.platform == "win32":
            r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS_START.replace("__NAME__", safe)],
                               capture_output=True, text=True, creationflags=_NO_WINDOW, timeout=30)
            if r.returncode == 2:
                return False, "Figma isn't open."
            if r.returncode != 0:
                return False, "Couldn't bring Figma to the front."
            return True, ""
        if sys.platform == "darwin":
            r = subprocess.run(["osascript", "-e", _OSA_START.replace("__NAME__", safe)], capture_output=True, text=True, timeout=30)
            if r.returncode != 0:
                if "not allowed" in r.stderr or "1002" in r.stderr or "-1743" in r.stderr:
                    return False, ("Allow Steelworks Media Optimiser to control your computer: System Settings › "
                                   "Privacy & Security › Accessibility.")
                return False, "Couldn't start the plugin in Figma."
            return True, ""
    except (OSError, subprocess.TimeoutExpired):
        pass
    return False, "Couldn't start the plugin automatically."
