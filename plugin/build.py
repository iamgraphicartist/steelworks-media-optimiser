"""Build the Figma plugin into plugin/dist (no Node required).

code.js = src/core.js + src/main.js, ui.html copied as-is.
Syntax and behaviour are checked by tests/js (run with scripts/run_js_tests.py).
"""
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC, DIST = os.path.join(HERE, "src"), os.path.join(HERE, "dist")


def build() -> str:
    os.makedirs(DIST, exist_ok=True)
    parts = []
    for name in ("core.js", "main.js"):
        with open(os.path.join(SRC, name), encoding="utf-8") as f:
            parts.append(f"// ---- {name}\n" + f.read())
    code = "\n".join(parts)
    with open(os.path.join(DIST, "code.js"), "w", encoding="utf-8", newline="\n") as f:
        f.write(code)
    shutil.copyfile(os.path.join(SRC, "ui.html"), os.path.join(DIST, "ui.html"))
    return DIST


if __name__ == "__main__":
    out = build()
    print("Plugin built ->", out)
    sys.exit(0)
