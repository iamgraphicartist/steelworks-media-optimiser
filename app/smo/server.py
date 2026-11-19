"""The localhost bridge (aiohttp), bound to loopback only.

  /app/            the desktop app's UI (also loaded by the native window)
  /ws/ui           UI <-> engine (needs the per-launch UI key)
  /plugin          Figma plugin <-> engine (needs a pairing code or a paired token)
  /blob/<sha1>     plugin uploads original image bytes   (paired token)
  /out/<file>      plugin downloads prepared files        (paired token)
  /oauth/<p>/callback   OAuth redirect target
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import json
import os
import re
import secrets
import socket
import sys
from typing import Optional

from aiohttp import WSMsgType, web

from .engine import Engine

UI_DIR = os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "smo", "ui") \
    if getattr(sys, "frozen", False) else os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")
_HASH = re.compile(r"^[0-9a-f]{40}$")
_FILE = re.compile(r"^[^/\\]+$")
MAX_BLOB = 2 * 1024 ** 3

CORS = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Private-Network": "true",
        "Access-Control-Max-Age": "600"}


@web.middleware
async def cors_mw(request, handler):
    if request.method == "OPTIONS":
        return web.Response(status=204, headers=CORS)
    resp = await handler(request)
    if request.path.startswith(("/blob/", "/out/", "/demo/")):
        resp.headers.update(CORS)
    return resp


@web.middleware
async def host_guard(request, handler):
    """Refuse DNS-rebinding: only loopback Host headers are served."""
    host = (request.host or "").rsplit(":", 1)[0].strip("[]").lower()
    if host not in ("localhost", "127.0.0.1", "::1"):
        return web.Response(status=403, text="Forbidden")
    return await handler(request)


class Bridge:
    def __init__(self, engine: Engine, port: int, extra_static: Optional[dict] = None):
        self.engine, self.port = engine, port
        self.ui_key = secrets.token_urlsafe(24)
        self.extra_static = extra_static or {}
        self.app = web.Application(middlewares=[host_guard, cors_mw], client_max_size=MAX_BLOB)
        r = self.app.router
        r.add_get("/", lambda req: web.HTTPFound(f"/app/?k={self.ui_key}"))
        r.add_get("/app/", self.index)
        r.add_static("/app/", UI_DIR, show_index=False)
        r.add_get("/ws/ui", self.ws_ui)
        r.add_get("/plugin", self.ws_plugin)
        r.add_post("/blob/{hash}", self.put_blob)
        r.add_get("/out/{name}", self.get_out)
        r.add_get("/oauth/{provider}/callback", self.oauth_cb)
        for prefix, folder in self.extra_static.items():
            r.add_static(prefix, folder, show_index=False)
        if engine.demo:
            r.add_get("/demo/code", lambda req: web.json_response({"code": engine.pair_code}))
        self.runner: Optional[web.AppRunner] = None

    async def start(self):
        self.engine.attach(asyncio.get_running_loop())
        self.runner = web.AppRunner(self.app, access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", self.port).start()
        if socket.has_ipv6:
            try:  # browsers may resolve localhost to ::1 first
                await web.TCPSite(self.runner, "::1", self.port).start()
            except OSError:
                pass

    async def stop(self):
        if self.runner:
            await self.runner.cleanup()

    @property
    def ui_url(self) -> str:
        return f"http://localhost:{self.port}/app/?k={self.ui_key}"

    # ------------------------------------------------------------------ UI
    async def index(self, request):
        with open(os.path.join(UI_DIR, "index.html"), encoding="utf-8") as f:
            body = f.read()
        return web.Response(text=body, content_type="text/html", headers={"Cache-Control": "no-store"})

    async def ws_ui(self, request):
        if not secrets.compare_digest(request.query.get("k", ""), self.ui_key):
            return web.Response(status=403)
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=16 << 20)
        await ws.prepare(request)

        async def send(text):
            if not ws.closed:
                await ws.send_str(text)
        self.engine.listeners.add(send)
        await ws.send_str(json.dumps({"event": "state", "data": self.engine.state}, default=str))
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                m = json.loads(msg.data)
                asyncio.ensure_future(self._ui_call(ws, m))
        finally:
            self.engine.listeners.discard(send)
        return ws

    async def _ui_call(self, ws, m):
        try:
            res = await self.engine.handle(m["action"], m.get("params") or {})
            out = {"id": m.get("id"), "result": res}
        except Exception as e:
            out = {"id": m.get("id"), "error": str(e) or type(e).__name__}
        if not ws.closed:
            await ws.send_str(json.dumps(out, default=str))

    # -------------------------------------------------------------- plugin
    async def ws_plugin(self, request):
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=256 << 20)
        await ws.prepare(request)
        token = self.engine.check_plugin_token(request.query.get("token"), request.query.get("pair"))
        if not token:
            if request.query.get("token"):
                await ws.close(code=4001, message=b"pair again")
            else:
                await ws.send_str(json.dumps({"type": "denied",
                                              "message": "That code didn’t match. Check the code in the app."}))
                await ws.close()
            return ws
        if request.query.get("pair"):
            await ws.send_str(json.dumps({"type": "paired", "token": token}))

        lock = asyncio.Lock()

        async def send(obj):
            async with lock:
                if not ws.closed:
                    await ws.send_str(json.dumps(obj, default=str))
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                m = json.loads(msg.data)
                if m.get("type") == "hello":
                    self.engine.plugin_connected(send, m.get("info"))
                else:
                    self.engine.plugin_message(m)
        finally:
            if self.engine.plugin_send is send:
                self.engine.plugin_disconnected()
        return ws

    async def put_blob(self, request):
        if not self.engine.valid_token(request.query.get("token")):
            return web.Response(status=403)
        h = request.match_info["hash"].lower()
        if not _HASH.match(h):
            return web.Response(status=400)
        path = os.path.join(self.engine.blob_dir, h)
        tmp = path + ".part"
        sha = hashlib.sha1()
        with open(tmp, "wb") as f:
            async for chunk in request.content.iter_chunked(1 << 20):
                sha.update(chunk)
                f.write(chunk)
        if sha.hexdigest() != h:
            os.remove(tmp)
            return web.json_response({"ok": False, "error": "hash mismatch"}, status=422)
        os.replace(tmp, path)
        return web.json_response({"ok": True})

    async def get_out(self, request):
        if not self.engine.valid_token(request.query.get("token")):
            return web.Response(status=403)
        name = request.match_info["name"]
        if not _FILE.match(name) or name.startswith("."):
            return web.Response(status=400)
        path = os.path.join(self.engine.out_dir, name)
        if not os.path.isfile(path):
            return web.Response(status=404)
        return web.FileResponse(path)

    # --------------------------------------------------------------- oauth
    async def oauth_cb(self, request):
        provider = request.match_info["provider"]
        if provider not in ("figma", "dropbox"):
            return web.Response(status=404)
        q = request.query
        if q.get("error"):
            return self._page("Sign-in cancelled", html.escape(q.get("error_description") or q["error"]), False)
        try:
            name = await self.engine.oauth_finished(provider, q.get("code", ""), q.get("state", ""))
        except Exception as e:
            return self._page("Couldn’t sign in", html.escape(str(e)), False)
        return self._page(f"Signed in to {name}", "You can close this tab and go back to Steelworks Media Optimiser.", True)

    @staticmethod
    def _page(title, text, ok):
        color = "#30d158" if ok else "#ff453a"
        icon = "✓" if ok else "!"
        body = f"""<!doctype html><meta charset=utf-8><title>{title}</title>
<style>body{{margin:0;height:100vh;display:grid;place-items:center;background:#f5f5f7;color:#1d1d1f;
font:15px -apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI Variable Text","Segoe UI",system-ui,sans-serif}}
@media(prefers-color-scheme:dark){{body{{background:#000;color:#f5f5f7}}.c{{background:#1c1c1e!important}}}}
.c{{background:#fff;border-radius:16px;padding:40px 48px;text-align:center;box-shadow:0 10px 40px rgba(0,0,0,.08);max-width:420px}}
.i{{width:56px;height:56px;border-radius:50%;background:{color};color:#fff;font-size:28px;display:grid;place-items:center;margin:0 auto 16px}}
h1{{font-size:22px;margin:0 0 8px}}p{{color:#86868b;margin:0}}</style>
<div class=c><div class=i>{icon}</div><h1>{title}</h1><p>{text}</p></div>"""
        return web.Response(text=body, content_type="text/html")
