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
import subprocess
import sys
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional

from PIL import Image

from . import images as imglib
from . import report as reportlib
from . import video as vidlib
from .config import APP_NAME, Config, data_dir, work_dir
from .figma_rest import FigmaRest, parse_file_key
from .hashing import sha1_file
from .oauth import DropboxOAuth, FigmaOAuth, OAuthError
from .secure_store import SecureStore
from .sources import (DropboxSource, HashCache, LocalFolderSource, dropbox_desktop_root, find_source)

VERSION = "1.0.4"
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
            "version": VERSION, "demo": bool(demo), "pairCode": self.pair_code, "pairRequest": False,
            "connections": {}, "deck": {}, "activity": {"title": "Ready", "detail": "", "progress": None},
            "scan": {"status": "idle"}, "images": {"status": "idle", "items": []},
            "cleanup": {"status": "idle", "items": []}, "videos": {"status": "idle", "items": []},
            "spacing": {"status": "idle", "grids": []}, "align": {"status": "idle", "items": []}, "report": {}, "undo": {"count": 0, "reversible": 0},
        }
        self._refresh_connections()

    # ------------------------------------------------------------- plumbing
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
        }
        if self.demo:
            self.state["connections"]["figma"].update({"signedIn": True, "user": {"handle": "Demo user"}, "demo": True})
            self.state["connections"]["dropbox"].update({"signedIn": False, "demo": True,
                                                         "account": {"name": "Sample folder", "team": "Demo"},
                                                         "localRoot": self.demo.get("dropbox"),
                                                         "localRootShort": "samples/dropbox (demo)"})
        self.changed()

    # ------------------------------------------------------------- plugin RPC
    def plugin_connected(self, send, info: Optional[dict]) -> None:
        self.plugin_send = send
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

    async def plugin_call(self, method: str, params: Optional[dict] = None, on_progress=None, timeout: float = 3600):
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
        out["figma_client_secret_set"] = bool(self.figma_auth.client_secret())
        out["sources"] = {k: self.cfg.source_of(k) for k in keys}
        out["dataDir"] = "the app’s data folder" if self.demo else _short(data_dir())
        out["port"] = self.port
        out["redirects"] = {"figma": self.figma_auth.redirect_uri, "dropbox": self.dropbox_auth.redirect_uri}
        out["detected"] = {"local_dropbox_root": "samples/dropbox (demo)" if self.demo else _short(dropbox_desktop_root()),
                           "ffmpeg": _short(vidlib.find_tool("ffmpeg"))}
        return out

    ALLOWED_URLS = ("https://www.figma.com/developers", "https://www.dropbox.com/developers", "https://www.dropbox.com/install", "https://brew.sh",
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
        deck = {"url": p["url"].strip(), "key": key, "name": None}
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
            inv = await self.plugin_call("inventory", {"upload": True}, prog)
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
                             "status": "pending", "detail": "Not looked for yet", "before": None, "after": None})
        for h, g in gifs.items():
            for u in g["uses"]:
                vids.append({"key": f"g:{h}:{u['nodeId']}", "kind": "gif", "hash": h, "name": u["name"],
                             "slide": u["slideIndex"], "nodeId": u["nodeId"], "fillIndex": u["fillIndex"],
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
            r = imglib.optimise(data, item["ratio"] * info["width"], item["ratio"] * info["height"])
            if r.keep:
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
                item.update(after=r.out_bytes if r.keep else None, reason=r.reason, target=r.out_size,
                            file=name, mime=r.mime, status="ready" if r.keep else "kept")
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
        if self.dropbox_auth.token():
            out.append(DropboxSource(self.dropbox_auth.access_token, self.hash_cache, local_root=local_root))
        elif local_root and os.path.isdir(local_root):
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
                set_all(group, status="searching", detail="Looking in Dropbox…")
                names = list(dict.fromkeys(v["name"] for v in group))
                res = None
                for nm in names:
                    def prog(f, g=group):
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

            def run():
                fn = vidlib.gif_to_mp4 if first["kind"] == "gif" else vidlib.compress_video
                fn(self.ffmpeg, self.ffprobe, src_path, out,
                   progress=lambda f: set_all(group, progress=f, detail=f"Compressing {round(f * 100)}%"))
            try:
                await self.loop.run_in_executor(self.vid_pool, run)
                before = os.path.getsize(src_path)
                after = os.path.getsize(out)
                if first["kind"] == "video" and after > (1 - vidlib.MIN_SAVING) * before:
                    set_all(group, status="kept", detail="Already efficient — left as it is.", before=before, after=None)
                    os.remove(out)
                else:
                    set_all(group, status="ready", detail="Ready to replace", before=before, after=after, out=out,
                            file=os.path.basename(out))
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
        self.activity("Spacing evened out", f"{len(chosen)} grid{'s' if len(chosen) != 1 else ''} now use{'s' if len(chosen) == 1 else ''} 16 px gaps", 1)
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
            if i["status"] == "error" or ("rotation" in (i.get("reason") or "")):
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
                    "Result": "16 px" if g["status"] == "done" else "Unchanged"} for g in self.state["spacing"]["grids"]]
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
            {"title": "Spacing", "summary": f"{rep['grids']} grid(s) set to 16 px", "rows": sp_rows,
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

    async def a_open(self, p):
        path = p.get("path")
        if path and os.path.exists(path):
            open_path(path)


def threading_ident() -> int:
    import threading
    return threading.get_ident()
