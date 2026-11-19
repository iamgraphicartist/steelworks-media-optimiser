"""Image recompression rules.

* Target = 1.5x the largest on-slide display size of the image (crop included),
  capped at 2560 px on the long edge and never upscaled.
* JPEG q86 for opaque images; WebP q88 when the image has real transparency.
* A result is only kept when it is at least 30 % smaller than the original.
"""
from __future__ import annotations

import io
from dataclasses import asdict, dataclass
from typing import Optional

from PIL import Image, ImageCms, ImageOps

Image.MAX_IMAGE_PIXELS = None

SCALE = 1.5
MAX_EDGE = 2560
MIN_SAVING = 0.30
JPEG_QUALITY = 86
WEBP_QUALITY = 88


@dataclass
class SizePlan:
    src_w: int
    src_h: int
    target_w: int
    target_h: int
    scale: float


def plan_size(src_w: int, src_h: int, need_w: float, need_h: float,
              scale: float = SCALE, max_edge: int = MAX_EDGE) -> SizePlan:
    """Pixel size to resize to. need_w/need_h is the display size (slide px) the
    whole image is shown at, after crop/fill maths done by the plugin."""
    if src_w <= 0 or src_h <= 0:
        raise ValueError("image has no pixels")
    if need_w <= 0 and need_h <= 0:
        s = 1.0
    else:
        s = max(scale * need_w / src_w, scale * need_h / src_h)
    s = min(s, 1.0, max_edge / max(src_w, src_h))
    tw, th = max(1, round(src_w * s)), max(1, round(src_h * s))
    # Within 2 px of the original is not worth a resample.
    if abs(tw - src_w) <= 2 and abs(th - src_h) <= 2:
        tw, th, s = src_w, src_h, 1.0
    return SizePlan(src_w, src_h, tw, th, round(s, 4))


def has_real_alpha(im: Image.Image) -> bool:
    """True only when at least one pixel is not fully opaque."""
    if im.mode in ("RGBA", "LA", "PA", "La", "RGBa") or (im.mode == "P" and "transparency" in im.info):
        alpha = im.convert("RGBA").getchannel("A")
        return alpha.getextrema()[0] < 255
    return False


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
                dst = ImageCms.createProfile("sRGB")
                return ImageCms.profileToProfile(im, src, dst, outputMode="RGB"), None
            except Exception:
                pass
        return im.convert("RGB"), None
    if im.mode in ("I;16", "I;16B", "I;16L", "I", "F"):
        return ImageOps.autocontrast(im.convert("I").point(lambda v: v / 256)).convert("L"), icc
    return im, icc


def optimise(data: bytes, need_w: float, need_h: float, min_saving: float = MIN_SAVING) -> OptimiseResult:
    """Recompress one image. Never raises for bad input: returns keep=False with a reason."""
    n = len(data)
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception as e:  # corrupt or unsupported (SVG, PSD...)
        return OptimiseResult(False, f"Could not read this image ({type(e).__name__}).", n, n, "?", "?", False)
    W, H = im.size
    if getattr(im, "is_animated", False) and getattr(im, "n_frames", 1) > 1:
        return OptimiseResult(False, "Animated - handled as a GIF-to-video conversion.", n, n, f"{W}x{H}", f"{W}x{H}", False)
    try:
        orientation = im.getexif().get(0x0112, 1)
    except Exception:
        orientation = 1
    if orientation not in (0, 1):
        return OptimiseResult(False, "Has a camera rotation tag - replace by hand to be safe.", n, n, f"{W}x{H}", f"{W}x{H}", False)

    plan = plan_size(W, H, need_w, need_h)
    alpha = has_real_alpha(im)
    im, icc = _to_srgb(im)
    img = im.convert("RGBA" if alpha else "RGB")
    if (plan.target_w, plan.target_h) != (W, H):
        img = img.resize((plan.target_w, plan.target_h), Image.LANCZOS)
    buf = io.BytesIO()
    kw = {"icc_profile": icc} if icc else {}
    if alpha:
        img.save(buf, "WEBP", quality=WEBP_QUALITY, method=6, **kw)
        mime, ext = "image/webp", "webp"
    else:
        img.save(buf, "JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True, **kw)
        mime, ext = "image/jpeg", "jpg"
    out = buf.getvalue()
    saving = 1 - len(out) / n if n else 0
    keep = saving >= min_saving
    reason = (f"{round(saving * 100)}% smaller" if keep
              else f"Only {max(0, round(saving * 100))}% smaller - left as it is.")
    return OptimiseResult(keep, reason, n, len(out), f"{W}x{H}", f"{plan.target_w}x{plan.target_h}",
                          alpha, mime, ext, out if keep else None)
