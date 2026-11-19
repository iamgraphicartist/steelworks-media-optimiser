"""Entry point: start the bridge on a background loop and open the native window.

  Steelworks Media Optimiser            normal start
  ... --browser                         use the default browser instead of a native window
  ... --headless                        bridge only (for tests / CI)
  ... --demo                            sample deck + sample Dropbox folder, no keys needed
"""
from __future__ import annotations

import argparse
import asyncio
import os
import socket
import sys
import threading
import webbrowser

from .config import APP_NAME, Config
from .engine import Engine
from .secure_store import SecureStore
from .server import Bridge


def _root() -> str:
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def start_bridge(engine: Engine, port: int, extra_static: dict) -> tuple[Bridge, asyncio.AbstractEventLoop]:
    loop = asyncio.new_event_loop()
    bridge = Bridge(engine, port, extra_static)
    ready = threading.Event()
    err: list = []

    def quiet(lp, ctx):  # Windows logs a reset whenever a browser tab closes its socket
        if isinstance(ctx.get("exception"), (ConnectionResetError, ConnectionAbortedError)):
            return
        lp.default_exception_handler(ctx)

    loop.set_exception_handler(quiet)

    def run():
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(bridge.start())
        except Exception as e:  # pragma: no cover
            err.append(e)
            ready.set()
            return
        ready.set()
        loop.run_forever()

    threading.Thread(target=run, name="bridge", daemon=True).start()
    ready.wait(15)
    if err:
        raise err[0]
    return bridge, loop


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog=APP_NAME)
    ap.add_argument("--demo", action="store_true", help="use the bundled sample deck and sample Dropbox folder")
    ap.add_argument("--browser", action="store_true", help="open in the default browser instead of a window")
    ap.add_argument("--headless", action="store_true", help="run the bridge only")
    ap.add_argument("--port", type=int)
    args = ap.parse_args(argv)

    cfg = Config()
    port = args.port or int(cfg.get("port"))
    root = _root()
    demo = None
    extra = {}
    if args.demo:
        demo = {"dropbox": os.path.join(root, "samples", "dropbox")}
        extra = {"/harness/": os.path.join(root, "plugin"), "/samples/": os.path.join(root, "samples")}
    if not port_free(port):
        msg = (f"Port {port} is already in use. Steelworks Media Optimiser may already be open — "
               f"check the Dock / taskbar. (Or set SMO_PORT, and update the plugin manifest.)")
        _fatal(msg, args.headless)
        return 1
    engine = Engine(cfg, SecureStore(), port, demo=demo)
    bridge, loop = start_bridge(engine, port, extra)
    url = bridge.ui_url
    print(f"{APP_NAME} is running on {url}", flush=True)
    if args.demo:
        print(f"Plugin test harness: http://localhost:{port}/harness/harness/harness.html", flush=True)

    if args.headless:
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
        return 0
    if args.browser:
        webbrowser.open(url)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
        return 0

    import webview  # pywebview: WebView2 on Windows, WKWebView on macOS
    mac = sys.platform == "darwin"
    engine.window = webview.create_window(APP_NAME, url + ("&v=1" if mac else ""), width=1200, height=800, min_size=(980, 660),
                          background_color="#F5F5F7", vibrancy=mac, transparent=False, text_select=False)
    webview.start(private_mode=False, storage_path=os.path.join(os.path.dirname(cfg.path), "webview"))
    asyncio.run_coroutine_threadsafe(bridge.stop(), loop).result(5)
    return 0


def _fatal(msg: str, headless: bool) -> None:
    print(msg, file=sys.stderr)
    if headless:
        return
    try:
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, msg, APP_NAME, 0x40)
        elif sys.platform == "darwin":
            import subprocess
            subprocess.run(["osascript", "-e", f'display alert "{APP_NAME}" message "{msg}"'])
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main())
