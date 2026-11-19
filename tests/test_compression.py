import io
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from PIL import Image, ImageDraw  # noqa: E402

from smo import images, video  # noqa: E402

FFMPEG = video.find_tool("ffmpeg")
FFPROBE = video.find_tool("ffprobe")


def noisy(w, h, mode="RGB", alpha_hole=False):
    """Photo-like content (smooth gradients, soft shapes, fine grain) so encoders
    and the quality check behave like they do on real slide images."""
    from PIL import ImageFilter
    g = Image.linear_gradient("L").resize((w, h))
    rgb = Image.merge("RGB", (g, g.rotate(90).resize((w, h)), g.transpose(Image.FLIP_LEFT_RIGHT)))
    d = ImageDraw.Draw(rgb)
    for i in range(12):
        x, y = (i * 389) % w, (i * 211) % h
        d.ellipse((x, y, x + w // 5, y + h // 4), fill=((i * 40) % 255, 120, 220 - i * 15))
    rgb = rgb.filter(ImageFilter.GaussianBlur(max(w, h) / 300))
    grain = Image.effect_noise((w, h), 6).convert("RGB")
    rgb = Image.blend(rgb, grain, 0.05)
    if mode == "RGBA":
        rgba = rgb.convert("RGBA")
        if alpha_hole:
            ImageDraw.Draw(rgba).ellipse((w // 4, h // 4, w // 2, h // 2), fill=(0, 0, 0, 0))
        return rgba
    return rgb


def png_bytes(im):
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


SIZE = {k: {"scale": v["scale"], "max_edge": v["max_edge"]} for k, v in images.PRESETS.items()}


class TestPlanSize(unittest.TestCase):
    def test_best_is_twice_display(self):
        p = images.plan_size(4000, 2000, 1000, 500, **SIZE["best"])
        self.assertEqual((p.target_w, p.target_h), (2000, 1000))

    def test_smaller_is_one_and_a_half_times_display(self):
        p = images.plan_size(4000, 2000, 1000, 500, **SIZE["smaller"])
        self.assertEqual((p.target_w, p.target_h), (1500, 750))

    def test_never_upscales(self):
        p = images.plan_size(800, 600, 1920, 1080)
        self.assertEqual((p.target_w, p.target_h), (800, 600))
        self.assertEqual(p.scale, 1.0)

    def test_caps_long_edge(self):
        p = images.plan_size(8000, 4000, 1920, 960, **SIZE["smaller"])   # 1.5x would be 2880
        self.assertEqual((p.target_w, p.target_h), (2560, 1280))
        p = images.plan_size(8000, 4000, 2400, 1200, **SIZE["best"])     # 2x would be 4800
        self.assertEqual((p.target_w, p.target_h), (4096, 2048))

    def test_crop_need_uses_larger_axis(self):
        # Shown cropped: needs full 1.5x of the tighter axis so the crop stays sharp.
        p = images.plan_size(3000, 3000, 600, 1200, **SIZE["smaller"])
        self.assertEqual((p.target_w, p.target_h), (1800, 1800))

    def test_tiny_difference_is_not_resampled(self):
        p = images.plan_size(1501, 751, 1000, 500, **SIZE["smaller"])
        self.assertEqual((p.target_w, p.target_h), (1501, 751))


class TestOptimise(unittest.TestCase):
    def test_png_photo_becomes_jpeg(self):
        src = png_bytes(noisy(3000, 2000))
        r = images.optimise(src, 960, 640)
        self.assertTrue(r.keep, r.reason)
        self.assertEqual(r.mime, "image/jpeg")
        self.assertEqual(r.out_size, "1920x1280")
        self.assertLess(r.out_bytes, 0.7 * r.src_bytes)
        self.assertGreaterEqual(r.psnr, images.PRESETS["best"]["min_psnr"])
        out = Image.open(io.BytesIO(r.data))
        self.assertEqual(out.size, (1920, 1280))
        self.assertEqual(out.layer[0][1] if hasattr(out, "layer") else 1, 1)  # 4:4:4: no chroma subsampling

    def test_smaller_preset_matches_original_spec(self):
        r = images.optimise(png_bytes(noisy(3000, 2000)), 960, 640, preset="smaller")
        self.assertTrue(r.keep, r.reason)
        self.assertEqual(r.out_size, "1440x960")

    def test_visible_quality_loss_is_refused(self):
        # Pure noise can't be stored lossily without visible change: the original must be kept.
        im = Image.effect_noise((1600, 1200), 90).convert("RGB")
        r = images.optimise(png_bytes(im), 800, 600)
        self.assertFalse(r.keep)               # kept: either it would look worse, or wouldn't be smaller
        self.assertIsNone(r.data)
        self.assertTrue("quality" in r.reason or "left as it is" in r.reason)

    def test_flat_graphics_are_lossless(self):
        im = Image.new("RGB", (2400, 1200), (255, 255, 255))
        d = ImageDraw.Draw(im)
        d.rectangle((200, 200, 1200, 800), fill=(10, 132, 255))
        d.text((1300, 500), "STEELWORKS", fill=(0, 0, 0))
        b = io.BytesIO()
        im.save(b, "BMP")                       # big, uncompressed source
        r = images.optimise(b.getvalue(), 600, 300)
        self.assertTrue(r.keep, r.reason)
        self.assertEqual(r.mime, "image/png")

    def test_real_alpha_becomes_webp(self):
        r = images.optimise(png_bytes(noisy(2400, 1600, "RGBA", alpha_hole=True)), 800, 533)
        self.assertTrue(r.keep, r.reason)
        self.assertTrue(r.alpha)
        self.assertEqual(r.mime, "image/webp")
        out = Image.open(io.BytesIO(r.data))
        self.assertIn("A", out.getbands())

    def test_opaque_rgba_is_treated_as_opaque(self):
        r = images.optimise(png_bytes(noisy(2400, 1600, "RGBA")), 800, 533)
        self.assertFalse(r.alpha)
        self.assertEqual(r.mime, "image/jpeg")

    def test_keeps_original_when_saving_under_30_percent(self):
        b = io.BytesIO()
        noisy(1200, 800).save(b, "JPEG", quality=70)
        r = images.optimise(b.getvalue(), 1200, 800)   # already small, no resize
        self.assertFalse(r.keep)
        self.assertIsNone(r.data)
        self.assertIn("left as it is", r.reason)

    def test_corrupt_input_is_reported_not_raised(self):
        r = images.optimise(b"not an image", 100, 100)
        self.assertFalse(r.keep)
        self.assertIn("Could not read", r.reason)

    def test_exif_rotated_gets_upright_copy_for_manual_placement(self):
        im = noisy(2000, 1000)
        ex = im.getexif()
        ex[0x0112] = 6
        b = io.BytesIO()
        im.save(b, "JPEG", quality=100, exif=ex)
        r = images.optimise(b.getvalue(), 300, 150)
        self.assertFalse(r.keep)
        self.assertTrue(r.manual)
        self.assertIn("rotation", r.reason)
        self.assertEqual(Image.open(io.BytesIO(r.data)).size[0] < Image.open(io.BytesIO(r.data)).size[1], True,
                         "the copy is upright (portrait)")

    def test_animated_gif_is_left_for_video_step(self):
        frames = [noisy(64, 64).convert("P") for _ in range(3)]
        b = io.BytesIO()
        frames[0].save(b, "GIF", save_all=True, append_images=frames[1:], loop=0)
        r = images.optimise(b.getvalue(), 64, 64)
        self.assertFalse(r.keep)
        self.assertIn("GIF", r.reason)

    def test_icc_profile_preserved(self):
        from PIL import ImageCms
        icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        b = io.BytesIO()
        noisy(2000, 1500).save(b, "PNG", icc_profile=icc)
        r = images.optimise(b.getvalue(), 500, 375)
        self.assertTrue(r.keep)
        self.assertEqual(Image.open(io.BytesIO(r.data)).info.get("icc_profile"), icc)


@unittest.skipUnless(FFMPEG and FFPROBE, "ffmpeg not installed")
class TestVideo(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.src = os.path.join(cls.tmp, "src.mov")
        subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=3840x2160:rate=25",
                        "-f", "lavfi", "-i", "sine=frequency=440", "-t", "2", "-c:v", "libx264", "-crf", "8",
                        "-c:a", "pcm_s16le", cls.src], check=True)
        cls.gif = os.path.join(cls.tmp, "anim.gif")
        subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=321x241:rate=10",
                        "-t", "1", cls.gif], check=True)

    def test_compress_1080p_h264_aac_faststart(self):
        out = os.path.join(self.tmp, "out.mp4")
        prog = []
        info = video.compress_video(FFMPEG, FFPROBE, self.src, out, progress=prog.append, preset="smaller")
        self.assertGreaterEqual(info.psnr, video.PRESETS["smaller"]["min_psnr"])
        p = video.probe(FFPROBE, out)
        self.assertEqual((p.width, p.height), (1920, 1080))
        self.assertEqual(p.codec, "h264")
        self.assertTrue(p.has_audio)
        self.assertLess(os.path.getsize(out), os.path.getsize(self.src))
        self.assertEqual(prog[-1], 1.0)
        with open(out, "rb") as f:
            head = f.read(4096)
        self.assertLess(head.find(b"moov"), head.find(b"mdat") if b"mdat" in head else 1 << 30,
                        "moov atom must come first (faststart)")
        self.assertNotEqual(head.find(b"moov"), -1)

    def test_best_keeps_4k_when_shown_full_screen(self):
        out = os.path.join(self.tmp, "best_full.mp4")
        info = video.compress_video(FFMPEG, FFPROBE, self.src, out, preset="best", display_long=1920)
        self.assertEqual(video.probe(FFPROBE, out).width, 3840)
        self.assertGreaterEqual(info.psnr, video.PRESETS["best"]["min_psnr"])

    def test_best_uses_1080p_when_shown_small(self):
        out = os.path.join(self.tmp, "best_small.mp4")
        video.compress_video(FFMPEG, FFPROBE, self.src, out, preset="best", display_long=500)
        self.assertEqual(video.probe(FFPROBE, out).width, 1920)

    def test_portrait_long_edge_capped(self):
        src = os.path.join(self.tmp, "portrait.mp4")
        subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=1440x2560:rate=25",
                        "-t", "1", src], check=True)
        out = os.path.join(self.tmp, "portrait_out.mp4")
        video.compress_video(FFMPEG, FFPROBE, src, out, preset="smaller")
        p = video.probe(FFPROBE, out)
        self.assertEqual((p.width, p.height), (1080, 1920))

    def test_small_video_not_upscaled(self):
        src = os.path.join(self.tmp, "small.mp4")
        subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25",
                        "-t", "1", src], check=True)
        out = os.path.join(self.tmp, "small_out.mp4")
        video.compress_video(FFMPEG, FFPROBE, src, out)
        self.assertEqual(video.probe(FFPROBE, out).width, 640)

    def test_gif_to_silent_even_mp4(self):
        out = os.path.join(self.tmp, "anim.mp4")
        info = video.gif_to_mp4(FFMPEG, FFPROBE, self.gif, out)
        self.assertGreaterEqual(info.psnr, video.PRESETS["best"]["min_psnr"])
        p = video.probe(FFPROBE, out)
        self.assertEqual(p.codec, "h264")
        self.assertFalse(p.has_audio)
        self.assertEqual((p.width % 2, p.height % 2), (0, 0))

    def test_bad_input_raises_readable_error(self):
        bad = os.path.join(self.tmp, "bad.mp4")
        open(bad, "wb").write(b"nope")
        with self.assertRaises(RuntimeError):
            video.compress_video(FFMPEG, FFPROBE, bad, os.path.join(self.tmp, "x.mp4"))

    def test_args_contain_required_settings(self):
        a = " ".join(video.compress_args("ffmpeg", "in", "out"))
        for s in ("-crf 22", "-b:a 128k", "+faststart", "libx264", "aac"):
            self.assertIn(s, a)
        g = " ".join(video.gif_args("ffmpeg", "in", "out"))
        self.assertIn("-an", g)


if __name__ == "__main__":
    unittest.main()
