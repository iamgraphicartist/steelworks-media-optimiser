"""One-click 'Optimise deck': order of steps, the single approval pause, stopping."""
import asyncio
import os
import sys
import tempfile
import unittest

TMP = tempfile.mkdtemp()
os.environ["APPDATA"] = TMP
os.environ["XDG_DATA_HOME"] = TMP
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from smo import config  # noqa: E402
from smo.engine import Engine  # noqa: E402
from smo.secure_store import SecureStore  # noqa: E402

if sys.platform == "darwin":
    SecureStore._load = lambda self: getattr(self, "_mem", {})
    SecureStore._save = lambda self, d: setattr(self, "_mem", d)


class FakeEngine(Engine):
    """Replaces each step with a recorder so only the orchestration is tested."""

    def __init__(self, *a, cleanup_items=1, **kw):
        super().__init__(*a, **kw)
        self.calls = []
        self._cleanup_items = cleanup_items

    async def _scan(self):
        self.calls.append("scan")
        self.state["scan"] = {"status": "done", "summary": {"slides": 2, "images": 3, "videos": 1, "gifs": 0}}
        self.state["cleanup"] = {"status": "ready", "items": [
            {"key": f"k{i}", "status": "pending", "selected": True, "nodeId": f"n{i}"} for i in range(self._cleanup_items)]}
        self.state["videos"] = {"status": "ready", "items": [{"status": "pending", "kind": "video", "hash": "h", "slide": 1, "name": "Film", "detail": ""}]}
        return {}

    async def _cleanup(self):
        self.calls.append("cleanup")
        return {"removed": sum(1 for i in self.state["cleanup"]["items"] if i["selected"])}

    async def _images(self):
        self.calls.append("images")
        self.state["images"]["totals"] = {"done": 2, "before": 100, "after": 10}
        return {}

    async def _videos_prepare(self):
        self.calls.append("videos")
        self.state["videos"]["items"][0]["status"] = "ready"
        return {}

    async def _videos_place(self, keys=None):
        self.calls.append("place")
        return {}

    async def _spacing(self):
        self.calls.append("spacing")
        return {"grids": 1}

    async def _align_scan(self, p):
        self.calls.append("align_scan")
        return {}

    async def _align_run(self):
        self.calls.append("align")
        return {"moved": 2}

    async def a_report_export(self, p):
        self.calls.append("report")
        return {}


class TestAuto(unittest.IsolatedAsyncioTestCase):
    async def make(self, **kw):
        e = FakeEngine(config.Config(os.path.join(TMP, "cfg.json")), SecureStore(os.path.join(TMP, "s.bin")), 47897, **kw)
        e.attach(asyncio.get_running_loop())
        e.ffmpeg = e.ffprobe = "ffmpeg"
        e.state["connections"]["plugin"] = {"connected": True, "file": {"name": "Deck", "_at": 1}}
        return e

    async def wait_for(self, e, status):
        for _ in range(200):
            if e.state["auto"].get("status") == status:
                return
            await asyncio.sleep(0.01)
        self.fail(f"never reached {status}: {e.state['auto']}")

    async def test_runs_everything_in_order_after_one_approval(self):
        e = await self.make()
        task = asyncio.ensure_future(e.a_auto_run({}))
        await self.wait_for(e, "waiting")
        self.assertEqual(e.calls, ["scan"], "nothing is removed or changed before the approval")
        await e.a_auto_continue({"remove": True})
        await task
        self.assertEqual(e.calls, ["scan", "cleanup", "images", "videos", "spacing", "align_scan", "align", "report"])
        self.assertEqual(e.state["auto"]["status"], "done")
        self.assertEqual({p["id"]: p["status"] for p in e.state["auto"]["phases"]},
                         {"open": "done", "scan": "done", "cleanup": "done", "images": "done", "videos": "done", "layout": "done", "report": "done"})
        videos = next(p for p in e.state["auto"]["phases"] if p["id"] == "videos")
        self.assertIn("ready to drag in", videos["detail"])

    async def test_keep_everything_skips_removal(self):
        e = await self.make()
        task = asyncio.ensure_future(e.a_auto_run({}))
        await self.wait_for(e, "waiting")
        await e.a_auto_continue({"remove": False})
        await task
        self.assertNotIn("cleanup", e.calls)
        self.assertEqual(next(p for p in e.state["auto"]["phases"] if p["id"] == "cleanup")["status"], "skipped")

    async def test_no_question_when_nothing_is_hidden(self):
        e = await self.make(cleanup_items=0)
        await asyncio.wait_for(e.a_auto_run({}), 5)
        self.assertEqual(e.state["auto"]["status"], "done")
        self.assertNotIn("cleanup", e.calls)

    async def test_videos_placed_automatically_when_figma_allows(self):
        e = await self.make(cleanup_items=0)
        e.state["connections"]["plugin"] = {"connected": True, "file": {"name": "Deck", "canCreateVideo": True}}
        await asyncio.wait_for(e.a_auto_run({}), 5)
        self.assertIn("place", e.calls)

    async def test_stop_skips_the_rest(self):
        e = await self.make()
        task = asyncio.ensure_future(e.a_auto_run({}))
        await self.wait_for(e, "waiting")
        await e.a_auto_stop({})
        await task
        self.assertEqual(e.calls, ["scan"])
        self.assertTrue(all(p["status"] in ("done", "skipped") for p in e.state["auto"]["phases"]))

    async def test_a_failing_step_doesnt_stop_the_others(self):
        e = await self.make(cleanup_items=0)

        async def boom():
            raise RuntimeError("Figma refused")
        e._images = boom
        await asyncio.wait_for(e.a_auto_run({}), 5)
        ph = {p["id"]: p for p in e.state["auto"]["phases"]}
        self.assertEqual(ph["images"]["status"], "error")
        self.assertIn("Figma refused", ph["images"]["detail"])
        self.assertEqual(ph["layout"]["status"], "done")


if __name__ == "__main__":
    unittest.main()


class TestOpenDeck(unittest.IsolatedAsyncioTestCase):
    """Pick a deck by link: the app opens it in Figma, starts the plugin, waits for that deck."""

    async def asyncSetUp(self):
        from smo import figma_desktop
        self.fd = figma_desktop
        self._orig = (figma_desktop.open_deck, figma_desktop.start_plugin)
        self.e = FakeEngine(config.Config(os.path.join(TMP, "cfg2.json")), SecureStore(os.path.join(TMP, "s2.bin")), 47896,
                            cleanup_items=0)
        self.e.attach(asyncio.get_running_loop())
        self.e.ffmpeg = self.e.ffprobe = "ffmpeg"
        self.e.OPEN_WAIT, self.e.RETRY_WAIT, self.e.POLL = 0.01, 0.05, 0.01
        self.e.plugin_send = None
        self.events = []

    async def asyncTearDown(self):
        self.fd.open_deck, self.fd.start_plugin = self._orig

    async def test_opens_starts_plugin_and_waits_for_the_right_deck(self):
        loop = asyncio.get_running_loop()

        def open_deck(url):
            self.events.append(("open", url))
            return True

        def start_plugin(name="x"):
            self.events.append(("start",))
            # Figma connects the plugin a moment later - first an unrelated deck, then the right one.
            loop.call_soon_threadsafe(self.e.plugin_connected, None, {"name": "Other deck"})
            loop.call_soon_threadsafe(lambda: loop.call_later(0.05, self.e.plugin_connected, None, {"name": "Spring Pitch Deck"}))
            return True, ""
        self.fd.open_deck, self.fd.start_plugin = open_deck, start_plugin
        url = "https://www.figma.com/slides/AbCdEf1234567890XyZw12/Spring-Pitch-Deck?node-id=1-2"
        await asyncio.wait_for(self.e.a_auto_run({"url": url}), 10)
        self.assertEqual(self.events[0], ("open", url))
        self.assertIn(("start",), self.events)
        ph = {p["id"]: p for p in self.e.state["auto"]["phases"]}
        self.assertEqual(ph["open"]["status"], "done")
        self.assertIn("Spring Pitch Deck", ph["open"]["detail"])
        self.assertEqual(self.e.calls[0], "scan", "optimising starts only once the right deck is connected")

    async def test_already_open_deck_is_used_straight_away(self):
        self.fd.open_deck = lambda url: self.fail("should not reopen")
        self.e.state["connections"]["plugin"] = {"connected": True, "file": {"name": "Spring Pitch Deck", "_at": 1}}
        await asyncio.wait_for(self.e.a_auto_run({"url": "https://www.figma.com/slides/AbCdEf1234567890XyZw12/Spring-Pitch-Deck"}), 5)
        self.assertEqual(next(p for p in self.e.state["auto"]["phases"] if p["id"] == "open")["detail"], "Already open")

    async def test_file_key_wins_over_name(self):
        info = {"fileKey": "AbCdEf1234567890XyZw12", "name": "Renamed deck"}
        self.assertTrue(self.e._deck_matches(info, "AbCdEf1234567890XyZw12", "Spring Pitch Deck", 0))
        self.assertFalse(self.e._deck_matches({"fileKey": "zzzzzzzzzzzzzzzz", "name": "Spring Pitch Deck"},
                                              "AbCdEf1234567890XyZw12", "Spring Pitch Deck", 0))

    async def test_unprovable_open_deck_is_not_reused(self):
        """Real-Figma finding: dev plugins get no file key; a key-only link has no name.
        The deck that happens to be open must not be assumed to be the chosen one."""
        info = {"name": "Some other deck", "_at": 5}
        self.assertFalse(self.e._deck_matches(info, "AbCdEf1234567890XyZw12", "", 0))
        self.assertTrue(self.e._deck_matches(info, "AbCdEf1234567890XyZw12", "", 4), "a connection after opening is trusted")
        self.assertFalse(self.e._deck_matches(info, "AbCdEf1234567890XyZw12", "", 6), "one from before opening is not")

    async def test_stop_before_the_question_never_asks(self):
        e = self.e
        e.state["connections"]["plugin"] = {"connected": True, "file": {"name": "Deck", "_at": 1}}
        e._cleanup_items = 2
        orig = e._scan

        async def scan_then_stop():
            r = await orig()
            e._auto_stop = True
            return r
        e._scan = scan_then_stop
        await asyncio.wait_for(e.a_auto_run({}), 5)
        self.assertEqual(next(p for p in e.state["auto"]["phases"] if p["id"] == "cleanup")["status"], "skipped")

    async def test_no_link_and_no_plugin_explains_what_to_do(self):
        await asyncio.wait_for(self.e.a_auto_run({}), 5)
        self.assertEqual(self.e.state["auto"]["status"], "error")
        self.assertIn("link", next(p for p in self.e.state["auto"]["phases"] if p["id"] == "open")["detail"])


class TestStopIsImmediate(unittest.IsolatedAsyncioTestCase):
    """Real-Figma finding: Stop must interrupt the running step and nothing may change the deck afterwards."""

    async def asyncSetUp(self):
        self.e = FakeEngine(config.Config(os.path.join(TMP, "cfg3.json")), SecureStore(os.path.join(TMP, "s3.bin")), 47895,
                            cleanup_items=0)
        self.e.attach(asyncio.get_running_loop())
        self.e.ffmpeg = self.e.ffprobe = "ffmpeg"
        self.e.state["connections"]["plugin"] = {"connected": True, "file": {"name": "Deck", "_at": 1}}

    async def test_stop_interrupts_a_long_step(self):
        e = self.e

        async def slow_images():
            e.calls.append("images-start")
            await asyncio.sleep(30)
            e.calls.append("images-end")
        e._images = slow_images
        task = asyncio.ensure_future(e.a_auto_run({}))
        for _ in range(200):
            if "images-start" in e.calls:
                break
            await asyncio.sleep(0.01)
        t0 = asyncio.get_running_loop().time()
        await e.a_auto_stop({})
        await asyncio.wait_for(task, 3)
        self.assertLess(asyncio.get_running_loop().time() - t0, 2, "stops within moments, not after the step")
        self.assertNotIn("images-end", e.calls)
        ph = {p["id"]: p["status"] for p in e.state["auto"]["phases"]}
        self.assertEqual((ph["images"], ph["videos"], ph["layout"], ph["report"]), ("skipped",) * 4)
        self.assertNotIn("spacing", e.calls)

    async def test_no_deck_changes_after_stop(self):
        e = self.e
        sent = []

        async def send(obj):
            sent.append(obj.get("method"))
        e.plugin_send = send
        e._busy = "optimising the deck"
        e._auto_stop = True
        for method in ("swapImages", "removeItems", "applySpacing", "applyMoves", "placeVideo", "saveVersion"):
            with self.assertRaises(asyncio.CancelledError):
                await e.plugin_call(method, {})
        self.assertEqual(sent, [], "nothing that edits the deck was sent")


class TestChoosingAnotherDeck(unittest.IsolatedAsyncioTestCase):
    """Pasting another deck's link replaces the previous deck and starts over."""
    A = "https://www.figma.com/slides/AAAAAAAAAAAAAAAAAAAAAA/Client-One-Deck"
    B = "https://www.figma.com/slides/BBBBBBBBBBBBBBBBBBBBBB"   # no name in the link

    async def make(self):
        e = FakeEngine(config.Config(os.path.join(TMP, "cfg3.json")), SecureStore(os.path.join(TMP, "s3.bin")), 47895)
        e.attach(asyncio.get_running_loop())
        e.state["connections"]["plugin"] = {"connected": True, "file": {"name": "Client One Deck", "_at": 1}}
        return e

    async def test_new_deck_starts_fresh(self):
        e = await self.make()
        await e.a_deck_set({"url": self.A})
        self.assertTrue(e.state["deck"]["pluginMatches"])
        e.state["scan"] = {"status": "done"}
        e.state["auto"] = {"status": "done"}
        e.state["details"] = {"status": "ready", "cover": [], "groups": []}
        e.undo = [{"x": 1}]
        e.inv = {"slides": []}
        d = await e.a_deck_set({"url": self.B})
        self.assertEqual(d["key"], "BBBBBBBBBBBBBBBBBBBBBB")
        for k in ("scan", "auto", "details"):
            self.assertEqual(e.state[k]["status"], "idle", k)
        self.assertEqual((e.undo, e.inv), ([], None))

    async def test_link_without_name_is_not_taken_for_the_open_deck(self):
        e = await self.make()
        d = await e.a_deck_set({"url": self.B})
        self.assertIsNone(d["name"], "it must not borrow the name of the deck open in Figma")
        self.assertIsNone(d["pluginMatches"], "unknown, so Optimise opens it first")

    async def test_same_deck_again_keeps_progress(self):
        e = await self.make()
        await e.a_deck_set({"url": self.A})
        e.state["scan"] = {"status": "done"}
        await e.a_deck_set({"url": self.A + "?node-id=1-2"})
        self.assertEqual(e.state["scan"]["status"], "done")

    async def test_cannot_switch_while_optimising(self):
        e = await self.make()
        await e.a_deck_set({"url": self.A})
        e._busy = "optimising the deck"
        with self.assertRaises(RuntimeError):
            await e.a_deck_set({"url": self.B})
        self.assertEqual(e.state["deck"]["key"], "AAAAAAAAAAAAAAAAAAAAAA")
