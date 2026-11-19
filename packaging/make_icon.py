"""App icon: an iOS-style squircle in Steelworks red with a black mark.

The mark is built from the Steelworks pinwheel: circles with one square
quarter cut away, turning around the centre. Two of them (top and bottom)
read as an "S" on their own.

  python packaging/make_icon.py            -> pixel "SW" in black (default)
  python packaging/make_icon.py s          -> the S from two pinwheel pieces
  python packaging/make_icon.py pinwheel   -> all four pieces

Writes icon.png (1024), icon.ico, icon.icns, icon-256.png and the app's UI icon.
"""
import os
import sys

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
RED_TOP = (255, 82, 36)
RED_BOTTOM = (240, 60, 16)
INK = (17, 17, 17)


def squircle_mask(size: int, inset: int, n: float = 5.0, ss: int = 4) -> Image.Image:
    """Continuous-corner rounded square (superellipse), like an iOS/macOS icon."""
    big = size * ss
    m = Image.new("L", (big, big), 0)
    px = m.load()
    c, r = big / 2, big / 2 - inset * ss
    for y in range(big):
        dy = abs((y + 0.5 - c) / r)
        if dy >= 1:
            continue
        half = (1 - dy ** n) ** (1 / n) * r
        for x in range(max(0, int(c - half)), min(big, int(c + half) + 1)):
            px[x, y] = 255
    return m.resize((size, size), Image.LANCZOS)


def piece(draw: ImageDraw.ImageDraw, cx: float, cy: float, r: float, cut: int) -> None:
    """A circle with one square quarter removed. cut: 0=bottom-right, 1=bottom-left, 2=top-left, 3=top-right."""
    # Draw the three remaining quarters as pie slices (PIL angles: 0 = east, clockwise).
    start = {0: 90, 1: 180, 2: 270, 3: 0}[cut]          # the quarter we skip begins here, going back 90 deg
    missing = (start - 90) % 360
    draw.pieslice((cx - r, cy - r, cx + r, cy + r), missing + 90, missing + 360, fill=255)


# 5 x 7 pixel glyphs for the "SW" monogram
GLYPH_S = [".###.", "#...#", "#....", ".###.", "....#", "#...#", ".###."]
GLYPH_W = ["#...#", "#...#", "#...#", "#.#.#", "#.#.#", "##.##", "#...#"]


def mark(size: int, style: str) -> Image.Image:
    ss = 4
    S = size * ss
    m = Image.new("L", (S, S), 0)
    d = ImageDraw.Draw(m)
    if style == "sw":
        # Pixel-built SW (11 x 7 cells), solid strokes, centred.
        inset = S * 0.1
        cell = round((S - 2 * inset) * 0.60 / 11)
        ox, oy = (S - 11 * cell) // 2, (S - 7 * cell) // 2
        for gi, glyph in enumerate((GLYPH_S, GLYPH_W)):
            for ry, row in enumerate(glyph):
                for rx, ch in enumerate(row):
                    if ch == "#":
                        x0, y0 = ox + (gi * 6 + rx) * cell, oy + ry * cell
                        d.rectangle((x0, y0, x0 + cell, y0 + cell), fill=255)
        return m.resize((size, size), Image.LANCZOS)
    if style == "s":
        # Two pieces stacked so their cut corners face each other: reads as an S.
        r = S * 0.17
        layout = [((-0.5, -1.0), 0), ((0.5, 1.0), 2)]
    else:
        # The full pinwheel, like the Steelworks mark.
        r = S * 0.112
        layout = [((-0.54, -1.6), 0), ((1.6, -0.54), 1), ((0.54, 1.6), 2), ((-1.6, 0.54), 3)]
    for (ox, oy), cut in layout:
        piece(d, S / 2 + ox * r, S / 2 + oy * r, r, cut)
    return m.resize((size, size), Image.LANCZOS)


def draw(size: int = 1024, style: str = "sw") -> Image.Image:
    inset = round(size * 0.1)
    bg = Image.new("RGB", (size, size))
    g = ImageDraw.Draw(bg)
    for y in range(size):
        t = y / (size - 1)
        g.line([(0, y), (size, y)], fill=tuple(round(RED_TOP[i] + (RED_BOTTOM[i] - RED_TOP[i]) * t) for i in range(3)))
    icon = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    icon.paste(bg, (0, 0), squircle_mask(size, inset))
    from PIL import ImageChops
    shape = squircle_mask(size, inset)
    icon.paste(Image.new("RGBA", (size, size), INK + (255,)), (0, 0), ImageChops.multiply(mark(size, style), shape))
    return icon


if __name__ == "__main__":
    style = sys.argv[1] if len(sys.argv) > 1 else "sw"
    big = draw(1024, style)
    big.save(os.path.join(HERE, "icon.png"))
    big.save(os.path.join(HERE, "icon.ico"), sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    big.save(os.path.join(HERE, "icon.icns"))
    big.resize((256, 256), Image.LANCZOS).save(os.path.join(HERE, "icon-256.png"))
    draw(128, style).save(os.path.join(HERE, "..", "app", "smo", "ui", "icon.png"))
    print("icons written:", style)
