"""Build installable releases.

  python scripts/build.py              Windows: dist/...-Windows-Setup.exe   macOS: dist/...-macOS-<arch>.dmg
  python scripts/build.py --no-ffmpeg  don't bundle ffmpeg (the app then uses one on PATH / Homebrew)
  python scripts/build.py --skip-tests

Also writes dist/Steelworks-Media-Optimiser-Figma-Plugin-<ver>.zip for importing the plugin.
Needs: Python 3.11+ with requirements.txt installed; Windows: Inno Setup 6; macOS: Xcode command line tools.
Optional signing (macOS): SMO_SIGN_IDENTITY="Developer ID Application: ..." and SMO_NOTARY_PROFILE=<notarytool profile>.
"""
import argparse
import os
import platform
import shutil
import subprocess
import sys
import zipfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
NAME = "Steelworks Media Optimiser"
SLUG = "Steelworks-Media-Optimiser"
VERSION = "1.0.9"
DIST = os.path.join(ROOT, "dist")
import tempfile
# Short path: Windows resource stamping (icon/version) fails on very long paths.
WORK = os.path.join(tempfile.gettempdir(), "smo-build") if sys.platform == "win32" else os.path.join(ROOT, "build", "pyinstaller")


def run(cmd, **kw):
    print(">", " ".join(f'"{c}"' if " " in c else c for c in cmd), flush=True)
    subprocess.run(cmd, check=True, **kw)


def step(t):
    print(f"\n=== {t}", flush=True)


def vendor_ffmpeg():
    sys.path.insert(0, os.path.join(ROOT, "app"))
    from smo.video import find_tool
    dest = os.path.join(ROOT, "vendor", "ffmpeg")
    if os.path.isdir(dest) and os.listdir(dest):
        print("Using binaries already in vendor/ffmpeg")
        return True
    exe = ".exe" if sys.platform == "win32" else ""
    ff, fp = find_tool("ffmpeg"), find_tool("ffprobe")
    if not (ff and fp):
        print("ffmpeg not found — building without it (users need ffmpeg installed for videos).")
        return False
    if sys.platform == "darwin":
        # Homebrew's ffmpeg depends on dozens of dylibs; only bundle self-contained (static) builds.
        out = subprocess.run(["otool", "-L", os.path.realpath(ff)], capture_output=True, text=True).stdout
        if "/opt/homebrew" in out or "/usr/local/Cellar" in out or "/usr/local/opt" in out:
            print("Homebrew ffmpeg is not self-contained — not bundling. Put static ffmpeg/ffprobe in vendor/ffmpeg "
                  "to bundle them, or tell users to `brew install ffmpeg`.")
            return False
    os.makedirs(dest, exist_ok=True)
    for src in (ff, fp):
        shutil.copy2(os.path.realpath(src), os.path.join(dest, os.path.basename(src).split(".")[0] + exe))
    print("Bundling", ff, "and", fp)
    return True


def plugin_zip():
    os.makedirs(DIST, exist_ok=True)
    path = os.path.join(DIST, f"{SLUG}-Figma-Plugin-{VERSION}.zip")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        base = os.path.join(ROOT, "plugin")
        z.write(os.path.join(base, "manifest.json"), "Steelworks Media Optimiser Plugin/manifest.json")
        for f in os.listdir(os.path.join(base, "dist")):
            z.write(os.path.join(base, "dist", f), f"Steelworks Media Optimiser Plugin/dist/{f}")
    return path


def windows_installer():
    iscc = None
    for c in [os.path.expandvars(r"%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"),
              r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe", r"C:\Program Files\Inno Setup 6\ISCC.exe"]:
        if os.path.isfile(c):
            iscc = c
            break
    iscc = iscc or shutil.which("iscc")
    if not iscc:
        print("Inno Setup not found — leaving the unpacked app in build/pyinstaller/dist. Install it with: "
              "winget install JRSoftware.InnoSetup")
        return None
    outdir = os.path.join(WORK, "installer")      # short path: ISCC also trips over MAX_PATH
    run([iscc, "/Qp", f"/DSrc={os.path.join(WORK, 'dist', NAME)}", f"/O{outdir}", os.path.join(ROOT, "packaging", "installer.iss")])
    name = f"{SLUG}-{VERSION}-Windows-Setup.exe"
    os.makedirs(DIST, exist_ok=True)
    shutil.copy2(os.path.join(outdir, name), os.path.join(DIST, name))
    return os.path.join(DIST, name)


def mac_dmg():
    app = os.path.join(WORK, "dist", NAME + ".app")
    ident = os.environ.get("SMO_SIGN_IDENTITY")
    if ident:
        run(["codesign", "--force", "--deep", "--options", "runtime", "--timestamp", "-s", ident, app])
    else:
        run(["codesign", "--force", "--deep", "-s", "-", app])   # ad-hoc: required on Apple silicon
    arch = os.environ.get("SMO_TARGET_ARCH") or platform.machine()
    os.makedirs(DIST, exist_ok=True)
    dmg = os.path.join(DIST, f"{SLUG}-{VERSION}-macOS-{arch}.dmg")
    stage = os.path.join(ROOT, "build", "dmg")
    shutil.rmtree(stage, ignore_errors=True)
    os.makedirs(stage)
    run(["ditto", app, os.path.join(stage, NAME + ".app")])
    os.symlink("/Applications", os.path.join(stage, "Applications"))
    if os.path.exists(dmg):
        os.remove(dmg)
    # hdiutil fails intermittently on CI Macs ("Resource busy"): retry a few times.
    import time
    for attempt in range(1, 6):
        try:
            run(["hdiutil", "create", "-volname", NAME, "-srcfolder", stage, "-ov", "-fs", "HFS+", "-format", "UDZO", dmg])
            break
        except subprocess.CalledProcessError:
            if attempt == 5:
                raise
            print(f"hdiutil failed (attempt {attempt}), retrying...", flush=True)
            subprocess.run(["hdiutil", "detach", "-force", f"/Volumes/{NAME}"], capture_output=True)
            time.sleep(10 * attempt)
    if ident:
        run(["codesign", "-s", ident, "--timestamp", dmg])
        if os.environ.get("SMO_NOTARY_PROFILE"):
            run(["xcrun", "notarytool", "submit", dmg, "--keychain-profile", os.environ["SMO_NOTARY_PROFILE"], "--wait"])
            run(["xcrun", "stapler", "staple", dmg])
    return dmg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-ffmpeg", action="store_true")
    ap.add_argument("--skip-tests", action="store_true")
    a = ap.parse_args()
    os.chdir(ROOT)

    step("Building the Figma plugin")
    run([sys.executable, os.path.join("plugin", "build.py")])
    step("Icons")
    if not os.path.exists(os.path.join(ROOT, "packaging", "icon.icns")):   # icons are checked in; redraw only if missing
        run([sys.executable, os.path.join("packaging", "make_icon.py")])
    if not a.skip_tests:
        step("Tests")
        run([sys.executable, "-m", "unittest", "discover", "-s", "tests"])
        run([sys.executable, os.path.join("scripts", "run_js_tests.py")])
    if not a.no_ffmpeg:
        step("ffmpeg")
        vendor_ffmpeg()
    else:
        shutil.rmtree(os.path.join(ROOT, "vendor", "ffmpeg"), ignore_errors=True)
    studio = {k: os.environ[e] for k, e in (("dropbox_app_key", "SMO_DROPBOX_APP_KEY"),
                                            ("figma_client_id", "SMO_FIGMA_CLIENT_ID")) if os.environ.get(e)}
    if studio:
        step("Studio settings")
        import json
        with open(os.path.join(ROOT, "steelworks.config.json"), "w", encoding="utf-8") as f:
            json.dump(studio, f, indent=2)
        print("Pre-configured:", ", ".join(studio))      # values are public IDs, not secrets
    step("Packaging the app (PyInstaller)")
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", os.path.join(WORK, "dist"),
         "--workpath", os.path.join(WORK, "work"), os.path.join("packaging", "smo.spec")])
    step("Installer")
    out = windows_installer() if sys.platform == "win32" else mac_dmg() if sys.platform == "darwin" else None
    pz = plugin_zip()
    print("\nDone.")
    for p in (out, pz):
        if p and os.path.exists(p):
            print(f"  {p}  ({os.path.getsize(p) / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
