"""'Save files to replace': prepared files for anything that couldn't be placed automatically."""
import asyncio
import csv
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


class TestExportReplacements(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        cfg = config.Config(os.path.join(TMP, "cfg.json"))
        self.e = Engine(cfg, SecureStore(os.path.join(TMP, "s.bin")), 47898, demo={"dropbox": TMP})
        self.e.attach(asyncio.get_running_loop())
        vid = os.path.join(self.e.out_dir, "Slide03_Hero_abc123.mp4")
        img = os.path.join(self.e.out_dir, "deadbeef.jpg")
        for p, data in ((vid, b"mp4 bytes"), (img, b"jpg bytes")):
            with open(p, "wb") as f:
                f.write(data)
        self.e.state["deck"] = {"name": "Spring Deck"}
        self.e.state["videos"]["items"] = [
            {"kind": "video", "slide": 3, "name": "Hero: final/cut", "status": "ready", "out": vid, "skipped": True, "hash": "a"},
            {"kind": "video", "slide": 4, "name": "Teaser", "status": "not_found", "detail": "No similar file", "hash": "b"},
            {"kind": "video", "slide": 5, "name": "Done one", "status": "done", "out": vid, "hash": "c"},
        ]
        self.e.state["images"]["items"] = [
            {"slides": [7], "names": ["Phone photo"], "status": "manual", "file": "deadbeef.jpg", "reason": "Has a camera rotation tag"},
            {"slides": [8], "names": ["Fine"], "status": "done", "file": "x.jpg"},
        ]

    async def test_copies_files_named_by_slide_with_list_and_instructions(self):
        r = await self.e.a_export_replacements({})
        folder = r["folder"]
        self.assertTrue(folder.endswith("Spring Deck – files to replace"))
        names = sorted(os.listdir(folder))
        self.assertIn("Slide 03 – Hero_ final_cut.mp4", names)       # unsafe characters cleaned
        self.assertIn("Slide 07 – Phone photo.jpg", names)
        self.assertIn("Replace list.csv", names)
        self.assertIn("How to replace.txt", names)
        self.assertEqual(r["files"], 2, "done items are not exported")
        with open(os.path.join(folder, "Replace list.csv"), encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual([row["Slide"] for row in rows], ["3", "7", "4"])
        self.assertEqual(rows[2]["Prepared file"], "", "not-found videos are listed without a file")
        with open(os.path.join(folder, "Slide 03 – Hero_ final_cut.mp4"), "rb") as f:
            self.assertEqual(f.read(), b"mp4 bytes")

    async def test_nothing_to_do_is_a_friendly_message(self):
        self.e.state["videos"]["items"] = []
        self.e.state["images"]["items"] = []
        with self.assertRaises(RuntimeError) as cm:
            await self.e.a_export_replacements({})
        self.assertIn("Nothing needs replacing", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
