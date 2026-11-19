"""Draw the app icon: a soft squircle glowing from deep blue through pale pink to
coral red, with "SW" in white. Writes icon.png (1024), icon.ico and icon.icns."""
import os
import sys

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
S = 1024

# Vertical colour stops (position 0..1, RGB), sampled from the reference look.
STOPS = [(0.00, (14, 112, 210)), (0.36, (28, 152, 228)), (0.50, (140, 188, 236)),
         (0.60, (226, 214, 238)), (0.74, (240, 206, 228)), (0.84, (240, 150, 168)),
         (0.93, (222, 72, 62)), (1.00, (150, 30, 24))]


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def gradient(size):
    col = Image.new("RGB", (1, size))
    for y in range(size):
        t = y / (size - 1)
        for (p0, c0), (p1, c1) in zip(STOPS, STOPS[1:]):
            if p0 <= t <= p1:
                col.putpixel((0, y), lerp(c0, c1, (t - p0) / (p1 - p0)))
                break
    return col.resize((size, size))


def squircle(size, inset, n=4.2):
    """Superellipse mask |x|^n + |y|^n <= 1 (the soft, puffy Apple-like shape)."""
    m = Image.new("L", (size, size), 0)
    px = m.load()
    r = size / 2 - inset
    c = size / 2
    for y in range(size):
        dy = abs((y + 0.5 - c) / r)
        if dy > 1:
            continue
        dx_max = (1 - dy ** n) ** (1 / n)
        x0, x1 = int(c - dx_max * r), int(c + dx_max * r)
        for x in range(max(0, x0), min(size, x1 + 1)):
            px[x, y] = 255
    return m


def font(px):
    cands = ["/System/Library/Fonts/SFNSRounded.ttf", "/System/Library/Fonts/SFNS.ttf",
             "/Library/Fonts/SF-Pro-Display-Bold.otf", "/System/Library/Fonts/Helvetica.ttc",
             r"C:\Windows\Fonts\segoeuib.ttf", r"C:\Windows\Fonts\seguisb.ttf", r"C:\Windows\Fonts\arialbd.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
    for c in cands:
        if os.path.exists(c):
            try:
                f = ImageFont.truetype(c, px)
                try:
                    f.set_variation_by_name("Bold")
                except Exception:
                    pass
                return f
            except OSError:
                continue
    return ImageFont.load_default()


def draw(size=S):
    inset = int(size * 0.085)                  # macOS icon grid: ~10% transparent margin
    shape = squircle(size, inset)
    body = gradient(size).convert("RGBA")
    # Soft edges: feather the mask a little, and darken the rim so it glows like the reference.
    soft = shape.filter(ImageFilter.GaussianBlur(size * 0.004))
    rim = ImageChops.subtract(shape, shape.filter(ImageFilter.GaussianBlur(size * 0.05)))
    shade = Image.new("RGBA", (size, size), (10, 20, 60, 0))
    shade.putalpha(rim.point(lambda v: int(v * 0.22)))
    body = Image.alpha_composite(body, shade)
    # Warm outer glow at the bottom.
    glow = Image.new("RGBA", (size, size), (225, 60, 40, 0))
    gmask = shape.filter(ImageFilter.GaussianBlur(size * 0.055))
    lower = Image.linear_gradient("L").resize((size, size)).point(lambda v: min(255, max(0, v - 110) * 2))
    glow.putalpha(ImageChops.multiply(gmask, lower).point(lambda v: int(v * 0.95)))

    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out = Image.alpha_composite(out, glow)
    body.putalpha(soft)
    out = Image.alpha_composite(out, body)

    # "SW" lettering, white with a gentle shadow, optically centred slightly high.
    f = font(int(size * 0.36))
    txt = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(txt)
    box = d.textbbox((0, 0), "SW", font=f)
    w, h = box[2] - box[0], box[3] - box[1]
    pos = ((size - w) / 2 - box[0], (size - h) / 2 - box[1] - size * 0.02)
    shadow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).text((pos[0], pos[1] + size * 0.012), "SW", font=f, fill=(20, 30, 80, 90))
    out = Image.alpha_composite(out, shadow.filter(ImageFilter.GaussianBlur(size * 0.015)))
    d.text(pos, "SW", font=f, fill=(255, 255, 255, 245))
    return Image.alpha_composite(out, txt)


if __name__ == "__main__":
    big = draw(S)
    big.save(os.path.join(HERE, "icon.png"))
    big.save(os.path.join(HERE, "icon.ico"), sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    big.save(os.path.join(HERE, "icon.icns"))
    ui = os.path.join(HERE, "..", "app", "smo", "ui")
    big.resize((128, 128), Image.LANCZOS).save(os.path.join(ui, "icon.png"))
    big.resize((256, 256), Image.LANCZOS).save(os.path.join(HERE, "icon-256.png"))
    print("icons written")
