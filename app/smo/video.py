"""ffmpeg wrappers: video compression (1080p, H.264 CRF 22, AAC 128k, faststart)
and GIF -> silent MP4."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable, Optional

MAX_SIDE = 1920          # long edge; 1920x1080 for landscape, 1080x1920 for portrait
CRF = 22
AUDIO_BITRATE = "128k"
MIN_SAVING = 0.10        # videos already well encoded are left alone

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def _bundled(name: str) -> Optional[str]:
    exe = name + (".exe" if sys.platform == "win32" else "")
    roots = [getattr(sys, "_MEIPASS", None), os.path.dirname(sys.executable),
             os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "vendor", "ffmpeg")]
    for r in roots:
        if r:
            for p in (os.path.join(r, exe), os.path.join(r, "ffmpeg", exe)):
                if os.path.isfile(p):
                    return os.path.abspath(p)
    return None


def find_tool(name: str, configured: Optional[str] = None) -> Optional[str]:
    if configured and os.path.isfile(configured):
        return configured
    p = _bundled(name) or shutil.which(name)
    if p:
        return p
    for d in ("/opt/homebrew/bin", "/usr/local/bin"):
        cand = os.path.join(d, name)
        if os.path.isfile(cand):
            return cand
    return None


SCALE_FILTER = (f"scale='if(gt(iw,ih),min({MAX_SIDE},iw),-2)':'if(gt(iw,ih),-2,min({MAX_SIDE},ih))'"
                ":flags=lanczos")


def compress_args(ffmpeg: str, src: str, out: str) -> list[str]:
    return [ffmpeg, "-hide_banner", "-nostdin", "-y", "-i", src,
            "-map", "0:v:0", "-map", "0:a:0?",
            "-vf", SCALE_FILTER + ",format=yuv420p",
            "-c:v", "libx264", "-preset", "slow", "-crf", str(CRF), "-profile:v", "high",
            "-c:a", "aac", "-b:a", AUDIO_BITRATE, "-ac", "2",
            "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", out]


def gif_args(ffmpeg: str, src: str, out: str) -> list[str]:
    # GIF frame sizes may be odd; H.264 4:2:0 needs even dimensions.
    vf = (f"scale='min({MAX_SIDE},iw)':'min({MAX_SIDE},ih)':force_original_aspect_ratio=decrease:flags=lanczos,"
          "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p")
    return [ffmpeg, "-hide_banner", "-nostdin", "-y", "-i", src, "-an", "-vf", vf,
            "-c:v", "libx264", "-preset", "slow", "-crf", str(CRF), "-profile:v", "high",
            "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", out]


@dataclass
class Probe:
    duration: float
    width: int
    height: int
    has_audio: bool
    codec: str


def probe(ffprobe: str, path: str) -> Probe:
    r = subprocess.run([ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
                       capture_output=True, text=True, creationflags=_NO_WINDOW)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[-300:] or "ffprobe failed")
    j = json.loads(r.stdout)
    v = next((s for s in j.get("streams", []) if s.get("codec_type") == "video"), {})
    a = any(s.get("codec_type") == "audio" for s in j.get("streams", []))
    dur = float(j.get("format", {}).get("duration") or v.get("duration") or 0)
    return Probe(dur, int(v.get("width") or 0), int(v.get("height") or 0), a, v.get("codec_name", ""))


def run(args: list[str], duration: float = 0.0, progress: Optional[Callable[[float], None]] = None,
        cancelled: Optional[Callable[[], bool]] = None) -> None:
    """Run ffmpeg, reporting 0..1 progress from its -progress output."""
    import threading

    err_tail: list[str] = []
    with subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                          encoding="utf-8", errors="replace", creationflags=_NO_WINDOW) as p:
        def drain():
            for line in p.stderr:  # type: ignore[union-attr]
                err_tail.append(line)
                del err_tail[:-30]

        t = threading.Thread(target=drain, daemon=True)
        t.start()
        for line in p.stdout:  # type: ignore[union-attr]
            if cancelled and cancelled():
                p.kill()
                raise RuntimeError("Cancelled")
            if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                try:
                    us = int(line.split("=", 1)[1])
                except ValueError:
                    continue
                if progress and duration > 0:
                    progress(min(1.0, us / 1e6 / duration))
        p.wait()
        t.join(timeout=2)
    if p.returncode != 0:
        raise RuntimeError("ffmpeg: " + "".join(err_tail).strip()[-400:])
    if progress:
        progress(1.0)


def compress_video(ffmpeg: str, ffprobe: str, src: str, out: str, progress=None, cancelled=None) -> Probe:
    info = probe(ffprobe, src)
    run(compress_args(ffmpeg, src, out), info.duration, progress, cancelled)
    return info


def gif_to_mp4(ffmpeg: str, ffprobe: str, src: str, out: str, progress=None, cancelled=None) -> Probe:
    try:
        info = probe(ffprobe, src)
    except RuntimeError:
        info = Probe(0, 0, 0, False, "gif")
    run(gif_args(ffmpeg, src, out), info.duration, progress, cancelled)
    return info
