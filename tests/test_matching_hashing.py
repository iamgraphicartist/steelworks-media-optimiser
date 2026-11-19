import hashlib
import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from smo import hashing, matching  # noqa: E402
from smo.matching import Candidate  # noqa: E402
from smo.sources import HashCache, LocalFolderSource, Source, find_source  # noqa: E402


class TestNormalize(unittest.TestCase):
    def test_strips_extension_case_and_punctuation(self):
        self.assertEqual(matching.normalize("Hero_Final-v03.MP4"), "herofinalv03")

    def test_strips_copy_suffixes(self):
        for n in ["Hero Final (1).mp4", "Hero Final copy", "Hero Final copy 2", "Hero Final 2", "Hero Final - Copy"]:
            self.assertEqual(matching.normalize(n), "herofinal", n)

    def test_keeps_numbers_without_space(self):
        self.assertEqual(matching.normalize("city01.mp4"), "city01")

    def test_handles_windows_paths(self):
        self.assertEqual(matching.normalize(r"E:\Dropbox\Jobs\Spot_30s.mov"), "spot30s")

    def test_non_media_extension_is_kept_as_text(self):
        self.assertEqual(matching.normalize("v1.2 render"), "v12render")


class TestScore(unittest.TestCase):
    def test_exact_after_normalising(self):
        self.assertEqual(matching.score("Moncler Teaser", "moncler_teaser.mp4"), 1.0)

    def test_containment_beats_threshold(self):
        self.assertGreater(matching.score("Moncler_Teaser", "Moncler_Teaser_FINAL_1080.mp4"), 0.8)

    def test_unrelated_is_low(self):
        self.assertLess(matching.score("Moncler Teaser", "mountain02.mp4"), 0.4)

    def test_camel_case_tokens(self):
        self.assertIn("teaser", matching.tokens("MonclerTeaserCut.mp4"))

    def test_search_queries_most_specific_first(self):
        q = matching.search_queries("Adidas_Hero_Spot (2).mp4")
        self.assertEqual(q[0], "Adidas_Hero_Spot")
        self.assertIn("adidas hero spot", q)


class TestRank(unittest.TestCase):
    def test_order_prefers_score_then_local_then_small(self):
        cands = [
            Candidate("hero.mp4", "/a/hero.mp4", 900, online_only=True),
            Candidate("hero.mp4", "/b/hero.mp4", 500, online_only=False),
            Candidate("hero.mp4", "/c/hero.mp4", 100, online_only=False),
            Candidate("hero_alt_cut.mp4", "/d/hero_alt_cut.mp4", 10),
            Candidate("cat.mp4", "/e/cat.mp4", 1),
            Candidate("hero.psd", "/f/hero.psd", 1),
        ]
        r = matching.rank("Hero", cands)
        self.assertEqual([c.path for c in r[:3]], ["/c/hero.mp4", "/b/hero.mp4", "/a/hero.mp4"])
        self.assertNotIn("/e/cat.mp4", [c.path for c in r])
        self.assertNotIn("/f/hero.psd", [c.path for c in r])

    def test_duplicate_paths_keep_local_copy(self):
        r = matching.rank("hero", [Candidate("hero.mp4", "/X/hero.mp4", 5, online_only=True),
                                   Candidate("hero.mp4", "/x/HERO.mp4", 5, online_only=False)])
        self.assertEqual(len(r), 1)
        self.assertFalse(r[0].online_only)


class TestHashing(unittest.TestCase):
    def test_sha1_matches_hashlib(self):
        data = os.urandom(3 * 1024 * 1024 + 7)
        self.assertEqual(hashing.sha1_bytes(data), hashlib.sha1(data).hexdigest())
        self.assertEqual(hashing.sha1_stream(io.BytesIO(data)), hashlib.sha1(data).hexdigest())

    def test_file_and_progress(self):
        data = os.urandom(20 << 20)
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(data)
        seen = []
        try:
            self.assertEqual(hashing.sha1_file(f.name, seen.append), hashlib.sha1(data).hexdigest())
            self.assertEqual(seen[-1], len(data))
        finally:
            os.remove(f.name)

    def test_hash_matches_rules(self):
        full = hashlib.sha1(b"x").hexdigest()
        self.assertTrue(hashing.hash_matches(full, full.upper()))
        self.assertTrue(hashing.hash_matches(full, full[:10]))
        self.assertFalse(hashing.hash_matches(full, full[:7]))        # too short to trust
        self.assertFalse(hashing.hash_matches(full, "zz" + full[2:]))  # not hex
        self.assertFalse(hashing.hash_matches(full[:39], full))
        self.assertFalse(hashing.hash_matches(full, ""))

    def test_hashing_writer(self):
        buf = io.BytesIO()
        w = hashing.HashingWriter(buf)
        for part in (b"abc", b"", b"def" * 1000):
            w.write(part)
        self.assertEqual(w.hexdigest(), hashlib.sha1(buf.getvalue()).hexdigest())
        self.assertEqual(w.bytes_written, len(buf.getvalue()))


class TestFindSource(unittest.TestCase):
    """Name match narrows the field; only the SHA-1 decides."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cache = HashCache(os.path.join(self.tmp, "cache.json"))
        root = os.path.join(self.tmp, "Dropbox", "Jobs")
        os.makedirs(os.path.join(root, "old"))
        os.makedirs(os.path.join(root, "final"))
        self.wrong = os.path.join(root, "old", "Hero Spot.mp4")
        self.right = os.path.join(root, "final", "Hero_Spot_v2.mp4")
        with open(self.wrong, "wb") as f:
            f.write(b"an older render")
        with open(self.right, "wb") as f:
            f.write(b"the real original bytes")
        open(os.path.join(root, "unrelated.mp4"), "wb").write(b"x")
        self.src = LocalFolderSource(os.path.join(self.tmp, "Dropbox"), self.cache)

    def test_confirms_by_hash_not_by_best_name(self):
        target = hashlib.sha1(b"the real original bytes").hexdigest()
        r = find_source("Hero Spot", target, [self.src], self.tmp)
        self.assertEqual(r.status, "confirmed")
        self.assertEqual(os.path.normcase(r.path), os.path.normcase(self.right))
        self.assertEqual([t["result"] for t in r.tried], ["different file", "match"])

    def test_not_found_when_no_hash_matches(self):
        r = find_source("Hero Spot", hashlib.sha1(b"nope").hexdigest(), [self.src], self.tmp)
        self.assertEqual(r.status, "not_found")
        self.assertEqual(len(r.tried), 2)

    def test_no_candidates(self):
        r = find_source("Completely Different", "a" * 40, [self.src], self.tmp)
        self.assertEqual(r.status, "no_candidates")

    def test_hash_cache_reused(self):
        target = hashlib.sha1(b"the real original bytes").hexdigest()
        find_source("Hero Spot", target, [self.src], self.tmp)
        self.assertEqual(HashCache(self.cache.path).get(self.right), target)

    def test_failing_source_does_not_stop_others(self):
        class Broken(Source):
            label = "Broken"

            def search(self, name):
                raise RuntimeError("offline")
        target = hashlib.sha1(b"the real original bytes").hexdigest()
        logs = []
        r = find_source("Hero Spot", target, [Broken(), self.src], self.tmp, log=logs.append)
        self.assertEqual(r.status, "confirmed")
        self.assertTrue(any("Broken" in m for m in logs))

    def test_temporary_downloads_removed_on_mismatch(self):
        tmpdl = os.path.join(self.tmp, "dl")

        class Remote(Source):
            label = "Remote"

            def search(self, name):
                return [Candidate("Hero Spot.mp4", "/Hero Spot.mp4", 3, "dropbox", {}, True)]

            def fetch(self, c, workdir, progress=None):
                os.makedirs(workdir, exist_ok=True)
                p = os.path.join(workdir, "x.bin")
                open(p, "wb").write(b"zzz")
                return p, hashlib.sha1(b"zzz").hexdigest(), True
        r = find_source("Hero Spot", "b" * 40, [Remote()], tmpdl)
        self.assertEqual(r.status, "not_found")
        self.assertFalse(os.path.exists(os.path.join(tmpdl, "x.bin")))


if __name__ == "__main__":
    unittest.main()
