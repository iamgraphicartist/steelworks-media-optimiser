"""Bridge security + Dropbox client behaviour, without network or real keys."""
import asyncio
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest

TMP = tempfile.mkdtemp()
os.environ["APPDATA"] = TMP                      # keep tests out of the real profile
os.environ["XDG_DATA_HOME"] = TMP
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import aiohttp  # noqa: E402

from smo import config  # noqa: E402
from smo.engine import Engine  # noqa: E402
from smo.figma_rest import parse_file_key  # noqa: E402
from smo.oauth import pkce_pair  # noqa: E402
from smo.report import REPLACE_COLUMNS, write_replace_list  # noqa: E402
from smo.secure_store import SecureStore  # noqa: E402
from smo.server import Bridge  # noqa: E402
from smo.sources import DropboxSource, HashCache  # noqa: E402

if sys.platform == "darwin":  # don't touch the real login keychain from tests
    SecureStore._load = lambda self: getattr(self, "_mem", {})
    SecureStore._save = lambda self, d: setattr(self, "_mem", d)

PORT = 47899


class TestBridge(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        cfg = config.Config(os.path.join(TMP, "cfg.json"))
        self.engine = Engine(cfg, SecureStore(os.path.join(TMP, "s.bin")), PORT)
        self.bridge = Bridge(self.engine, PORT)
        await self.bridge.start()
        self.http = aiohttp.ClientSession()
        self.base = f"http://127.0.0.1:{PORT}"

    async def asyncTearDown(self):
        await self.http.close()
        await self.bridge.stop()

    async def _pair(self):
        ws = await self.http.ws_connect(f"{self.base}/plugin?pair={self.engine.pair_code}")
        msg = json.loads((await ws.receive()).data)
        return ws, msg

    async def test_wrong_code_is_denied(self):
        ws = await self.http.ws_connect(f"{self.base}/plugin?pair=000000" if self.engine.pair_code != "000000"
                                        else f"{self.base}/plugin?pair=111111")
        msg = json.loads((await ws.receive()).data)
        self.assertEqual(msg["type"], "denied")
        await ws.close()

    async def test_pairing_issues_token_and_rotates_code(self):
        code = self.engine.pair_code
        ws, msg = await self._pair()
        self.assertEqual(msg["type"], "paired")
        self.assertNotEqual(self.engine.pair_code, code, "a code works once")
        await ws.close()
        ws2 = await self.http.ws_connect(f"{self.base}/plugin?token={msg['token']}")
        await ws2.send_str(json.dumps({"type": "hello", "info": {"name": "Deck", "fileKey": "abc"}}))
        for _ in range(50):
            if self.engine.state["connections"]["plugin"].get("connected"):
                break
            await asyncio.sleep(0.02)
        self.assertTrue(self.engine.state["connections"]["plugin"]["connected"])
        await ws2.close()

    async def test_one_click_pairing_needs_allow(self):
        ws = await self.http.ws_connect(f"{self.base}/plugin?request=1")
        self.assertEqual(json.loads((await ws.receive()).data)["type"], "waiting")
        self.assertTrue(self.engine.state["pairRequest"])
        await self.engine.a_plugin_allow({"allow": True})
        msg = json.loads((await ws.receive()).data)
        self.assertEqual(msg["type"], "paired")
        self.assertTrue(self.engine.valid_token(msg["token"]))
        await ws.close()

    async def test_one_click_pairing_can_be_refused(self):
        ws = await self.http.ws_connect(f"{self.base}/plugin?request=1")
        await ws.receive()
        await self.engine.a_plugin_allow({"allow": False})
        self.assertEqual(json.loads((await ws.receive()).data)["type"], "denied")

    async def test_stale_token_asked_to_repair(self):
        ws = await self.http.ws_connect(f"{self.base}/plugin?token=nope")
        m = await ws.receive()
        self.assertEqual(ws.close_code, 4001)

    async def test_rpc_round_trip_with_progress(self):
        _, msg = await self._pair()
        ws = await self.http.ws_connect(f"{self.base}/plugin?token={msg['token']}")
        await ws.send_str(json.dumps({"type": "hello", "info": {}}))
        await asyncio.sleep(0.1)
        seen = []
        task = asyncio.ensure_future(self.engine.plugin_call("inventory", {"x": 1}, seen.append))
        req = {}
        while "method" not in req:   # status messages for the plugin panel come first
            req = json.loads((await ws.receive()).data)
        self.assertEqual(req["method"], "inventory")
        await ws.send_str(json.dumps({"id": req["id"], "progress": {"done": 1, "total": 2}}))
        await ws.send_str(json.dumps({"id": req["id"], "result": {"ok": True}}))
        self.assertEqual(await asyncio.wait_for(task, 3), {"ok": True})
        self.assertEqual(seen, [{"done": 1, "total": 2}])
        await ws.close()

    async def test_blob_requires_token_and_matching_hash(self):
        _, msg = await self._pair()
        data = os.urandom(5000)
        h = hashlib.sha1(data).hexdigest()
        r = await self.http.post(f"{self.base}/blob/{h}", data=data)
        self.assertEqual(r.status, 403)
        r = await self.http.post(f"{self.base}/blob/{'0' * 40}?token={msg['token']}", data=data)
        self.assertEqual(r.status, 422)
        r = await self.http.post(f"{self.base}/blob/{h}?token={msg['token']}", data=data)
        self.assertEqual(r.status, 200)
        self.assertTrue(os.path.exists(os.path.join(self.engine.blob_dir, h)))
        self.assertEqual(r.headers.get("Access-Control-Allow-Origin"), "*")

    async def test_out_blocks_traversal(self):
        _, msg = await self._pair()
        r = await self.http.get(f"{self.base}/out/..%2Fsecrets.bin?token={msg['token']}")
        self.assertIn(r.status, (400, 404))

    async def test_ui_socket_needs_launch_key(self):
        with self.assertRaises(aiohttp.WSServerHandshakeError):
            await self.http.ws_connect(f"{self.base}/ws/ui?k=wrong")
        ws = await self.http.ws_connect(f"{self.base}/ws/ui?k={self.bridge.ui_key}")
        first = json.loads((await ws.receive()).data)
        self.assertEqual(first["event"], "state")
        await ws.close()

    async def test_dns_rebinding_host_refused(self):
        r = await self.http.get(f"{self.base}/app/", headers={"Host": "evil.example:47899"})
        self.assertEqual(r.status, 403)

    async def test_private_network_preflight(self):
        r = await self.http.options(f"{self.base}/blob/{'a' * 40}")
        self.assertEqual(r.status, 204)
        self.assertEqual(r.headers.get("Access-Control-Allow-Private-Network"), "true")


# ---------------------------------------------------------------- Dropbox
class FakeResp:
    def __init__(self, status=200, body=None, raw=b""):
        self.status_code, self._body, self._raw = status, body or {}, raw
        self.headers = {"Content-Length": str(len(raw))}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body

    def iter_content(self, n):
        for i in range(0, len(self._raw), n):
            yield self._raw[i:i + n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeDropbox:
    """Answers the handful of endpoints DropboxSource uses and records headers."""

    def __init__(self, files):
        self.files, self.calls = files, []

    def post(self, url, headers=None, json=None, stream=False, timeout=None):
        route = url.split("/2/", 1)[1]
        self.calls.append((route, headers or {}, json))
        if route == "users/get_current_account":
            return FakeResp(body={"name": {"display_name": "Sam"}, "email": "s@x", "team": {"name": "Steelworks"},
                                  "root_info": {".tag": "team", "root_namespace_id": "100", "home_namespace_id": "200"}})
        if route == "sharing/list_folders":
            return FakeResp(body={"entries": [{"shared_folder_id": "300", "name": "Client share"}]})
        if route == "files/search_v2":
            ns = "300" if "namespace_id" in (headers or {}).get("Dropbox-API-Path-Root", "") else "root"
            matches = [{"metadata": {"metadata": {".tag": "file", "name": f["name"], "path_display": f["path"],
                                                  "id": f["id"], "size": len(f["data"])}}}
                       for f in self.files if f["ns"] == ns and json["query"].lower().split()[0] in f["name"].lower()]
            return FakeResp(body={"matches": matches, "has_more": False})
        if route == "files/download":
            arg = __import__("json").loads(headers["Dropbox-API-Arg"])
            f = next(f for f in self.files if f["id"] == arg["path"])
            return FakeResp(raw=f["data"])
        return FakeResp(404)


class TestDropboxSource(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.files = [
            {"id": "id:a", "ns": "root", "name": "Hero Spot.mp4", "path": "/Jobs/Old/Hero Spot.mp4", "data": b"old"},
            {"id": "id:b", "ns": "root", "name": "Hero_Spot_v2.mov", "path": "/Jobs/Final/Hero_Spot_v2.mov", "data": b"orig" * 1000},
            {"id": "id:c", "ns": "300", "name": "Hero Spot client.mp4", "path": "/Hero Spot client.mp4", "data": b"shared"},
        ]
        self.fake = FakeDropbox(self.files)
        self.src = DropboxSource(lambda: "TOKEN", HashCache(os.path.join(self.tmp, "c.json")), session=self.fake)

    def test_team_root_header_used_for_search(self):
        self.src.search("Hero Spot")
        searches = [c for c in self.fake.calls if c[0] == "files/search_v2"]
        root_hdr = json.loads(searches[0][1]["Dropbox-API-Path-Root"])
        self.assertEqual(root_hdr, {".tag": "root", "root": "100"})
        self.assertTrue(any("namespace_id" in c[1].get("Dropbox-API-Path-Root", "") for c in searches),
                        "unmounted shared folders are searched too")

    def test_finds_unsynced_and_shared_candidates(self):
        names = sorted(c.name for c in self.src.search("Hero Spot"))
        self.assertEqual(names, ["Hero Spot client.mp4", "Hero Spot.mp4", "Hero_Spot_v2.mov"])

    def test_download_is_hashed_while_streaming(self):
        from smo.sources import find_source
        target = hashlib.sha1(b"orig" * 1000).hexdigest()
        r = find_source("Hero Spot", target, [self.src], self.tmp)
        self.assertEqual(r.status, "confirmed")
        self.assertEqual(r.candidate.name, "Hero_Spot_v2.mov")
        self.assertTrue(r.temporary)
        with open(r.path, "rb") as f:
            self.assertEqual(hashlib.sha1(f.read()).hexdigest(), target)
        self.assertEqual(len([n for n in os.listdir(self.tmp) if n.endswith((".mp4", ".mov"))]), 1,
                         "non-matching downloads are deleted")

    def test_synced_local_copy_preferred(self):
        local = os.path.join(self.tmp, "DropboxRoot")
        os.makedirs(os.path.join(local, "Jobs", "Final"))
        with open(os.path.join(local, "Jobs", "Final", "Hero_Spot_v2.mov"), "wb") as f:
            f.write(b"orig" * 1000)
        self.src.local_root = local
        c = next(c for c in self.src.search("Hero Spot") if c.name == "Hero_Spot_v2.mov")
        path, sha, temp = self.src.fetch(c, self.tmp)
        self.assertFalse(temp)
        self.assertTrue(path.startswith(local))
        self.assertFalse(any(x[0] == "files/download" for x in self.fake.calls))


class TestSmallBits(unittest.TestCase):
    def test_parse_file_key(self):
        self.assertEqual(parse_file_key("https://www.figma.com/slides/AbCdEf1234567890XyZw12/Pitch-Deck?node-id=1-2"),
                         "AbCdEf1234567890XyZw12")
        self.assertEqual(parse_file_key("https://www.figma.com/design/abcDEF1234567890/x"), "abcDEF1234567890")
        self.assertIsNone(parse_file_key("https://example.com/nope"))

    def test_pkce(self):
        import base64
        v, c = pkce_pair()
        self.assertGreaterEqual(len(v), 43)
        self.assertEqual(c, base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode())

    def test_replace_list_csv(self):
        p = os.path.join(TMP, "r.csv")
        write_replace_list(p, [{"Slide": 3, "Layer": "Hero, cut", "Type": "Video", "What to do": "x"}])
        with open(p, encoding="utf-8-sig") as f:
            import csv
            rows = list(csv.DictReader(f))
        self.assertEqual(list(rows[0].keys()), REPLACE_COLUMNS)
        self.assertEqual(rows[0]["Layer"], "Hero, cut")

    @unittest.skipUnless(sys.platform == "win32", "DPAPI is Windows-only")
    def test_secure_store_encrypts_at_rest(self):
        p = os.path.join(TMP, "sec.bin")
        s = SecureStore(p)
        s.set("figma_token", {"access_token": "very-secret-value"})
        with open(p, "rb") as f:
            self.assertNotIn(b"very-secret-value", f.read())
        self.assertEqual(SecureStore(p).get("figma_token")["access_token"], "very-secret-value")


if __name__ == "__main__":
    unittest.main()
