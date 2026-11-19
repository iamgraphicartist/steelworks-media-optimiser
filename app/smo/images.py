"""Image recompression — quality first.

Two presets (Settings › Quality):

* ``best`` (default, visually lossless): 2x the largest on-slide display size
  (sharp on high-res screens), capped at 4096 px, never upscaled. JPEG q92 with
  full colour resolution (4:4:4), WebP q95 when the image really has
  transparency, and flat graphics (logos, UI, diagrams) saved losslessly.
* ``smaller`` (the original manual-run spec): 1.5x, capped at 2560 px,
  JPEG q86 / WebP q88.

Every result is decoded again and compared with the original at the new size
(PSNR). If it would visibly lose quality, or isn't at least 30 % smaller, the
original is kept. So the tool can only ever make an image smaller, never worse.
"""
from __future__ import annotations

import io
import math
from dataclasses import asdict, dataclass
from typing import Optional

from PIL import Image, ImageChops, ImageCms, ImageOps, ImageStat

Image.MAX_IMAGE_PIXELS = None

PRESETS = {
    "best":    {"scale": 2.0, "max_edge": 4096, "jpeg_q": 92, "subsampling": 0, "webp_q": 95, "min_psnr": 40.0},
    "smaller": {"scale": 1.5, "max_edge": 2560, "jpeg_q": 86, "subsampling": 2, "webp_q": 88, "min_psnr": 36.0},
}
DEFAULT_PRESET = "best"
MIN_SAVING = 0.30
FLAT_MAX_COLOURS = 256          # images with this few colours are graphics: save them losslessly

# Kept for callers that read the old names.
SCALE, MAX_EDGE = PRESETS["smaller"]["scale"], PRESETS["smaller"]["max_edge"]


@dataclass
class SizePlan:
    src_w: int
    src_h: int
    target_w: int
    target_h: int
    scale: float


def plan_size(src_w: int, src_h: int, need_w: float, need_h: float,
              scale: float = 2.0, max_edge: int = 4096) -> SizePlan:
    """Pixel size to resize to. need_w/need_h is the size (slide px) the whole
    image is displayed at, after the plugin's crop/fill maths."""
    if src_w <= 0 or src_h <= 0:
        raise ValueError("image has no pixels")
    if need_w <= 0 and need_h <= 0:
        s = 1.0
    else:
        s = max(scale * need_w / src_w, scale * need_h / src_h)
    s = min(s, 1.0, max_edge / max(src_w, src_h))
    tw, th = max(1, round(src_w * s)), max(1, round(src_h * s))
    if abs(tw - src_w) <= 2 and abs(th - src_h) <= 2:   # not worth a resample
        tw, th, s = src_w, src_h, 1.0
    return SizePlan(src_w, src_h, tw, th, round(s, 4))


def has_real_alpha(im: Image.Image) -> bool:
    """True only when at least one pixel is not fully opaque."""
    if im.mode in ("RGBA", "LA", "PA", "La", "RGBa") or (im.mode == "P" and "transparency" in im.info):
        return im.convert("RGBA").getchannel("A").getextrema()[0] < 255
    return False


def is_flat_graphic(im: Image.Image) -> bool:
    """Logos, UI shots, diagrams: few distinct colours. JPEG would smear their edges."""
    probe = im.convert("RGBA")
    if max(probe.size) > 1024:
        probe = probe.resize((max(1, probe.width * 1024 // max(probe.size)), max(1, probe.height * 1024 // max(probe.size))),
                             Image.NEAREST)
    return probe.getcolors(maxcolors=FLAT_MAX_COLOURS) is not None


def psnr(a: Image.Image, b: Image.Image) -> float:
    """Peak signal-to-noise ratio in dB (higher = closer; 40+ is visually identical)."""
    if a.size != b.size:
        b = b.resize(a.size, Image.LANCZOS)
    a, b = _flatten(a), _flatten(b)
    rms = ImageStat.Stat(ImageChops.difference(a, b)).rms
    mse = sum(r * r for r in rms) / len(rms)
    return float("inf") if mse == 0 else 10 * math.log10(255 * 255 / mse)


def _flatten(im: Image.Image) -> Image.Image:
    """Compare transparent images as they'd look on a mid-grey slide."""
    if "A" in im.getbands():
        bg = Image.new("RGBA", im.size, (128, 128, 128, 255))
        return Image.alpha_composite(bg, im.convert("RGBA")).convert("RGB")
    return im.convert("RGB")


@dataclass
class OptimiseResult:
    keep: bool
    reason: str
    src_bytes: int
    out_bytes: int
    src_size: str
    out_size: str
    alpha: bool
    mime: Optional[str] = None
    ext: Optional[str] = None
    data: Optional[bytes] = None
    psnr: Optional[float] = None
    manual: bool = False          # a good file was made, but it must be placed by hand

    def summary(self) -> dict:
        d = asdict(self)
        d.pop("data")
        return d


def _to_srgb(im: Image.Image) -> tuple[Image.Image, Optional[bytes]]:
    icc = im.info.get("icc_profile")
    if im.mode == "CMYK":
        if icc:
            try:
                src = ImageCms.ImageCmsProfile(io.BytesIO(icc))
                return ImageCms.profileToProfile(im, src, ImageCms.createProfile("sRGB"), outputMode="RGB"), None
            except Exception:
                pass
        return im.convert("RGB"), None
    if im.mode in ("I;16", "I;16B", "I;16L", "I", "F"):
        return ImageOps.autocontrast(im.convert("I").point(lambda v: v / 256)).convert("L"), icc
    return im, icc


def optimise(data: bytes, need_w: float, need_h: float, preset: str = DEFAULT_PRESET,
             min_saving: float = MIN_SAVING) -> OptimiseResult:
    """Recompress one image. Never raises for bad input: returns keep=False with a reason."""
    P = PRESETS.get(preset, PRESETS[DEFAULT_PRESET])
    n = len(data)
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception as e:  # corrupt or unsupported (SVG, PSD...)
        return OptimiseResult(False, f"Could not read this image ({type(e).__name__}).", n, n, "?", "?", False)
    W, H = im.size
    if getattr(im, "is_animated", False) and getattr(im, "n_frames", 1) > 1:
        return OptimiseResult(False, "Animated - handled as a GIF-to-video conversion.", n, n, f"{W}x{H}", f"{W}x{H}", False)
    manual = False
    try:
        orientation = im.getexif().get(0x0112, 1)
    except Exception:
        orientation = 1
    if orientation not in (0, 1):
        # Make an upright, optimised copy for a person to place; never swap it automatically.
        im = ImageOps.exif_transpose(im)
        manual = True
        W, H = im.size
        need_w, need_h = (need_h, need_w) if orientation in (5, 6, 7, 8) else (need_w, need_h)

    plan = plan_size(W, H, need_w, need_h, P["scale"], P["max_edge"])
    alpha = has_real_alpha(im)
    flat = is_flat_graphic(im)
    im, icc = _to_srgb(im)
    ref = im.convert("RGBA" if alpha else "RGB")
    if (plan.target_w, plan.target_h) != (W, H):
        ref = ref.resize((plan.target_w, plan.target_h), Image.LANCZOS)
    buf = io.BytesIO()
    kw = {"icc_profile": icc} if icc else {}
    if flat:
        ref.save(buf, "PNG", optimize=True, **kw)
        mime, ext = "image/png", "png"
    elif alpha:
        ref.save(buf, "WEBP", quality=P["webp_q"], method=6, **kw)
        mime, ext = "image/webp", "webp"
    else:
        ref.save(buf, "JPEG", quality=P["jpeg_q"], subsampling=P["subsampling"], optimize=True, progressive=True, **kw)
        mime, ext = "image/jpeg", "jpg"
    out = buf.getvalue()
    score = psnr(ref, Image.open(io.BytesIO(out)))
    if score < P["min_psnr"] and mime == "image/jpeg":
        # Borderline (very fine detail): one more try at near-maximum quality before giving up.
        buf = io.BytesIO()
        ref.save(buf, "JPEG", quality=97, subsampling=0, optimize=True, progressive=True, **kw)
        out = buf.getvalue()
        score = psnr(ref, Image.open(io.BytesIO(out)))
    saving = 1 - len(out) / n if n else 0
    size = (f"{W}x{H}", f"{plan.target_w}x{plan.target_h}")
    if score < P["min_psnr"]:
        return OptimiseResult(False, "Would lose visible quality - left as it is.", n, len(out), *size, alpha,
                              psnr=round(score, 1))
    if manual:
        return OptimiseResult(False, "Has a camera rotation tag - an upright copy is ready to place by hand.",
                              n, len(out), *size, alpha, mime, ext, out, round(score, 1), manual=True)
    keep = saving >= min_saving
    reason = (f"{round(saving * 100)}% smaller, no visible change" if keep
              else f"Only {max(0, round(saving * 100))}% smaller - left as it is.")
    return OptimiseResult(keep, reason, n, len(out), *size, alpha, mime, ext, out if keep else None,
                          round(score, 1) if score != float("inf") else 99.0)
