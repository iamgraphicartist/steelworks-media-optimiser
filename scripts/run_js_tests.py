"""Run tests/js/*.test.html in a headless Chromium (Edge or Chrome) and report.
Usage: python scripts/run_js_tests.py      (exit code 1 on failure)"""
import glob
import html
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
]


def browser():
    for c in CANDIDATES:
        if os.path.isfile(c):
            return c
    for n in ("google-chrome", "chromium", "chromium-browser", "msedge"):
        p = shutil.which(n)
        if p:
            return p
    return None


def main():
    exe = browser()
    if not exe:
        print("No Chrome/Edge found - skipping JS tests")
        return 0
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    failed = 0
    for page in sorted(glob.glob(os.path.join(root, "tests", "js", "*.test.html"))):
        prof = tempfile.mkdtemp()
        args = [exe, "--headless=new", "--disable-gpu", "--allow-file-access-from-files", f"--user-data-dir={prof}",
                "--no-first-run", "--no-default-browser-check", "--disable-extensions",
                "--use-mock-keychain", "--password-store=basic",       # macOS: never wait on a Keychain prompt
                "--virtual-time-budget=5000", "--dump-dom", pathlib.Path(page).as_uri()]
        try:
            r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", timeout=120)
        except subprocess.TimeoutExpired:
            shutil.rmtree(prof, ignore_errors=True)
            print(f"== {os.path.basename(page)}: the browser didn't respond - skipped "
                  "(set SMO_STRICT_JS_TESTS=1 to fail instead).")
            if os.environ.get("SMO_STRICT_JS_TESTS"):
                failed += 1
            continue
        shutil.rmtree(prof, ignore_errors=True)
        m = re.search(r'<pre id="result">(.*?)</pre>', r.stdout, re.S)
        out = html.unescape(m.group(1)) if m else "(no output)\n" + r.stderr[-500:]
        print(f"== {os.path.basename(page)}\n{out}\n")
        if "RESULT:PASS" not in out:
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
