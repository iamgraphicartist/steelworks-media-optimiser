"""Generate the sample deck used by --demo and the plugin harness.

samples/images/   heavy images as Figma would store them
samples/dropbox/  a fake "Dropbox" with the original videos plus look-alike decoys
samples/deck.json the slide layout the mock Figma API serves
"""
import hashlib
import json
import os
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFilter

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "samples"))
IMG, DBX = os.path.join(ROOT, "images"), os.path.join(ROOT, "dropbox")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from smo.video import find_tool  # noqa: E402

FF = find_tool("ffmpeg")


def photo(w, h, seed, alpha=False):
    base = Image.linear_gradient("L").rotate(seed * 37 % 360, expand=False).resize((w, h))
    noise = Image.effect_noise((w, h), 40 + seed * 7)
    r = Image.blend(base, noise, 0.35)
    g = Image.blend(base.transpose(Image.FLIP_LEFT_RIGHT), noise, 0.3)
    b = Image.blend(base.transpose(Image.FLIP_TOP_BOTTOM), Image.effect_noise((w, h), 30), 0.4)
    im = Image.merge("RGB", (r, g, b)).filter(ImageFilter.GaussianBlur(1.2))
    d = ImageDraw.Draw(im)
    for i in range(6):
        x, y = (seed * 131 + i * 389) % w, (seed * 71 + i * 211) % h
        d.ellipse((x, y, x + w // 6, y + w // 6), fill=((seed * 50 + i * 40) % 255, 120, 200 - i * 20))
    if alpha:
        im = im.convert("RGBA")
        mask = Image.new("L", (w, h), 0)
        ImageDraw.Draw(mask).rounded_rectangle((w * .08, h * .2, w * .92, h * .8), radius=h // 8, fill=255)
        im.putalpha(mask)
    return im


def sha1(path):
    with open(path, "rb") as f:
        return hashlib.sha1(f.read()).hexdigest()


def save(im, name, **kw):
    p = os.path.join(IMG, name)
    im.save(p, **kw)
    return name


def video(path, size, seconds, src="testsrc2", audio=True, crf=6):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    args = [FF, "-v", "error", "-y", "-f", "lavfi", "-i", f"{src}=size={size}:rate=25"]
    if audio:
        args += ["-f", "lavfi", "-i", "sine=frequency=330"]
    args += ["-t", str(seconds), "-c:v", "libx264", "-crf", str(crf), "-pix_fmt", "yuv420p"]
    args += ["-c:a", "aac", "-b:a", "320k"] if audio else []
    subprocess.run(args + [path], check=True)


def main():
    os.makedirs(IMG, exist_ok=True)
    os.makedirs(DBX, exist_ok=True)
    imgs = {
        "hero": save(photo(4000, 2250, 1), "hero.png"),
        "covered": save(photo(3000, 1700, 2), "covered.png"),
        "cover_top": save(photo(3840, 2160, 3), "cover_top.jpg", quality=97),
        "logo": save(photo(2400, 1200, 4, alpha=True), "logo.png"),
        "hidden": save(photo(2600, 1800, 5), "hidden.png"),
        "small": save(photo(600, 400, 6).resize((600, 400)), "small.jpg", quality=80),
    }
    for i in range(6):
        imgs[f"t{i}"] = save(photo(1800, 1800, 10 + i), f"thumb{i}.png")
    frames = [photo(480, 270, 20 + i).convert("P", palette=Image.ADAPTIVE) for i in range(12)]
    frames[0].save(os.path.join(IMG, "loader.gif"), save_all=True, append_images=frames[1:], duration=80, loop=0)
    imgs["gif"] = "loader.gif"

    # "Dropbox": the real originals + decoys with similar names
    hero_v = os.path.join(DBX, "Jobs", "Final", "Hero_Spot_v2.mov")
    video(hero_v, "3840x2160", 4)
    video(os.path.join(DBX, "Jobs", "Old", "Hero Spot.mp4"), "1280x720", 2, src="testsrc")       # decoy
    teaser_decoy = os.path.join(DBX, "Archive", "Teaser.mp4")
    video(teaser_decoy, "640x360", 2, src="smptebars", audio=False)                              # same name, wrong file
    reel = os.path.join(DBX, "Showreel", "Studio Reel 2026 (final).mp4")
    video(reel, "2560x1440", 3, src="mandelbrot")
    teaser_hash = hashlib.sha1(b"teaser original that is not in dropbox").hexdigest()

    H = {k: sha1(os.path.join(IMG, v)) for k, v in imgs.items()}
    files = {H[k]: v for k, v in imgs.items()}
    full = {"x": 0, "y": 0, "w": 1920, "h": 1080}

    def rect(name, key, x, y, w, h, **kw):
        paint = {"type": "IMAGE", "imageHash": H[key], "scaleMode": kw.pop("scaleMode", "FILL")}
        paint.update(kw.pop("paint", {}))
        return {"type": "RECTANGLE", "name": name, "x": x, "y": y, "w": w, "h": h, "fills": [paint], **kw}

    def vid(name, h, x, y, w, hh, **kw):
        return {"type": "RECTANGLE", "name": name, "x": x, "y": y, "w": w, "h": hh,
                "fills": [{"type": "VIDEO", "videoHash": h, "scaleMode": "FILL"}], **kw}

    gaps = [(0, 0), (512, 0), (1040, 0), (0, 360), (512, 360), (1040, 360)]  # gutters of 12, 28 and 20 px
    deck = {"name": "Sample Pitch Deck", "fileKey": "SAMPLEdeck00000000000", "slides": [
        {"name": "Cover", "children": [rect("Hero image", "hero", **{k: v for k, v in zip("xywh", full.values())}),
                                       rect("Logo", "logo", 660, 380, 600, 300, scaleMode="FIT"),
                                       {"type": "TEXT", "name": "Title", "x": 160, "y": 860, "w": 900, "h": 80, "fills": [{"type": "SOLID", "color": {"r": 1, "g": 1, "b": 1}}]}]},
        {"name": "Work", "children": [
            {"type": "FRAME", "name": "Thumbnails", "x": 190, "y": 180, "w": 1540, "h": 700, "clipsContent": False,
             "children": [rect(f"Project {i + 1}", f"t{i}", x, y, 500, 340) for i, (x, y) in enumerate(gaps)]}]},
        {"name": "Film", "children": [
            rect("Old background", "covered", 0, 0, 1920, 1080),
            rect("Background", "cover_top", 0, 0, 1920, 1080, scaleMode="CROP",
                 paint={"imageTransform": [[0.5, 0, 0.25], [0, 0.5, 0.25]]}),
            rect("Alt layout", "hidden", 200, 200, 800, 500, visible=False),
            vid("Hero Spot", sha1(hero_v), 160, 140, 1600, 900)]},
        {"name": "Details", "children": [
            rect("Loader anim", "gif", 160, 300, 800, 450),
            rect("Swatch", "small", 1100, 300, 600, 400, paint={"visible": False}),
            vid("Teaser", teaser_hash, 1100, 760, 480, 270),
            vid("Studio Reel 2026", sha1(reel), 160, 780, 480, 270, cornerRadius=12)]},
    ]}
    # Headers, footers and titles for the alignment step: mostly consistent, a few a hair off.
    def text(name, x, y, w, h):
        return {"type": "TEXT", "name": name, "x": x, "y": y, "w": w, "h": h,
                "fills": [{"type": "SOLID", "color": {"r": 1, "g": 1, "b": 1}}]}
    header_x = [120, 120, 126, 120]          # slide 3 header is 6 px off
    page_y = [1004, 1004, 1004, 1011]        # slide 4 page number sits 7 px low
    title_x = [None, 129, 120, 120]          # slide 2 title is 9 px off the margin
    for i, s in enumerate(deck["slides"]):
        s["children"].append(text("Steelworks logo", header_x[i], 60, 220, 28))
        s["children"].append(text(f"Page {i + 1:02d}", 1740, page_y[i], 60, 24))
        if title_x[i] is not None:
            s["children"].append(text("Slide title", title_x[i], 110 if i != 2 else 980, 700, 56))
    # Deck details: cover lines, a repeated header name and a footer date on every slide.
    for s in deck["slides"]:
        for c in s["children"]:
            if c.get("name") == "Steelworks logo":
                c["characters"] = "SAMPLE CLIENT"
        s["children"].append(text("Footer date", 120, 1010, 300, 30) | {"characters": "November 2026"})
    deck["slides"][0]["children"] += [text("Cover date", 160, 640, 500, 40) | {"characters": "November 2026"},
                                      text("Cover title", 160, 700, 1100, 160) | {"characters": "SAMPLE CLIENT
Pitch Deck"}]
    with open(os.path.join(ROOT, "deck.json"), "w", encoding="utf-8") as f:
        json.dump({"deck": deck, "files": files}, f, indent=1)
    print("Samples written to", ROOT)


if __name__ == "__main__":
    main()
