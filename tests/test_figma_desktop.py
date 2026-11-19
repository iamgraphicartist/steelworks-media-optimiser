"""Adding the plugin to Figma desktop's settings.json (on a copy, never the real one)."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from smo import figma_desktop as fd  # noqa: E402

PLUGIN = os.path.join(os.path.dirname(__file__), "..", "plugin", "manifest.json")


class TestRegister(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.settings = os.path.join(self.tmp, "settings.json")
        self.other = {"id": 1, "manifestPath": "C:/x/Other/manifest.json", "lastKnownName": "Other",
                      "fileMetadata": {"type": "manifest", "codeFileId": None, "uiFileIds": None}}
        with open(self.settings, "w", encoding="utf-8") as f:
            json.dump({"themePreference": "dark", "localFileExtensions": [self.other]}, f)

    def load(self):
        with open(self.settings, encoding="utf-8") as f:
            return json.load(f)

    def test_adds_manifest_code_and_ui_linked_by_id(self):
        self.assertFalse(fd.is_registered(PLUGIN, self.settings))
        self.assertTrue(fd.register(PLUGIN, self.settings))
        data = self.load()
        exts = data["localFileExtensions"]
        self.assertEqual(exts[0], self.other, "other plugins are untouched")
        self.assertEqual(data["themePreference"], "dark", "other settings are untouched")
        m = next(e for e in exts if e.get("lastKnownName") == "Steelworks Media Optimiser")
        code = next(e for e in exts if e["id"] == m["fileMetadata"]["codeFileId"])
        ui = next(e for e in exts if e["id"] == m["fileMetadata"]["uiFileIds"][0])
        self.assertTrue(code["manifestPath"].endswith(os.path.join("dist", "code.js")))
        self.assertTrue(ui["manifestPath"].endswith(os.path.join("dist", "ui.html")))
        self.assertEqual(code["fileMetadata"], {"type": "code", "manifestFileId": m["id"]})
        self.assertEqual(len({e["id"] for e in exts}), len(exts), "ids are unique")
        self.assertTrue(fd.is_registered(PLUGIN, self.settings))
        self.assertTrue(os.path.exists(self.settings + ".before-steelworks"), "a backup is kept")

    def test_second_time_changes_nothing(self):
        fd.register(PLUGIN, self.settings)
        before = self.load()
        self.assertFalse(fd.register(PLUGIN, self.settings))
        self.assertEqual(self.load(), before)

    def test_stale_old_install_is_replaced(self):
        data = self.load()
        data["localFileExtensions"] += [
            {"id": 7, "manifestPath": "C:/gone/plugin/manifest.json", "lastKnownName": "Steelworks Media Optimiser",
             "fileMetadata": {"type": "manifest", "codeFileId": 8, "uiFileIds": [9]}},
            {"id": 8, "manifestPath": "C:/gone/plugin/dist/code.js", "fileMetadata": {"type": "code", "manifestFileId": 7}},
            {"id": 9, "manifestPath": "C:/gone/plugin/dist/ui.html", "fileMetadata": {"type": "ui", "manifestFileId": 7}}]
        with open(self.settings, "w", encoding="utf-8") as f:
            json.dump(data, f)
        fd.register(PLUGIN, self.settings)
        paths = [e["manifestPath"] for e in self.load()["localFileExtensions"]]
        self.assertFalse(any("gone" in p for p in paths))
        self.assertEqual(sum(1 for e in self.load()["localFileExtensions"]
                             if e.get("lastKnownName") == "Steelworks Media Optimiser"), 1)

    def test_missing_settings_file_is_a_clear_error(self):
        with self.assertRaises(FileNotFoundError):
            fd.register(PLUGIN, os.path.join(self.tmp, "nope.json"))

if __name__ == "__main__":
    unittest.main()
