"""The workflow: everything the app's steps do, plus the job queue.

Runs on the bridge's asyncio loop. Heavy work (Pillow, ffmpeg, hashing,
downloads) goes to thread pools; the plugin is driven with `self.plugin_call`.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import io
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional

from PIL import Image

from . import figma_desktop
from . import images as imglib
from . import report as reportlib
from . import video as vidlib
from .config import APP_NAME, Config, data_dir, work_dir
from .figma_rest import FigmaRest, parse_file_key
from .hashing import sha1_file
from .oauth import DropboxOAuth, FigmaOAuth, OAuthError
from .secure_store import SecureStore
from .sources import (DropboxSource, HashCache, LocalFolderSource, dropbox_desktop_root, find_source)

VERSION = "1.0.8"
SMALL_IMAGE = 100 * 1024   # below this an image is never worth touching


def _now() -> str:
    return _dt.datetime.now().strftime("%d %b %Y %H:%M")


def _short(path):
    """Show paths inside the home folder as ~/… (shorter, and no user name on screen)."""
    if not path:
        return path
    home = os.path.normpath(os.path.expanduser("~"))
    p = os.path.normpath(path)
    return "~" + p[len(home):] if os.path.normcase(p).startswith(os.path.normcase(home) + os.sep) else p


def _display_long(use: dict) -> float:
    sc = use.get("scale") or {}
    return max(use.get("w", 0) * (sc.get("x") or 1), use.get("h", 0) * (sc.get("y") or 1))


def reveal(path: str) -> None:
    """Show a file selected in Explorer / Finder."""
    try:
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(path)])
    except OSError:
        pass


def open_path(path: str) -> None:
    try:
        if sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except OSError:
        pass


class PluginGone(Exception):
    pass


class Engine:
    def __init__(self, cfg: Config, store: SecureStore, port: int, demo: Optional[dict] = None):
        self.cfg, self.store, self.port, self.demo = cfg, store, port, demo
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.figma_auth = FigmaOAuth(cfg, store, port)
        self.dropbox_auth = DropboxOAuth(cfg, store, port)
        self.hash_cache = HashCache(os.path.join(data_dir(), "hash-cache.json"))
        self.blob_dir = os.path.join(work_dir(), "blobs")
        self.out_dir = os.path.join(work_dir(), "out")
        self.dl_dir = os.path.join(work_dir(), "downloads")
        for d in (self.blob_dir, self.out_dir, self.dl_dir):
            os.makedirs(d, exist_ok=True)
        self.img_pool = ThreadPoolExecutor(int(cfg.get("image_workers")), thread_name_prefix="img")
        self.vid_pool = ThreadPoolExecutor(int(cfg.get("video_workers")), thread_name_prefix="ffmpeg")
        self.io_pool = ThreadPoolExecutor(int(cfg.get("download_workers")), thread_name_prefix="io")
        self.ffmpeg = vidlib.find_tool("ffmpeg", cfg.get("ffmpeg"))
        self.ffprobe = vidlib.find_tool("ffprobe", os.path.join(os.path.dirname(self.ffmpeg), "ffprobe" +
                                                                (".exe" if sys.platform == "win32" else ""))
                                        if self.ffmpeg else None)
        self.listeners: set = set()
        self.plugin_send: Optional[Callable[[dict], Any]] = None
        self._rpc: dict[str, tuple[asyncio.Future, Optional[Callable]]] = {}
        self.pair_code = store.get("pair_code") or self._new_code()
        self.plugin_tokens: list = store.get("plugin_tokens") or []
        self._pair_waiters: list = []
        self.inv: Optional[dict] = None
        self.undo: list[dict] = []
        self.version_saved = False
        self._dirty = False
        self._busy: Optional[str] = None
        self.state: dict = {
            "version": VERSION, "gap": 12, "demo": bool(demo), "pairCode": self.pair_code, "pairRequest": False,
            "connections": {}, "deck": {}, "activity": {"title": "Ready", "detail": "", "progress": None},
            "scan": {"status": "idle"}, "images": {"status": "idle", "items": []},
            "cleanup": {"status": "idle", "items": []}, "videos": {"status": "idle", "items": []},
            "spacing": {"status": "idle", "grids": []}, "align": {"status": "idle", "items": []}, "report": {},
            "auto": {"status": "idle"}, "undo": {"count": 0, "reversible": 0},
        }
        self.state["gap"] = self.gap()
        self._refresh_connections()

    # ------------------------------------------------------------- plumbing
    def quality(self) -> str:
        q = self.cfg.get("quality") or "best"
        return q if q in imglib.PRESETS else "best"

    def gap(self) -> float:
        try:
            return max(0.0, min(200.0, float(self.cfg.get("thumbnail_gap"))))
        except (TypeError, ValueError):
            return 12.0

    def _new_code(self) -> str:
        code = "".join(secrets.choice("0123456789") for _ in range(6))
        self.store.set("pair_code", code)
        return code

    def changed(self) -> None:
        """Schedule a (coalesced) state push to every open UI."""
        if self.loop is None:
            return
        if threading_ident() != self._loop_thread:
            self.loop.call_soon_threadsafe(self.changed)
            return
        if not self._dirty:
            self._dirty = True
            self.loop.call_later(0.08, self._flush)

    def _flush(self) -> None:
        self._dirty = False
        msg = json.dumps({"event": "state", "data": self.state}, default=str)
        for send in list(self.listeners):
            asyncio.ensure_future(send(msg))

    def attach(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self._loop_thread = threading_ident()
        self._local_source = None
        root = None if self.demo else (self.cfg.get("local_dropbox_root") or dropbox_desktop_root())
        if not self.demo:
            loop.run_in_executor(self.io_pool, self._auto_register_plugin)
        if root and os.path.isdir(root):
            self._local_source = LocalFolderSource(root, self.hash_cache, "Dropbox")
            loop.run_in_executor(self.io_pool, self._local_source.search, "warm-up")  # builds the index under its lock

    def activity(self, title: str, detail: str = "", progress: Optional[float] = None) -> None:
        self.state["activity"] = {"title": title, "detail": detail, "progress": progress}
        self.changed()
        if self.plugin_send:
            try:
                asyncio.run_coroutine_threadsafe(self.plugin_send({"type": "status", "title": title, "detail": detail,
                                                                   "progress": progress}), self.loop)
            except Exception:
                pass

    def _refresh_connections(self) -> None:
        f_tok, d_tok = self.figma_auth.token(), self.dropbox_auth.token()
        local_root = self.cfg.get("local_dropbox_root") or dropbox_desktop_root()
        self.state["connections"] = {
            "figma": {"configured": self.figma_auth.configured(), "signedIn": bool(f_tok),
                      "user": (f_tok or {}).get("user")},
            "dropbox": {"configured": self.dropbox_auth.configured(), "signedIn": bool(d_tok),
                        "account": (d_tok or {}).get("account"), "localRoot": local_root,
                        "localRootShort": _short(local_root)},
            "plugin": self.state.get("connections", {}).get("plugin", {"connected": False}),
            "ffmpeg": {"ok": bool(self.ffmpeg and self.ffprobe), "path": self.ffmpeg},
            "figmaApp": self._figma_app_status(),
        }
        if self.demo:
            self.state["connections"]["figma"].update({"signedIn": True, "user": {"handle": "Demo user"}, "demo": True})
            self.state["connections"]["dropbox"].update({"signedIn": getattr(self, "_demo_dbx", False), "demo": True,
                                                         "configured": True,
                                                         "account": {"name": "Sample folder", "team": "Demo"},
                                                         "localRoot": self.demo.get("dropbox"),
                                                         "localRootShort": "samples/dropbox (demo)"})
        self.changed()

    def _figma_app_status(self) -> dict:
        if self.demo:
            return getattr(self, "_demo_figma", {"installed": True, "registered": False, "running": True})
        return {"installed": bool(figma_desktop.settings_path()),
                "registered": figma_desktop.is_registered(self.plugin_manifest()),
                "running": figma_desktop.is_running()}

    def _auto_register_plugin(self) -> None:
        """At start-up: add the plugin to Figma desktop if Figma is closed. Zero clicks."""
        try:
            if figma_desktop.settings_path() and not figma_desktop.is_running():
                if figma_desktop.register(self.plugin_manifest()):
                    self.activity("Plugin added to Figma", "It’s under Plugins › Development the next time you open Figma.")
        except Exception:
            pass
        self.state["connections"]["figmaApp"] = self._figma_app_status()
        self.changed()

    async def a_plugin_status(self, p):
        st = await self.loop.run_in_executor(self.io_pool, self._figma_app_status)
        self.state["connections"]["figmaApp"] = st
        self.changed()
        return st

    async def a_plugin_install(self, p):
        """One click: (restart Figma if it's open) and add the plugin to it."""
        if self.demo:
            self._demo_figma = {"installed": True, "registered": True, "running": True}
            self.state["connections"]["figmaApp"] = self._demo_figma
            self.activity("Plugin added to Figma", "Figma restarted with the plugin ready.")
            return True
        manifest = self.plugin_manifest()

        def work():
            if not figma_desktop.settings_path():
                raise RuntimeError("Figma desktop isn’t installed on this computer. Install it from figma.com/downloads, open it once, then try again.")
            was_running = figma_desktop.is_running()
            if was_running:
                self.activity("Restarting Figma", "Closing Figma to add the plugin…", None)
                if not figma_desktop.quit_figma():
                    raise RuntimeError("Figma didn’t close. Close it yourself (File › Quit), then click Add to Figma again.")
            figma_desktop.register(manifest)
            if was_running:
                figma_desktop.launch_figma()
            return was_running
        restarted = await self.loop.run_in_executor(self.io_pool, work)
        self.state["connections"]["figmaApp"] = self._figma_app_status()
        self.activity("Plugin added to Figma", "Figma is reopening." if restarted else "Open Figma and it’s ready under Plugins › Development.")
        return True

    # ------------------------------------------------------------- plugin RPC
    def plugin_connected(self, send, info: Optional[dict]) -> None:
        self.plugin_send = send
        info = dict(info or {})
        info["_at"] = time.time()
        self.state["connections"]["plugin"] = {"connected": True, "file": info}
        self._check_deck_match()
        self.activity("Plugin connected", f"Working in “{(info or {}).get('name', 'Figma')}”.")

    def plugin_disconnected(self) -> None:
        self.plugin_send = None
        self.state["connections"]["plugin"] = {"connected": False}
        for fut, _ in self._rpc.values():
            if not fut.done():
                fut.set_exception(PluginGone("The Figma plugin was closed."))
        self._rpc.clear()
        self.changed()

    def plugin_message(self, m: dict) -> None:
        rid = m.get("id")
        if rid not in self._rpc:
            return
        fut, on_progress = self._rpc[rid]
        if "progress" in m:
            if on_progress and m["progress"]:
                on_progress(m["progress"])
            return
        self._rpc.pop(rid, None)
        if fut.done():
            return
        if m.get("error"):
            fut.set_exception(RuntimeError(m["error"]))
        else:
            fut.set_result(m.get("result"))

    DECK_CHANGES = {"swapImages", "removeItems", "applySpacing", "applyMoves", "placeVideo", "saveVersion"}

    async def plugin_call(self, method: str, params: Optional[dict] = None, on_progress=None, timeout: float = 3600):
        if method in self.DECK_CHANGES and getattr(self, "_auto_stop", False) and self._busy == "optimising the deck":
            raise asyncio.CancelledError()          # Stop was pressed: never touch the deck again in this run
        if not self.plugin_send:
            raise PluginGone("Open the deck in Figma and run the Steelworks Media Optimiser plugin first.")
        rid = secrets.token_hex(6)
        fut = self.loop.create_future()
        self._rpc[rid] = (fut, on_progress)
        await self.plugin_send({"id": rid, "method": method, "params": params or {}})
        return await asyncio.wait_for(fut, timeout)

    def check_plugin_token(self, token: Optional[str], pair: Optional[str]) -> Optional[str]:
        """Returns the token to use, or None to refuse the connection."""
        if token and token in self.plugin_tokens:
            return token
        if pair and secrets.compare_digest(pair, self.pair_code):
            tok = secrets.token_urlsafe(32)
            self.plugin_tokens = (self.plugin_tokens + [tok])[-10:]
            self.store.set("plugin_tokens", self.plugin_tokens)
            self.pair_code = self._new_code()          # one code, one pairing
            self.state["pairCode"] = self.pair_code
            self.changed()
            return tok
        return None

    async def request_pairing(self, timeout: float = 180) -> Optional[str]:
        """One-click pairing: the plugin asks, the person clicks Allow in the app."""
        fut = self.loop.create_future()
        self._pair_waiters.append(fut)
        self.state["pairRequest"] = True
        self.changed()
        try:
            ok = await asyncio.wait_for(asyncio.shield(fut), timeout)
        except asyncio.TimeoutError:
            ok = False
        finally:
            if fut in self._pair_waiters:
                self._pair_waiters.remove(fut)
            self.state["pairRequest"] = bool(self._pair_waiters)
            self.changed()
        if not ok:
            return None
        tok = secrets.token_urlsafe(32)
        self.plugin_tokens = (self.plugin_tokens + [tok])[-10:]
        self.store.set("plugin_tokens", self.plugin_tokens)
        return tok

    async def a_plugin_allow(self, p):
        for f in list(self._pair_waiters):
            if not f.done():
                f.set_result(bool(p.get("allow", True)))
        self._pair_waiters.clear()
        self.state["pairRequest"] = False
        self.changed()

    def valid_token(self, token: Optional[str]) -> bool:
        return bool(token) and token in self.plugin_tokens

    # ------------------------------------------------------------- UI actions
    async def handle(self, action: str, p: dict) -> Any:
        fn = getattr(self, "a_" + action.replace(".", "_"), None)
        if not fn:
            raise ValueError(f"Unknown action {action}")
        return await fn(p or {})

    async def _exclusive(self, name: str, coro):
        if self._busy:
            raise RuntimeError(f"Please wait — still {self._busy}.")
        self._busy = name
        try:
            return await coro
        finally:
            self._busy = None
            self.changed()

    async def a_state(self, p):
        return self.state

    async def a_settings_get(self, p):
        keys = ["figma_client_id", "dropbox_app_key", "local_dropbox_root", "ffmpeg"]
        out = {k: self.cfg.get(k) or "" for k in keys}
        out["thumbnail_gap"] = self.gap()
        out["quality"] = self.quality()
        out["figma_client_secret_set"] = bool(self.figma_auth.client_secret())
        out["sources"] = {k: self.cfg.source_of(k) for k in keys}
        out["dataDir"] = "the app’s data folder" if self.demo else _short(data_dir())
        out["port"] = self.port
        out["redirects"] = {"figma": self.figma_auth.redirect_uri, "dropbox": self.dropbox_auth.redirect_uri}
        out["detected"] = {"local_dropbox_root": "samples/dropbox (demo)" if self.demo else _short(dropbox_desktop_root()),
                           "ffmpeg": _short(vidlib.find_tool("ffmpeg"))}
        return out

    ALLOWED_URLS = ("https://www.figma.com/developers", "https://www.dropbox.com/developers", "https://www.dropbox.com/install", "https://www.figma.com/downloads", "https://brew.sh",
                    "https://www.gyan.dev/ffmpeg", "https://github.com/")

    async def a_open_url(self, p):
        url = p.get("url", "")
        if not url.startswith(self.ALLOWED_URLS):
            raise ValueError("That link isn't allowed.")
        webbrowser.open(url)

    async def a_copy(self, p):
        text = str(p.get("text", ""))[:500]
        if sys.platform == "win32":
            subprocess.run(["clip"], input=text.encode("utf-16-le"), check=False)
        elif sys.platform == "darwin":
            subprocess.run(["pbcopy"], input=text.encode(), check=False)

    async def a_pick(self, p):
        """Native folder/file chooser (only in the app window, not in a browser tab)."""
        win = getattr(self, "window", None)
        if not win:
            raise RuntimeError("Choosing files only works in the app window. Paste the path instead.")
        import webview
        kind = webview.FOLDER_DIALOG if p.get("kind") == "folder" else webview.OPEN_DIALOG
        res = await self.loop.run_in_executor(self.io_pool, lambda: win.create_file_dialog(kind))
        return res[0] if res else None

    async def a_settings_save(self, p):
        for k in ("figma_client_id", "dropbox_app_key", "local_dropbox_root", "ffmpeg"):
            if k in p:
                self.cfg.set(k, (p[k] or "").strip())
        if p.get("quality") in imglib.PRESETS:
            self.cfg.set("quality", p["quality"])
        if p.get("thumbnail_gap") not in (None, ""):
            try:
                self.cfg.set("thumbnail_gap", max(0.0, min(200.0, float(p["thumbnail_gap"]))))
            except ValueError:
                raise ValueError("The gap must be a number of pixels, like 12.")
            self.state["gap"] = self.gap()
        if p.get("figma_client_secret"):
            self.store.set("figma_client_secret", p["figma_client_secret"].strip())
        if p.get("ffmpeg") is not None:
            self.ffmpeg = vidlib.find_tool("ffmpeg", self.cfg.get("ffmpeg"))
        self._refresh_connections()
        return True

    async def a_figma_signin(self, p):
        url = self.figma_auth.start()
        webbrowser.open(url)
        self.activity("Waiting for Figma", "Finish signing in in your browser.")
        return {"url": url}

    async def a_dropbox_signin(self, p):
        if self.demo:
            self._demo_dbx = True
            self._refresh_connections()
            return {}
        url = self.dropbox_auth.start()
        webbrowser.open(url)
        self.activity("Waiting for Dropbox", "Finish signing in in your browser.")
        return {"url": url}

    async def a_figma_signout(self, p):
        self.figma_auth.sign_out()
        self._refresh_connections()

    async def a_dropbox_signout(self, p):
        self.dropbox_auth.sign_out()
        self._refresh_connections()

    async def a_plugin_newcode(self, p):
        self.pair_code = self._new_code()
        self.state["pairCode"] = self.pair_code
        self.changed()
        return self.pair_code

    def plugin_manifest(self) -> str:
        if getattr(sys, "frozen", False):
            return os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(sys.executable)), "plugin", "manifest.json")
        return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "plugin", "manifest.json"))

    async def a_plugin_reveal(self, p):
        path = self.plugin_manifest()
        if not self.demo:
            reveal(path)
        await self.a_copy({"text": path})
        return path

    async def a_plugin_forget(self, p):
        self.plugin_tokens = []
        self.store.set("plugin_tokens", [])
        return await self.a_plugin_newcode(p)

    async def oauth_finished(self, provider: str, code: str, state: str) -> str:
        auth = self.figma_auth if provider == "figma" else self.dropbox_auth
        tok = await self.loop.run_in_executor(self.io_pool, auth.finish, code, state)
        try:
            if provider == "figma":
                me = await self.loop.run_in_executor(self.io_pool, FigmaRest(lambda: tok["access_token"]).me)
                tok["user"] = {"handle": me.get("handle"), "email": me.get("email"), "img": me.get("img_url")}
            else:
                src = DropboxSource(lambda: tok["access_token"], self.hash_cache)
                acc = await self.loop.run_in_executor(self.io_pool, src.connect)
                tok["account"] = {"name": acc.name, "email": acc.email, "team": acc.team,
                                  "teamSpace": bool(acc.root_namespace_id and acc.root_namespace_id != acc.home_namespace_id)}
            self.store.set(f"{provider}_token", tok)
        except Exception as e:  # signed in, but couldn't read the profile
            self.activity("Signed in", f"(Couldn't read your profile: {e})")
        self._refresh_connections()
        self.activity(f"Signed in to {provider.title()}")
        return "Figma" if provider == "figma" else "Dropbox"

    # ------------------------------------------------------------------ deck
    async def a_deck_set(self, p):
        key = parse_file_key(p.get("url", ""))
        if not key:
            raise ValueError("That doesn't look like a Figma link. Copy it from Share → Copy link.")
        m = re.search(r"/[A-Za-z0-9]{10,}/([^/?#]+)", p["url"])
        deck = {"url": p["url"].strip(), "key": key, "name": (m.group(1).replace("-", " ") if m else None)}
        tok = self.figma_auth.access_token()
        if tok:
            try:
                meta = await self.loop.run_in_executor(self.io_pool, FigmaRest(self.figma_auth.access_token).file_meta, key)
                deck.update(meta)
            except Exception as e:
                deck["warning"] = str(e)
        self.state["deck"] = deck
        self._check_deck_match()
        self.changed()
        return deck

    def _check_deck_match(self):
        deck, plug = self.state.get("deck") or {}, self.state["connections"].get("plugin") or {}
        f = plug.get("file") or {}
        if deck.get("key") and plug.get("connected"):
            if f.get("fileKey"):
                deck["pluginMatches"] = f["fileKey"] == deck["key"]
            else:  # fileKey is only exposed to private plugins; fall back to the name
                deck["pluginMatches"] = (not deck.get("name")) or deck.get("name") == f.get("name")
            if not deck.get("name"):
                deck["name"] = f.get("name")
        self.changed()

    # ------------------------------------------------------------------ scan
    async def a_scan(self, p):
        return await self._exclusive("reading the deck", self._scan())

    async def _scan(self):
        st = self.state["scan"] = {"status": "running"}
        self.activity("Reading the deck", "Looking at every slide…", 0)

        def prog(d):
            frac = d.get("done", 0) / d["total"] if d.get("total") else None
            st["progress"] = d
            self.activity("Reading the deck", d.get("label", ""), frac)
        try:
            await self.plugin_call("hello")
            inv = await self.plugin_call("inventory", {"upload": True, "gap": self.gap()}, prog)
        except Exception as e:
            st.update(status="error", error=str(e))
            self.activity("Couldn't read the deck", str(e))
            raise
        self.inv = inv
        info = inv.get("file") or {}
        self.state["connections"]["plugin"]["file"] = info
        if not self.state["deck"].get("key"):
            self.state["deck"] = {"name": info.get("name"), "key": info.get("fileKey")}
        self._check_deck_match()
        await self.loop.run_in_executor(self.img_pool, self._classify_images)
        self._build_after_scan()
        st["status"] = "done"
        s = st["summary"]
        self.activity("Deck read", f"{s['slides']} slides · {s['images']} images · {s['videos']} videos · "
                                   f"{s['gifs']} GIFs · {reportlib.mb(s['imageBytes'])} of images")
        return st["summary"]

    def _classify_images(self):
        for h, info in self.inv["images"].items():
            path = os.path.join(self.blob_dir, h)
            info["animated"] = False
            if info.get("format") == "gif" and os.path.exists(path):
                try:
                    with Image.open(path) as im:
                        info["animated"] = bool(getattr(im, "is_animated", False) and im.n_frames > 1)
                except Exception:
                    pass
            info["haveBytes"] = os.path.exists(path)

    def _build_after_scan(self):
        inv = self.inv
        imgs = inv["images"]
        gifs = {h: i for h, i in imgs.items() if i.get("animated")}
        stills = {h: i for h, i in imgs.items() if not i.get("animated")}
        img_bytes = sum(i.get("bytes") or 0 for i in imgs.values())
        rows = []
        for m in inv["media"]:
            info = imgs.get(m["hash"]) if m["kind"] == "image" else None
            kind = "gif" if info and info.get("animated") else m["kind"]
            rows.append({"nodeId": m["id"], "slide": m["slideIndex"], "name": m["name"], "kind": kind,
                         "bytes": (info or {}).get("bytes"),
                         "px": f"{info['width']}×{info['height']}" if info and info.get("width") else None,
                         "display": f"{m['display']['w']}×{m['display']['h']}",
                         "crop": m.get("scaleMode") == "CROP", "scaleMode": m.get("scaleMode"),
                         "issue": next((i["reason"] for i in inv["issues"] if i["nodeId"] == m["id"]), None)})
        rows.sort(key=lambda r: -(r["bytes"] or 0))
        self.state["scan"]["summary"] = {
            "slides": len(inv["slides"]), "images": len(stills), "gifs": len(gifs), "videos": len(inv["videos"]),
            "layers": len(inv["media"]), "imageBytes": img_bytes, "issues": len(inv["issues"]),
            "grids": len(inv["grids"])}
        self.state["scan"]["rows"] = rows

        # Images step
        items = []
        for h, info in stills.items():
            ratio = 0.0
            for u in info["uses"]:
                need = u.get("need") or {"w": u["w"], "h": u["h"]}
                if info.get("width"):
                    ratio = max(ratio, need["w"] / info["width"], need["h"] / info["height"])
            names = sorted({u["name"] for u in info["uses"]})
            slides = sorted({u["slideIndex"] for u in info["uses"]})
            plan = imglib.plan_size(info["width"], info["height"], ratio * info["width"], ratio * info["height"]) \
                if info.get("width") else None
            status = "pending"
            reason = ""
            if info.get("error") or not info.get("haveBytes"):
                status, reason = "skipped", info.get("error") or "Couldn't read this image from Figma."
            elif (info.get("bytes") or 0) < SMALL_IMAGE:
                status, reason = "skipped", "Already small."
            items.append({"hash": h, "names": names, "slides": slides, "before": info.get("bytes"),
                          "src": f"{info.get('width')}×{info.get('height')}", "format": info.get("format"),
                          "target": f"{plan.target_w}×{plan.target_h}" if plan else None, "ratio": ratio,
                          "status": status, "reason": reason, "after": None,
                          "nodes": [{"nodeId": u["nodeId"], "fillIndex": u["fillIndex"]} for u in info["uses"]]})
        items.sort(key=lambda i: -(i["before"] or 0))
        self.state["images"] = {"status": "ready", "items": items}

        # Clean-up step
        issues = []
        for i, iss in enumerate(inv["issues"]):
            b = (imgs.get(iss.get("hash")) or {}).get("bytes") if iss["kind"] == "image" else None
            issues.append({**iss, "key": f"{iss['nodeId']}#{iss['fillIndex']}", "bytes": b, "selected": True,
                           "status": "pending"})
        self.state["cleanup"] = {"status": "ready", "items": issues}

        # Videos step
        vids = []
        for h, v in inv["videos"].items():
            for idx, u in enumerate(v["uses"]):
                vids.append({"key": f"v:{h}:{u['nodeId']}", "kind": "video", "hash": h, "name": u["name"],
                             "slide": u["slideIndex"], "nodeId": u["nodeId"], "fillIndex": u["fillIndex"],
                             "display": _display_long(u),
                             "status": "pending", "detail": "Not looked for yet", "before": None, "after": None})
        for h, g in gifs.items():
            for u in g["uses"]:
                vids.append({"key": f"g:{h}:{u['nodeId']}", "kind": "gif", "hash": h, "name": u["name"],
                             "slide": u["slideIndex"], "nodeId": u["nodeId"], "fillIndex": u["fillIndex"],
                             "display": _display_long(u),
                             "status": "pending", "detail": "Will become a looping MP4", "before": g.get("bytes"),
                             "after": None})
        vids.sort(key=lambda v: (v["slide"], v["name"]))
        self.state["videos"] = {"status": "ready", "items": vids}
        # Removed layers shouldn't be worked on later: cleanup marks them.
        grids = [{**g, "selected": True, "status": "pending"} for g in inv["grids"]]
        self.state["spacing"] = {"status": "ready", "grids": grids}
        self.changed()

    # ----------------------------------------------------- version + undo log
    async def _ensure_version(self):
        if self.version_saved:
            return
        try:
            r = await self.plugin_call("saveVersion", {"title": f"Before Media Optimiser · {_now()}",
                                                       "description": "Saved automatically by Steelworks Media Optimiser."})
            self.version_saved = bool(r and r.get("saved"))
        except Exception:
            self.version_saved = False

    def _log_undo(self, entries: list[dict]):
        stamp = time.time()
        for e in entries:
            if e.get("ok"):
                e["at"] = stamp
                self.undo.append(e)
        self.state["undo"] = {"count": len(self.undo),
                              "reversible": sum(1 for e in self.undo if e["type"] in ("image", "move") and not e.get("reverted")),
                              "versionSaved": self.version_saved}
        try:
            key = (self.state.get("deck") or {}).get("key") or "deck"
            reportlib.write_undo_log(os.path.join(work_dir(), f"undo-{key}.json"), self.state.get("deck") or {}, self.undo)
        except OSError:
            pass
        self.changed()

    # ---------------------------------------------------------------- images
    async def a_images_run(self, p):
        return await self._exclusive("optimising images", self._images())

    async def _images(self):
        st = self.state["images"]
        todo = [i for i in st["items"] if i["status"] in ("pending", "error")]
        st["status"] = "running"
        total, done = len(todo), 0
        self.activity("Optimising images", f"0 of {total}", 0)

        def work(item):
            with open(os.path.join(self.blob_dir, item["hash"]), "rb") as f:
                data = f.read()
            info = self.inv["images"][item["hash"]]
            r = imglib.optimise(data, item["ratio"] * info["width"], item["ratio"] * info["height"], preset=self.quality())
            if r.keep or r.manual:
                name = f"{item['hash']}.{r.ext}"
                with open(os.path.join(self.out_dir, name), "wb") as f:
                    f.write(r.data)
                return r, name
            return r, None

        async def one(item):
            nonlocal done
            item["status"] = "working"
            self.changed()
            try:
                r, name = await self.loop.run_in_executor(self.img_pool, work, item)
                item.update(after=r.out_bytes if (r.keep or r.manual) else None, reason=r.reason, target=r.out_size,
                            file=name, mime=r.mime, psnr=r.psnr,
                            status="ready" if r.keep else "manual" if r.manual else "kept")
            except Exception as e:
                item.update(status="error", reason=f"Couldn't process: {e}")
            done += 1
            self.activity("Optimising images", f"{done} of {total} compressed", done / max(1, total) * 0.6)

        await asyncio.gather(*(one(i) for i in todo))
        ready = [i for i in st["items"] if i["status"] == "ready"]
        if ready:
            await self._ensure_version()
            for b in range(0, len(ready), 8):
                batch = ready[b:b + 8]
                res = await self.plugin_call("swapImages", {"items": [
                    {"oldHash": i["hash"], "url": f"/out/{i['file']}", "nodes": i["nodes"]} for i in batch]})
                log = res.get("log", [])
                self._log_undo(log)
                for i in batch:
                    ok = [e for e in log if e.get("oldHash") == i["hash"] and e.get("ok")]
                    err = [e for e in log if e.get("oldHash") == i["hash"] and not e.get("ok")]
                    if ok:
                        i["status"], i["newHash"] = "done", ok[0]["newHash"]
                    elif err and i.get("mime") == "image/webp":
                        await self._png_fallback(i)
                    else:
                        i.update(status="error", reason=(err[0]["error"] if err else "Not swapped"))
                self.activity("Swapping images in Figma", f"{min(b + 8, len(ready))} of {len(ready)}",
                              0.6 + 0.4 * min(b + 8, len(ready)) / len(ready))
        st["status"] = "done"
        self._update_totals()
        t = st["totals"]
        self.activity("Images done", f"{t['done']} images swapped · {reportlib.mb(t['before'])} → {reportlib.mb(t['after'])}", 1)
        return t

    async def _png_fallback(self, item):
        """Figma builds that refuse WebP get an optimised PNG instead (if still worth it)."""
        def work():
            with open(os.path.join(self.out_dir, item["file"]), "rb") as f:
                im = Image.open(io.BytesIO(f.read()))
                im.load()
            buf = io.BytesIO()
            im.save(buf, "PNG", optimize=True)
            return buf.getvalue()
        data = await self.loop.run_in_executor(self.img_pool, work)
        if len(data) > (1 - imglib.MIN_SAVING) * item["before"]:
            item.update(status="kept", reason="Figma didn't accept WebP and PNG wouldn't be smaller.", after=None)
            return
        name = f"{item['hash']}.png"
        with open(os.path.join(self.out_dir, name), "wb") as f:
            f.write(data)
        res = await self.plugin_call("swapImages", {"items": [{"oldHash": item["hash"], "url": f"/out/{name}",
                                                               "nodes": item["nodes"]}]})
        self._log_undo(res.get("log", []))
        ok = [e for e in res.get("log", []) if e.get("ok")]
        item.update(status="done" if ok else "error", after=len(data), file=name,
                    reason="Saved as PNG" if ok else "Figma refused the new image.")

    # --------------------------------------------------------------- cleanup
    async def a_cleanup_toggle(self, p):
        for i in self.state["cleanup"]["items"]:
            if p.get("all") is not None or i["key"] == p.get("key"):
                i["selected"] = bool(p["all"]) if p.get("all") is not None else not i["selected"]
        self.changed()

    async def a_cleanup_run(self, p):
        return await self._exclusive("removing layers", self._cleanup())

    async def _cleanup(self):
        st = self.state["cleanup"]
        chosen = [i for i in st["items"] if i["selected"] and i["status"] == "pending"]
        if not chosen:
            return {"removed": 0}
        st["status"] = "running"
        self.activity("Removing layers", f"{len(chosen)} approved", 0.3)
        await self._ensure_version()
        res = await self.plugin_call("removeItems", {"items": [
            {"nodeId": i["nodeId"], "removeKind": i["removeKind"], "fillIndex": i["fillIndex"], "hash": i["hash"]}
            for i in chosen]})
        log = res.get("log", [])
        self._log_undo(log)
        removed = 0
        for i in chosen:
            e = next((e for e in log if e.get("nodeId") == i["nodeId"]), None)
            if e and e.get("ok"):
                i["status"] = "done"
                removed += 1
                self._forget_node(i["nodeId"], i["removeKind"], i["hash"])
            else:
                i["status"] = "error"
                i["reason"] = (e or {}).get("error", "Not removed")
        st["status"] = "done"
        self._update_totals()
        self.activity("Clean-up done", f"{removed} layer{'s' if removed != 1 else ''} removed", 1)
        return {"removed": removed}

    def _forget_node(self, node_id, kind, hash_):
        """Drop removed layers from the other steps so they aren't processed."""
        for v in self.state["videos"]["items"]:
            if v["nodeId"] == node_id and (kind == "node" or v["hash"] == hash_):
                v.update(status="removed", detail="Removed in clean-up")
        for i in self.state["images"]["items"]:
            i["nodes"] = [n for n in i["nodes"] if not (n["nodeId"] == node_id and (kind == "node" or i["hash"] == hash_))]
            if not i["nodes"] and i["status"] in ("pending", "ready", "kept", "skipped"):
                i.update(status="removed", reason="All uses removed in clean-up")

    # ---------------------------------------------------------------- videos
    def _sources(self) -> list:
        out = []
        local_root = self.cfg.get("local_dropbox_root") or dropbox_desktop_root()
        if self.demo:
            return [LocalFolderSource(self.demo["dropbox"], self.hash_cache, "Sample Dropbox")]
        # Online first in the list, but local copies always win: DropboxSource maps each
        # online hit to the synced file when there is one, and the local index is also
        # searched so files synced from a personal (non-team) path are found too.
        if self.dropbox_auth.token():
            out.append(DropboxSource(self.dropbox_auth.access_token, self.hash_cache, local_root=local_root))
        if local_root and os.path.isdir(local_root):
            # No sign-in needed: the Dropbox desktop app's folder. Files that aren't synced
            # ("online-only") are downloaded by Dropbox automatically when they're read.
            if not (self._local_source and self._local_source.root == local_root):
                self._local_source = LocalFolderSource(local_root, self.hash_cache, "Dropbox")
            out.append(self._local_source)
        return out

    async def a_videos_prepare(self, p):
        return await self._exclusive("preparing videos", self._videos_prepare())

    async def _videos_prepare(self):
        st = self.state["videos"]
        if not (self.ffmpeg and self.ffprobe):
            raise RuntimeError("ffmpeg isn't installed. See README → Setup.")
        st["status"] = "running"
        sources = self._sources()
        items = [v for v in st["items"] if v["status"] in ("pending", "not_found", "error")]
        by_hash: dict[str, list] = {}
        for v in items:
            by_hash.setdefault(v["kind"] + v["hash"], []).append(v)
        total, done = len(by_hash), 0
        self.activity("Preparing videos", f"0 of {total}", 0)

        def set_all(group, **kw):
            for v in group:
                v.update(kw)
            self.changed()

        async def one(group):
            nonlocal done
            first = group[0]
            src_path, temp = None, False
            if first["kind"] == "gif":
                src_path = os.path.join(self.blob_dir, first["hash"])
                set_all(group, status="compressing", detail="Converting GIF to MP4", progress=0)
            else:
                if not sources:
                    set_all(group, status="not_found", detail="Sign in to Dropbox (or set a local Dropbox folder) to find the original.")
                    done += 1
                    return
                first_index = any(isinstance(src, LocalFolderSource) and src._index is None for src in sources)
                set_all(group, status="searching", detail="Looking through your Dropbox folder (the first search takes a minute)…"
                        if first_index else "Looking in Dropbox…")
                if first_index:
                    self.activity("Preparing videos", "Looking through your Dropbox folder for the originals. The first search takes a minute.")
                names = list(dict.fromkeys(v["name"] for v in group))
                res = None
                for nm in names:
                    def prog(f, g=group):
                        if getattr(self, "_auto_stop", False) and self._busy == "optimising the deck":
                            raise RuntimeError("Stopped")
                        set_all(g, detail=f"Checking a candidate… {round(f * 100)}%")
                    res = await self.loop.run_in_executor(
                        self.io_pool, lambda nm=nm: find_source(
                            nm, first["hash"], sources, self.dl_dir, int(self.cfg.get("max_candidates_to_hash")),
                            int(self.cfg.get("max_download_mb")) * 1_000_000, prog))
                    if res.status == "confirmed":
                        break
                if not res or res.status != "confirmed":
                    msg = ("No file with a similar name in Dropbox." if (not res or res.status == "no_candidates")
                           else f"Checked {len(res.tried)} similar file(s); none is the exact original.")
                    set_all(group, status="not_found", detail=msg, tried=(res.tried if res else []))
                    done += 1
                    return
                src_path, temp = res.path, res.temporary
                set_all(group, status="compressing", detail="Compressing", progress=0,
                        source=res.candidate.path, before=os.path.getsize(src_path))
            stem = re.sub(r'[\\/:*?"<>|]+', "_", os.path.splitext(first["name"])[0])[:60]
            out = os.path.join(self.out_dir, f"Slide{int(first['slide']):02d}_{stem}_{first['hash'][:6]}.mp4")

            preset = self.quality()
            display = max(v.get("display") or 0 for v in group)

            def run():
                fn = vidlib.gif_to_mp4 if first["kind"] == "gif" else vidlib.compress_video
                return fn(self.ffmpeg, self.ffprobe, src_path, out, preset=preset, display_long=display,
                          cancelled=lambda: getattr(self, "_auto_stop", False) and self._busy == "optimising the deck",
                          progress=lambda f: set_all(group, progress=f, detail=f"Compressing {round(f * 100)}%"))
            try:
                set_all(group, detail="Compressing")
                info = await self.loop.run_in_executor(self.vid_pool, run)
                before = os.path.getsize(src_path)
                after = os.path.getsize(out)
                score = getattr(info, "psnr", None)
                if score is not None and score < vidlib.PRESETS[preset]["min_psnr"]:
                    set_all(group, status="kept", detail=f"Would lose visible quality ({score:.0f} dB) — left as it is.",
                            before=before, after=None, psnr=round(score, 1))
                    os.remove(out)
                elif first["kind"] == "video" and after > (1 - vidlib.MIN_SAVING) * before:
                    set_all(group, status="kept", detail="Already efficient — left as it is.", before=before, after=None)
                    os.remove(out)
                else:
                    q = f" · quality check passed ({score:.0f} dB)" if score else ""
                    set_all(group, status="ready", detail="Ready to replace" + q, before=before, after=after, out=out,
                            file=os.path.basename(out), psnr=round(score, 1) if score else None)
            except Exception as e:
                set_all(group, status="error", detail=str(e)[:200])
            finally:
                if temp:
                    try:
                        os.remove(src_path)
                    except OSError:
                        pass
            done += 1
            self.activity("Preparing videos", f"{done} of {total} ready", done / max(1, total))

        await asyncio.gather(*(one(g) for g in by_hash.values()))
        st["status"] = "prepared"
        ready = sum(1 for v in st["items"] if v["status"] == "ready")
        self.activity("Videos prepared", f"{ready} ready to replace in Figma", 1)
        return {"ready": ready}

    async def a_videos_place(self, p):
        return await self._exclusive("replacing videos", self._videos_place(p.get("keys")))

    async def _videos_place(self, keys=None):
        st = self.state["videos"]
        ready = [v for v in st["items"] if v["status"] == "ready" and (not keys or v["key"] in keys)]
        if not ready:
            return {"placed": 0}
        await self._ensure_version()
        info = (self.state["connections"]["plugin"].get("file") or {})
        guided = not info.get("canCreateVideo")
        placed = 0
        for n, v in enumerate(ready, 1):
            v.update(status="placing", detail="Waiting for you to drag it in" if guided else "Uploading to Figma")
            self.activity(f"Replacing {'GIF' if v['kind'] == 'gif' else 'video'} {n} of {len(ready)}",
                          f"Slide {v['slide']} · {v['name']}", (n - 1) / len(ready))
            if guided and not self.demo:
                reveal(v["out"])

            def on_prog(d, v=v):
                if d.get("phase") == "guided":
                    v["detail"] = "Waiting for you to drag it in"
                    if not self.demo:
                        reveal(v["out"])
                    self.changed()
            try:
                r = await self.plugin_call("placeVideo", {
                    "nodeId": v["nodeId"], "fillIndex": v["fillIndex"], "url": f"/out/{v['file']}",
                    "fileName": v["file"], "kind": v["kind"], "slideIndex": v["slide"], "index": n,
                    "total": len(ready), "forceGuided": guided}, on_prog)
            except Exception as e:
                v.update(status="error", detail=str(e))
                continue
            if r.get("skipped"):
                v.update(status="ready", detail="Skipped — still ready to replace", skipped=True)
                continue
            if r.get("mode") == "guided":
                guided = True
            placed += 1
            v.update(status="done", detail="Replaced" + (" (check playback)" if not r.get("playbackApplied") else ""),
                     playbackApplied=r.get("playbackApplied"), newNodeId=r.get("nodeId"), mode=r.get("mode"))
            self._log_undo([{"type": "video", "ok": True, "nodeId": v["nodeId"], "newNodeId": r.get("nodeId"),
                             "name": v["name"], "file": v["out"], "mode": r.get("mode")}])
        self._update_totals()
        for ph in (self.state.get("auto") or {}).get("phases") or []:
            if ph["id"] == "videos" and ph["status"] == "done":
                ph["detail"] = self._videos_summary()
        self.activity("Videos replaced", f"{placed} of {len(ready)} done", 1)
        return {"placed": placed}

    async def a_videos_reveal(self, p):
        v = next((v for v in self.state["videos"]["items"] if v["key"] == p["key"]), None)
        if v and v.get("out"):
            reveal(v["out"])

    # --------------------------------------------------------------- spacing
    async def a_spacing_toggle(self, p):
        for g in self.state["spacing"]["grids"]:
            if g["id"] == p.get("id"):
                g["selected"] = not g["selected"]
        self.changed()

    async def a_spacing_run(self, p):
        return await self._exclusive("evening out spacing", self._spacing())

    async def _spacing(self):
        st = self.state["spacing"]
        chosen = [g for g in st["grids"] if g["selected"] and g["status"] == "pending"]
        if not chosen:
            return {"grids": 0}
        await self._ensure_version()
        res = await self.plugin_call("applySpacing", {"grids": [{"plan": g["plan"]} for g in chosen]})
        self._log_undo(res.get("log", []))
        for g in chosen:
            g["status"] = "done"
        self.activity("Spacing evened out", f"{len(chosen)} grid{'s' if len(chosen) != 1 else ''} now use{'s' if len(chosen) == 1 else ''} {self.gap():g} px gaps", 1)
        return {"grids": len(chosen)}

    # ------------------------------------------------------------- alignment
    async def a_align_scan(self, p):
        return await self._exclusive("checking alignment", self._align_scan(p))

    async def _align_scan(self, p):
        st = self.state["align"] = {"status": "running", "items": []}
        self.activity("Checking alignment", "Finding margins, headers and footers…", None)
        try:
            r = await self.plugin_call("alignmentScan", {"snapTol": p.get("snapTol", 12), "hfTol": p.get("hfTol", 40)})
        except Exception as e:
            st.update(status="error", error=str(e))
            raise
        items = [{**i, "key": i["id"], "selected": True, "status": "pending"} for i in r["plan"]]
        st.update(status="ready", guides=r["guides"], items=items, layers=r["layers"])
        g = r["guides"]
        self.activity("Alignment checked", f"{len(items)} layer{'s' if len(items) != 1 else ''} slightly out of line"
                      + (f" · margins {g['left']:g} px" if g.get("left") is not None else ""))
        return {"count": len(items)}

    async def a_align_toggle(self, p):
        for i in self.state["align"]["items"]:
            if p.get("all") is not None:
                if i["status"] == "pending":
                    i["selected"] = bool(p["all"])
            elif i["key"] == p.get("key") or (p.get("role") and i["role"] == p["role"] and i["status"] == "pending"):
                i["selected"] = (not i["selected"]) if not p.get("role") else bool(p.get("value"))
        self.changed()

    async def a_align_run(self, p):
        return await self._exclusive("aligning layers", self._align_run())

    async def _align_run(self):
        st = self.state["align"]
        chosen = [i for i in st["items"] if i["selected"] and i["status"] == "pending"]
        if not chosen:
            return {"moved": 0}
        await self._ensure_version()
        res = await self.plugin_call("applyMoves", {"moves": [
            {"id": i["id"], "dx": i["after"]["x"] - i["before"]["x"], "dy": i["after"]["y"] - i["before"]["y"]} for i in chosen]})
        log = res.get("log", [])
        self._log_undo(log)
        ok = {e["nodeId"] for e in log if e.get("ok")}
        for i in chosen:
            i["status"] = "done" if i["id"] in ok else "error"
        self._update_totals()
        self.activity("Layers aligned", f"{len(ok)} layer{'s' if len(ok) != 1 else ''} moved onto the layout", 1)
        return {"moved": len(ok)}

    # ------------------------------------------------------------------ undo
    async def a_undo_run(self, p):
        return await self._exclusive("undoing", self._undo())

    async def _undo(self):
        entries = [e for e in self.undo if e["type"] in ("image", "move") and not e.get("reverted")]
        if not entries:
            return {"reverted": 0}
        res = await self.plugin_call("revert", {"entries": entries})
        okids = {r["nodeId"] for r in res.get("results", []) if r.get("ok")}
        n = 0
        for e in entries:
            if e["nodeId"] in okids:
                e["reverted"] = True
                n += 1
        for i in self.state["images"]["items"]:
            if i["status"] == "done":
                i["status"], i["reason"] = "reverted", "Put back"
        for g in self.state["spacing"]["grids"]:
            if g["status"] == "done":
                g["status"] = "pending"
        for a in self.state["align"]["items"]:
            if a["status"] == "done":
                a["status"] = "pending"
        self._log_undo([])
        self._update_totals()
        gone = len(entries) - n
        self.activity("Changes undone", f"{n} change{'s' if n != 1 else ''} put back" +
                      (f" · {gone} were on layers removed since (use version history for those)" if gone else ""))
        return {"reverted": n}

    # ------------------------------------------------------- one-click optimise
    POLL = 1
    OPEN_WAIT, RETRY_WAIT = 7, 15          # seconds: Figma loading a deck, then between plugin-start attempts
    AUTO_PHASES = [("open", "Open the deck in Figma"), ("scan", "Read the deck"), ("cleanup", "Remove hidden & covered media"),
                   ("images", "Optimise images"), ("videos", "Compress videos & GIFs"),
                   ("layout", "Tidy spacing & alignment"), ("report", "Save the report")]

    def _videos_summary(self) -> str:
        items = self.state["videos"]["items"]
        ready = sum(1 for v in items if v["status"] == "ready")
        done = sum(1 for v in items if v["status"] == "done")
        missing = sum(1 for v in items if v["status"] == "not_found")
        bits = [f"{done} replaced"] if done else []
        if ready:
            bits.append(f"{ready} ready to drag in")
        if missing:
            bits.append(f"{missing} original{'s' if missing != 1 else ''} not found")
        return " · ".join(bits) or "Nothing to change"

    def _auto_set(self, pid, status, detail=""):
        for ph in self.state["auto"]["phases"]:
            if ph["id"] == pid:
                ph.update(status=status, detail=detail)
        self.state["auto"]["phase"] = pid
        self.changed()

    async def a_auto_run(self, p):
        """Everything, in the right order, with one pause before anything is deleted.
        p.url: a Figma share link; the app opens it and starts the plugin itself."""
        return await self._exclusive("optimising the deck", self._auto(p.get("url") or ""))

    @staticmethod
    def _norm_name(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", (s or "").lower())

    def _deck_matches(self, info: dict, key: Optional[str], slug: str, since: float) -> bool:
        if not info:
            return False
        if key and info.get("fileKey"):
            return info["fileKey"] == key
        if slug and info.get("name"):
            return self._norm_name(info["name"]) == self._norm_name(slug)
        # Can't prove which deck it is (development plugins aren't told the file key, and the
        # link has no name): only trust a connection that arrived after we opened the deck.
        return since > 0 and info.get("_at", 0) >= since

    async def _open_deck(self, url: str) -> bool:
        """Make sure the plugin is running in the deck at `url`, opening it and starting the plugin if needed."""
        key = parse_file_key(url)
        m = re.search(r"/[A-Za-z0-9]{10,}/([^/?#]+)", url)
        slug = (m.group(1) if m else "").replace("-", " ")
        plug = self.state["connections"]["plugin"]
        if plug.get("connected") and self._deck_matches(plug.get("file") or {}, key, slug, 0):
            self._auto_set("open", "done", "Already open")
            return True
        self.state["deck"] = {"url": url, "key": key, "name": slug or None}
        if self.demo:
            if plug.get("connected"):
                self._auto_set("open", "done", "Demo: using the open sample deck")
                return True
            raise RuntimeError("Open the sample deck in the plugin harness first.")
        if plug.get("connected"):
            # A plugin is open somewhere but we can't prove it's this deck: close it, so the
            # connection that follows opening the deck is unambiguous.
            try:
                await self.plugin_call("close", {}, timeout=4)
            except Exception:
                pass
            await asyncio.sleep(1)
        since = time.time()
        self._auto_set("open", "running", "Opening the deck in Figma…")
        if not await self.loop.run_in_executor(self.io_pool, figma_desktop.open_deck, url):
            self._auto_set("open", "waiting", "Open this deck in Figma and run Plugins › Development › Steelworks Media Optimiser.")
        else:
            await asyncio.sleep(self.OPEN_WAIT)       # let Figma load the file
        attempts, deadline = 0, time.time() + 300
        while time.time() < deadline and not self._auto_stop:
            plug = self.state["connections"]["plugin"]
            if plug.get("connected") and self._deck_matches(plug.get("file") or {}, key, slug, since):
                self._auto_set("open", "done", f"“{(plug.get('file') or {}).get('name', 'Deck')}” is open")
                return True
            if attempts < 3 and time.time() - since > self.OPEN_WAIT + attempts * self.RETRY_WAIT:
                attempts += 1
                self._auto_set("open", "running", "Starting the plugin in Figma…")
                ok, msg = await self.loop.run_in_executor(self.io_pool, figma_desktop.start_plugin)
                if not ok and msg:
                    self._auto_set("open", "waiting", msg)
            elif attempts >= 3:
                self._auto_set("open", "waiting", "Almost there: in Figma, choose Plugins › Development › Steelworks Media Optimiser "
                                                  "(and click Connect if it asks).")
            await asyncio.sleep(self.POLL)
        raise RuntimeError("The deck didn't connect. Open it in Figma, run the plugin, then try again.")

    async def a_auto_continue(self, p):
        fut = getattr(self, "_auto_gate", None)
        if fut and not fut.done():
            fut.set_result(bool(p.get("remove", True)))

    async def a_auto_stop(self, p):
        self._auto_stop = True
        if getattr(self, "_stop_evt", None):
            self._stop_evt.set()
        fut = getattr(self, "_auto_gate", None)
        if fut and not fut.done():
            fut.set_result(False)
        self.state["auto"]["stopping"] = True
        self.changed()

    async def a_auto_reset(self, p):
        if not self._busy:
            self.state["auto"] = {"status": "idle"}
            self.changed()

    async def _auto(self, url: str = ""):
        self._auto_stop = False
        self._stop_evt = asyncio.Event()
        A = self.state["auto"] = {"status": "running", "phase": "scan", "started": time.time(),
                                  "phases": [{"id": i, "label": l, "status": "pending", "detail": ""} for i, l in self.AUTO_PHASES]}
        self.changed()

        async def step(pid, coro_fn):
            if self._auto_stop:
                self._auto_set(pid, "skipped", "Stopped")
                return None
            self._auto_set(pid, "running")
            task = asyncio.ensure_future(coro_fn())
            stop = asyncio.ensure_future(self._stop_evt.wait())
            try:
                await asyncio.wait({task, stop}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                stop.cancel()
            if not task.done():
                task.cancel()
                try:
                    await task
                except BaseException:
                    pass
                self._auto_set(pid, "skipped", "Stopped")
                return None
            try:
                return task.result()
            except asyncio.CancelledError:
                self._auto_set(pid, "skipped", "Stopped")
                return None
            except Exception as e:
                self._auto_set(pid, "error", str(e)[:200])
                return None

        # 0. Open the deck (and start the plugin) when a link was given
        if url:
            try:
                ok = await step("open", lambda: self._open_deck(url))
                if ok is None:
                    raise RuntimeError(next(p for p in A["phases"] if p["id"] == "open")["detail"] or "Stopped")
            except Exception as e:
                if next(p for p in A["phases"] if p["id"] == "open")["status"] != "skipped":
                    self._auto_set("open", "error", str(e)[:200])
                for ph in A["phases"]:
                    if ph["status"] == "pending":
                        ph.update(status="skipped", detail="Stopped" if self._auto_stop else "")
                A["status"] = "done" if self._auto_stop else "error"
                self.changed()
                return A
        else:
            if not self.state["connections"]["plugin"].get("connected"):
                self._auto_set("open", "error", "Paste the deck’s link, or open it in Figma and run the plugin.")
                A["status"] = "error"
                self.changed()
                return A
            self._auto_set("open", "done", "Using the deck open in Figma")

        # 1. Read
        r = await step("scan", self._scan)
        if r is None:
            for ph in A["phases"]:
                if ph["status"] == "pending":
                    ph.update(status="skipped", detail="Stopped" if self._auto_stop else "")
            A["status"] = "done" if self._auto_stop else "error"
            self.changed()
            return A
        s_ = self.state["scan"]["summary"]
        self._auto_set("scan", "done", f"{s_['slides']} slides · {s_['images']} images · {s_['videos'] + s_['gifs']} videos & GIFs")

        # 2. Clean-up: the one question
        items = [i for i in self.state["cleanup"]["items"] if i["status"] == "pending"]
        if self._auto_stop:
            self._auto_set("cleanup", "skipped", "Stopped")
        elif not items:
            self._auto_set("cleanup", "done", "Nothing hidden or covered")
        elif self._auto_stop:
            self._auto_set("cleanup", "skipped", "Stopped")
        else:
            self._auto_set("cleanup", "waiting", f"{len(items)} layer{'s' if len(items) != 1 else ''} nobody can see")
            A["status"] = "waiting"
            self._auto_gate = self.loop.create_future()
            self.activity("Your OK, please", "Check the layers that will be removed, then continue.")
            remove = await self._auto_gate
            A["status"] = "running"
            if self._auto_stop:
                self._auto_set("cleanup", "skipped", "Stopped")
            elif remove and any(i["selected"] for i in items):
                self._auto_set("cleanup", "running")
                try:
                    res = await self._cleanup()
                    self._auto_set("cleanup", "done", f"{res['removed']} removed")
                except Exception as e:
                    self._auto_set("cleanup", "error", str(e)[:200])
            else:
                self._auto_set("cleanup", "skipped", "Kept everything")

        # 3. Images
        if await step("images", self._images) is not None:
            t = self.state["images"].get("totals") or {}
            self._auto_set("images", "done", f"{t.get('done', 0)} swapped · {reportlib.mb(t.get('before'))} → {reportlib.mb(t.get('after'))}")

        # 4. Videos & GIFs: compress; place automatically only where Figma allows it
        if not (self.ffmpeg and self.ffprobe):
            self._auto_set("videos", "skipped", "Video tools missing")
        elif not self.state["videos"]["items"]:
            self._auto_set("videos", "done", "No videos or GIFs")
        else:
            prep = await step("videos", self._videos_prepare)
            if prep is not None:
                can_auto = (self.state["connections"]["plugin"].get("file") or {}).get("canCreateVideo")
                if can_auto and not self._auto_stop:
                    await self._videos_place()
                self._auto_set("videos", "done", self._videos_summary())

        # 5. Layout: spacing + alignment
        async def layout():
            g = await self._spacing()
            await self._align_scan({})
            a = await self._align_run()
            return {"grids": g.get("grids", 0), "moved": a.get("moved", 0)}
        lay = await step("layout", layout)
        if lay is not None:
            self._auto_set("layout", "done", f"{lay['grids']} grid{'s' if lay['grids'] != 1 else ''} evened · {lay['moved']} layer{'s' if lay['moved'] != 1 else ''} aligned")

        # 6. Report (saved quietly; shown on the summary)
        rep = await step("report", lambda: self.a_report_export({"open": False}))
        if rep is not None:
            self._auto_set("report", "done", "Saved")
        self._update_totals()
        r = self.state["report"]
        A.update(status="done", finished=time.time(), before=r.get("before"), after=r.get("after"),
                 needsHand=len(self._replace_rows()))
        saved = (r.get("before") or 0) - (r.get("after") or 0)
        self.activity("Deck optimised", f"{reportlib.mb(r.get('before'))} → {reportlib.mb(r.get('after'))} · {reportlib.mb(saved)} saved", 1)
        return A

    async def a_focus(self, p):
        return await self.plugin_call("focus", {"nodeId": p["nodeId"]}, timeout=20)

    # ---------------------------------------------------------------- report
    def _update_totals(self):
        imgs = self.state["images"]["items"]
        ib = sum(i["before"] or 0 for i in imgs)
        ia = sum((i["after"] if i["status"] == "done" else 0 if i["status"] == "removed" else (i["before"] or 0))
                 for i in imgs)
        self.state["images"]["totals"] = {"before": ib, "after": ia,
                                          "done": sum(1 for i in imgs if i["status"] == "done")}
        vids = self.state["videos"]["items"]
        seen, vb, va = set(), 0, 0
        for v in vids:
            k = v["kind"] + v["hash"]
            if k in seen or not v.get("before"):
                continue
            seen.add(k)
            vb += v["before"]
            va += v["after"] if v["status"] == "done" and v.get("after") else (0 if v["status"] == "removed" else v["before"])
        self.state["report"] = {"before": ib + vb, "after": ia + va, "imagesBefore": ib, "imagesAfter": ia,
                                "videosBefore": vb, "videosAfter": va,
                                "removed": sum(1 for c in self.state["cleanup"]["items"] if c["status"] == "done"),
                                "grids": sum(1 for g in self.state["spacing"]["grids"] if g["status"] == "done"),
                                "aligned": sum(1 for a in self.state["align"]["items"] if a["status"] == "done"),
                                "replaceCount": len(self._replace_rows()),
                                **{k: v for k, v in self.state["report"].items() if k in ("exportedTo", "exportedLabel")}}
        self.changed()

    def _replace_rows(self) -> list[dict]:
        rows = []
        for i in self.state["images"]["items"]:
            if i["status"] in ("error", "manual"):
                rows.append({"Slide": ", ".join(map(str, i["slides"])), "Layer": ", ".join(i["names"]), "Type": "Image",
                             "Why it needs a person": i.get("reason", ""),
                             "What to do": "Export a smaller copy (about " + (i.get("target") or "1.5× display size") +
                                           ") and use Replace image in Figma."})
        for v in self.state["videos"]["items"]:
            what = None
            if v["status"] == "not_found":
                what = "Find the original video, compress it (1080p H.264, CRF 22) and drop it in place of this layer."
            elif v["status"] == "error":
                what = "Try again, or compress the source by hand."
            elif v["status"] == "ready":
                what = "Drag the prepared file onto the slide and replace this layer."
            elif v["status"] == "done" and not v.get("playbackApplied"):
                what = ("Check playback: loop on, sound off, autoplay on, controls off." if v["kind"] == "gif"
                        else "Check playback settings match the original (loop, sound, autoplay, controls).")
            if what:
                rows.append({"Slide": v["slide"], "Layer": v["name"], "Type": "GIF" if v["kind"] == "gif" else "Video",
                             "Why it needs a person": v.get("detail", ""), "What to do": what,
                             "Source file": v.get("source", ""), "Prepared file": v.get("out", "")})
        for c in self.state["cleanup"]["items"]:
            if c["status"] == "error":
                rows.append({"Slide": c["slideIndex"], "Layer": c["name"], "Type": "Clean-up",
                             "Why it needs a person": c.get("reason", ""), "What to do": "Delete this layer by hand."})
        return rows

    async def a_report_export(self, p):
        self._update_totals()
        deck = self.state.get("deck") or {}
        docs = (os.path.join(work_dir(), "reports") if self.demo
                else os.path.join(os.path.expanduser("~"), "Documents", APP_NAME))
        folder = os.path.join(docs, f"{reportlib.safe_name(deck.get('name') or 'Deck')} {_dt.datetime.now():%Y-%m-%d %H%M}")
        rep = self.state["report"]
        img_rows = [{"Slide": ", ".join(map(str, i["slides"])), "Layer": ", ".join(i["names"])[:80],
                     "Size": f"{i['src']} → {i.get('target') or ''}", "Before": reportlib.mb(i["before"]),
                     "After": reportlib.mb(i["after"]) if i["status"] == "done" else "—", "Result": i.get("reason") or i["status"]}
                    for i in self.state["images"]["items"] if i["status"] != "skipped"]
        vid_rows = [{"Slide": v["slide"], "Layer": v["name"], "Type": v["kind"].upper(),
                     "Before": reportlib.mb(v.get("before")), "After": reportlib.mb(v.get("after")),
                     "Result": v.get("detail", "")} for v in self.state["videos"]["items"]]
        cl_rows = [{"Slide": c["slideIndex"], "Layer": c["name"], "Reason": c["reason"],
                    "Result": {"done": "Removed", "pending": "Kept"}.get(c["status"], c["status"])}
                   for c in self.state["cleanup"]["items"]]
        sp_rows = [{"Slide": g["slideIndex"], "Group": g["parentName"], "Grid": f"{g['cols']}×{g['rows']}",
                    "Gaps before": ", ".join(f"{x:g}" for x in (g["gapsX"] + g["gapsY"])),
                    "Result": f"{self.gap():g} px" if g["status"] == "done" else "Unchanged"} for g in self.state["spacing"]["grids"]]
        al_rows = [{"Slide": a["slide"], "Layer": a["name"], "Kind": a["role"].title(), "Why": a["reason"],
                    "Move": f"{a['after']['x'] - a['before']['x']:+g}, {a['after']['y'] - a['before']['y']:+g} px",
                    "Result": "Aligned" if a["status"] == "done" else "Unchanged"} for a in self.state["align"]["items"]]
        sections = [
            {"title": "Images", "summary": f"{reportlib.mb(rep['imagesBefore'])} → {reportlib.mb(rep['imagesAfter'])}",
             "rows": img_rows, "cols": ["Slide", "Layer", "Size", "Before", "After", "Result"]},
            {"title": "Videos & GIFs", "summary": f"{reportlib.mb(rep['videosBefore'])} → {reportlib.mb(rep['videosAfter'])}",
             "rows": vid_rows, "cols": ["Slide", "Layer", "Type", "Before", "After", "Result"]},
            {"title": "Hidden & covered layers", "summary": f"{rep['removed']} removed", "rows": cl_rows,
             "cols": ["Slide", "Layer", "Reason", "Result"]},
            {"title": "Spacing", "summary": f"{rep['grids']} grid(s) set to {self.gap():g} px", "rows": sp_rows,
             "cols": ["Slide", "Group", "Grid", "Gaps before", "Result"]},
            {"title": "Alignment", "summary": f"{rep.get('aligned', 0)} layer(s) aligned to the layout",
             "rows": al_rows, "cols": ["Slide", "Layer", "Kind", "Why", "Move", "Result"]},
        ]
        paths = await self.loop.run_in_executor(self.io_pool, reportlib.export_bundle, folder, deck,
                                                {"before": rep["before"], "after": rep["after"]}, sections,
                                                self._replace_rows(), self.undo)
        self.state["report"]["exportedTo"] = folder
        folder = os.path.normpath(folder)
        home = os.path.normpath(os.path.expanduser("~"))
        if self.demo:
            label = os.path.join("Demo reports", os.path.basename(folder))
        elif os.path.normcase(folder).startswith(os.path.normcase(home) + os.sep):
            label = "~" + folder[len(home):]
        else:
            label = folder
        self.state["report"]["exportedLabel"] = label
        self.changed()
        if p.get("open", not self.demo):
            open_path(paths["report"])
        return paths

    def _replacement_files(self) -> list[dict]:
        """Prepared, already-compressed files that still need placing by hand."""
        out = []
        for v in self.state["videos"]["items"]:
            if v["status"] == "ready" and v.get("out") and os.path.exists(v["out"]):
                out.append({"src": v["out"], "slide": v["slide"], "layer": v["name"], "type": "GIF" if v["kind"] == "gif" else "Video",
                            "why": "Not placed yet" + (" (skipped)" if v.get("skipped") else ""), "ext": "mp4"})
        for i in self.state["images"]["items"]:
            if i["status"] in ("manual", "error") and i.get("file") and os.path.exists(os.path.join(self.out_dir, i["file"])):
                out.append({"src": os.path.join(self.out_dir, i["file"]), "slide": (i["slides"] or ["?"])[0],
                            "layer": ", ".join(i["names"]), "type": "Image", "why": i.get("reason", ""),
                            "ext": i["file"].rsplit(".", 1)[-1]})
        return out

    async def a_export_replacements(self, p):
        """Copy every file that couldn't be placed automatically into a folder the person picks."""
        files = self._replacement_files()
        missing = [v for v in self.state["videos"]["items"] if v["status"] == "not_found"]
        if not files and not missing:
            raise RuntimeError("Nothing needs replacing by hand — everything was done automatically.")
        deck = reportlib.safe_name((self.state.get("deck") or {}).get("name") or "Deck")
        base = None
        win = getattr(self, "window", None)
        if win and not self.demo:
            import webview
            res = await self.loop.run_in_executor(self.io_pool, lambda: win.create_file_dialog(webview.FOLDER_DIALOG))
            if not res:
                return None          # cancelled
            base = res[0] if isinstance(res, (list, tuple)) else res
        if not base:
            base = (os.path.join(work_dir(), "reports") if self.demo
                    else os.path.join(os.path.expanduser("~"), "Documents", APP_NAME))
        folder = os.path.join(base, f"{deck} – files to replace")
        os.makedirs(folder, exist_ok=True)

        def work():
            rows = []
            for f in sorted(files, key=lambda f: (int(f["slide"]) if str(f["slide"]).isdigit() else 999, f["layer"])):
                name = reportlib.safe_name(f"Slide {int(f['slide']):02d} – {f['layer']}" if str(f["slide"]).isdigit()
                                           else f["layer"])[:90] + "." + f["ext"]
                shutil.copy2(f["src"], os.path.join(folder, name))
                rows.append({"Slide": f["slide"], "Layer": f["layer"], "Type": f["type"], "Why it needs a person": f["why"],
                             "What to do": "Drag this file onto the slide in place of the layer (match its size and position).",
                             "Prepared file": name})
            for v in missing:
                rows.append({"Slide": v["slide"], "Layer": v["name"], "Type": "Video", "Why it needs a person": v.get("detail", ""),
                             "What to do": "Find the original video and run the app again, or compress it by hand.",
                             "Prepared file": ""})
            reportlib.write_replace_list(os.path.join(folder, "Replace list.csv"), rows)
            with open(os.path.join(folder, "How to replace.txt"), "w", encoding="utf-8") as fh:
                fh.write("These files are already compressed for the deck, with no visible quality loss.\n\n"
                         "For each one: open the slide named in the file name, drag the file onto the canvas,\n"
                         "line it up with the old layer (same size and position), then delete the old layer.\n\n"
                         "Replace list.csv lists every item, including any the app couldn't find.\n")
            return len(rows)
        n = await self.loop.run_in_executor(self.io_pool, work)
        self.state["report"]["replacementsTo"] = folder
        self.state["report"]["replacementsLabel"] = _short(folder) if not self.demo else os.path.join("Demo reports", os.path.basename(folder))
        self.changed()
        if not self.demo:
            open_path(folder)
        self.activity("Files to replace saved", (f"{len(files)} ready file{'s' if len(files) != 1 else ''} and " if files else "")
                      + f"a list of {n} item{'s' if n != 1 else ''} in “{os.path.basename(folder)}”")
        return {"folder": folder, "files": len(files), "rows": n}

    async def a_open(self, p):
        path = p.get("path")
        if path and os.path.exists(path):
            open_path(path)


def threading_ident() -> int:
    import threading
    return threading.get_ident()
