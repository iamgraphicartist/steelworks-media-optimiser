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
                         {"scan": "done", "cleanup": "done", "images": "done", "videos": "done", "layout": "done", "report": "done"})
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
        e.state["connections"]["plugin"] = {"connected": True, "file": {"canCreateVideo": True}}
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
