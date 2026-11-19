# PyInstaller spec — builds the app for Windows (folder + .exe) and macOS (.app).
# Run through scripts/build_windows.ps1 or scripts/build_macos.sh.
import os
import sys

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
NAME = "Steelworks Media Optimiser"
VERSION = "1.0.1"

datas = [
    (os.path.join(ROOT, "app", "smo", "ui"), os.path.join("smo", "ui")),
    (os.path.join(ROOT, "plugin", "manifest.json"), "plugin"),
    (os.path.join(ROOT, "plugin", "dist"), os.path.join("plugin", "dist")),
]
binaries = []
vendor = os.path.join(ROOT, "vendor", "ffmpeg")       # optional: bundle ffmpeg/ffprobe
if os.path.isdir(vendor):
    for f in os.listdir(vendor):
        binaries.append((os.path.join(vendor, f), "ffmpeg"))
studio_cfg = os.path.join(ROOT, "steelworks.config.json")  # optional: client IDs for the studio (no secrets)
if os.path.isfile(studio_cfg):
    datas.append((studio_cfg, "."))

a = Analysis([os.path.join(ROOT, "app", "run.py")], pathex=[os.path.join(ROOT, "app")], binaries=binaries,
             datas=datas, hiddenimports=["webview"], excludes=["tkinter", "matplotlib", "numpy", "pytest"],
             noarchive=False)
pyz = PYZ(a.pure)
icon = os.path.join(SPECPATH, "icon.icns" if sys.platform == "darwin" else "icon.ico")
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name=NAME, console=False, icon=icon,
          version=os.path.join(SPECPATH, "version_info.txt") if sys.platform == "win32" else None,
          target_arch=os.environ.get("SMO_TARGET_ARCH") or None)
coll = COLLECT(exe, a.binaries, a.datas, name=NAME, strip=False, upx=False)

if sys.platform == "darwin":
    app = BUNDLE(coll, name=NAME + ".app", icon=icon, bundle_identifier="com.steelworks.media-optimiser",
                 info_plist={
                     "CFBundleShortVersionString": VERSION, "CFBundleVersion": VERSION,
                     "NSHighResolutionCapable": True, "LSMinimumSystemVersion": "11.0",
                     "NSRequiresAquaSystemAppearance": False,      # follow light/dark mode
                     "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
                 })
