"""Tests for the shared DPR2 segmented screenshot pipeline."""

from __future__ import annotations

import asyncio
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image, ImageChops

import keylol_browser
import screenshot_capture
import screenshot_safety
import tieba_browser


DEVICE_SCALE_FACTOR = screenshot_safety.DEVICE_SCALE_FACTOR
MAX_IMAGE_DIMENSION = screenshot_safety.MAX_IMAGE_DIMENSION
MAX_IMAGE_PIXELS = screenshot_safety.MAX_IMAGE_PIXELS


def _png_bytes(image: Image.Image) -> bytes:
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _solid_png(width: int, height: int, colour=(255, 255, 255)) -> bytes:
    return _png_bytes(Image.new("RGB", (width, height), colour))


def _row_colour(y: int) -> tuple[int, int, int]:
    return ((y // 251) % 256, (y * 7) % 256, (y * 13 + 37) % 256)


def _row_pattern_png(width: int, height: int, start_y: int = 0) -> bytes:
    image = Image.new("RGB", (width, height))
    pixels = image.load()
    for y in range(height):
        colour = _row_colour(start_y + y)
        for x in range(width):
            pixels[x, y] = colour
    return _png_bytes(image)


def _fine_pattern_image(width: int, height: int) -> Image.Image:
    image = Image.new("RGB", (width, height))
    pixels = image.load()
    for y in range(height):
        for x in range(width):
            pixels[x, y] = (
                (y * 13 + x * 17) % 256,
                (y * 7 + x * 31) % 256,
                (y + x * 53) % 256,
            )
    return image


class ScreenshotSegmentPlanningTests(unittest.TestCase):
    def test_plans_short_exact_and_partial_segments_in_css_pixels(self):
        self.assertEqual(
            screenshot_capture._plan_screenshot_segments(1234),
            [(0, 1234)],
        )
        self.assertEqual(
            screenshot_capture._plan_screenshot_segments(4000, 2000),
            [(0, 2000), (2000, 2000)],
        )
        self.assertEqual(
            screenshot_capture._plan_screenshot_segments(4501, 2000),
            [(0, 2000), (2000, 2000), (4000, 501)],
        )

    def test_explicit_page_height_limit_rejects_instead_of_truncating(self):
        with self.assertRaisesRegex(ValueError, "exceeds safety limit"):
            screenshot_capture._plan_screenshot_segments(
                100_001,
                max_total_height=100_000,
            )


class ScreenshotImageSafetyTests(unittest.TestCase):
    def test_safe_size_keeps_native_dimensions_until_a_lock_is_hit(self):
        self.assertEqual(screenshot_safety._safe_image_size(780, 5000), (780, 5000))
        self.assertEqual(screenshot_safety._safe_image_size(20_000, 500), (16_384, 409))
        self.assertEqual(screenshot_safety._safe_image_size(5000, 5000), (4472, 4472))

    def test_safe_size_rounds_down_and_never_upscales(self):
        for source_size in ((1, 1), (320, 480), (780, 5000), (20_000, 2000)):
            with self.subTest(source_size=source_size):
                result = screenshot_safety._safe_image_size(*source_size)
                self.assertLessEqual(result[0], source_size[0])
                self.assertLessEqual(result[1], source_size[1])
                self.assertLessEqual(max(result), MAX_IMAGE_DIMENSION)
                self.assertLessEqual(result[0] * result[1], MAX_IMAGE_PIXELS)


class ScreenshotSegmentStitchingTests(unittest.TestCase):
    def test_short_images_keep_every_dpr2_pixel_at_supported_widths(self):
        expected_physical_widths = {320: 640, 390: 780, 440: 880}
        for css_width, physical_width in expected_physical_widths.items():
            with self.subTest(css_width=css_width):
                source = _fine_pattern_image(physical_width, 72)
                stitched_bytes = screenshot_capture._stitch_screenshot_segments(
                    [(0, _png_bytes(source))],
                    total_height=36,
                    css_width=css_width,
                )
                with Image.open(io.BytesIO(stitched_bytes)) as stitched:
                    self.assertEqual(stitched.size, (physical_width, 72))
                    self.assertIsNone(ImageChops.difference(source, stitched).getbbox())

    def test_tail_crop_uses_the_actual_non_integer_capture_scale(self):
        # 13 physical rows for a 7 CSS px viewport is deliberately not an
        # integer DPR. Cropping the last 3 CSS px must use that measured ratio.
        source = Image.new("RGB", (4, 13))
        pixels = source.load()
        for y in range(source.height):
            for x in range(source.width):
                pixels[x, y] = (y, y * 3, 255 - y)

        cropped_bytes = screenshot_capture._crop_screenshot_segment(
            _png_bytes(source),
            viewport_height=7,
            crop_top=4,
            crop_height=3,
        )

        with Image.open(io.BytesIO(cropped_bytes)) as cropped:
            self.assertEqual(cropped.size, (4, 6))
            self.assertEqual(
                [cropped.getpixel((0, y)) for y in range(cropped.height)],
                [(y, y * 3, 255 - y) for y in range(7, 13)],
            )

    def test_dpr1_segment_is_rejected_instead_of_being_upscaled(self):
        css_width = 8
        with self.assertRaises(ValueError):
            screenshot_capture._stitch_screenshot_segments(
                [(0, _solid_png(css_width, 20))],
                total_height=10,
                css_width=css_width,
            )

    def test_scaled_band_seams_match_one_whole_image_lanczos_resize(self):
        total_height = 10_003
        css_width = 10
        plans = screenshot_capture._plan_screenshot_segments(total_height, 997)
        colours = [(10 + index * 10, 80, 160) for index in range(len(plans))]
        parts = [
            (top, _solid_png(css_width * DEVICE_SCALE_FACTOR,
                             height * DEVICE_SCALE_FACTOR, colour))
            for (top, height), colour in zip(plans, colours)
        ]

        # Building this unbounded reference is test-only. Production stitching
        # must allocate only the already-limited destination and bounded strips.
        reference = Image.new(
            "RGB",
            (css_width * DEVICE_SCALE_FACTOR, total_height * DEVICE_SCALE_FACTOR),
        )
        for (top, height), colour in zip(plans, colours):
            reference.paste(
                Image.new(
                    "RGB",
                    (css_width * DEVICE_SCALE_FACTOR, height * DEVICE_SCALE_FACTOR),
                    colour,
                ),
                (0, top * DEVICE_SCALE_FACTOR),
            )
        expected_size = screenshot_safety._safe_image_size(*reference.size)
        reference = reference.resize(expected_size, Image.Resampling.LANCZOS)

        result = screenshot_capture._stitch_screenshot_segments(
            parts,
            total_height,
            css_width=css_width,
        )

        with Image.open(io.BytesIO(result)) as stitched:
            self.assertEqual(stitched.size, expected_size)
            for y in range(stitched.height):
                self.assertLessEqual(
                    max(
                        abs(left - right)
                        for left, right in zip(
                            stitched.getpixel((0, y)),
                            reference.getpixel((0, y)),
                        )
                    ),
                    1,
                    f"row {y} differs from whole-image resampling",
                )

    def test_scaled_fine_detail_across_boundaries_matches_reference(self):
        total_height = 8201
        css_width = 4
        physical_width = css_width * DEVICE_SCALE_FACTOR
        original = _fine_pattern_image(
            physical_width,
            total_height * DEVICE_SCALE_FACTOR,
        )
        plans = screenshot_capture._plan_screenshot_segments(total_height, 2000)
        parts = []
        for top, height in plans:
            segment = original.crop(
                (
                    0,
                    top * DEVICE_SCALE_FACTOR,
                    physical_width,
                    (top + height) * DEVICE_SCALE_FACTOR,
                )
            )
            parts.append((top, _png_bytes(segment)))

        expected_size = screenshot_safety._safe_image_size(
            physical_width,
            total_height * DEVICE_SCALE_FACTOR,
        )
        # The global reference uses a fixed pass order as production does.
        # Recent Pillow versions reverse passes for very tall inputs; letting
        # each strip pick its own order creates a visible change in the tail.
        reference = original.resize(
            (expected_size[0], original.height), Image.Resampling.LANCZOS
        ).resize(expected_size, Image.Resampling.LANCZOS)
        result = screenshot_capture._stitch_screenshot_segments(
            parts,
            total_height,
            css_width=css_width,
        )

        with Image.open(io.BytesIO(result)) as stitched:
            self.assertEqual(stitched.size, reference.size)
            difference = ImageChops.difference(stitched, reference)
            self.assertLessEqual(max(channel[1] for channel in difference.getextrema()), 1)

    def test_100000_css_pixel_page_allocates_only_the_safe_canvas(self):
        total_height = 100_000
        css_width = 390
        physical_width = css_width * DEVICE_SCALE_FACTOR
        physical_height = total_height * DEVICE_SCALE_FACTOR
        segment_css_height = 2000
        segment_png = _solid_png(
            physical_width,
            segment_css_height * DEVICE_SCALE_FACTOR,
            (47, 63, 159),
        )
        parts = [
            (top, segment_png)
            for top in range(0, total_height, segment_css_height)
        ]
        expected_size = screenshot_safety._safe_image_size(
            physical_width,
            physical_height,
        )
        original_new = Image.new
        allocations: list[tuple[int, int]] = []

        def tracked_new(mode, size, color=0):
            allocation = tuple(map(int, size))
            allocations.append(allocation)
            if allocation == (physical_width, physical_height):
                raise AssertionError("the full theoretical DPR2 canvas was allocated")
            return original_new(mode, size, color)

        with patch.object(screenshot_capture.Image, "new", side_effect=tracked_new):
            result = screenshot_capture._stitch_screenshot_segments(
                parts,
                total_height,
                css_width=css_width,
            )

        self.assertTrue(allocations)
        self.assertEqual(allocations[0], expected_size)
        with Image.open(io.BytesIO(result)) as stitched:
            self.assertEqual(stitched.size, expected_size)
            self.assertLessEqual(max(stitched.size), MAX_IMAGE_DIMENSION)
            self.assertLessEqual(stitched.width * stitched.height, MAX_IMAGE_PIXELS)


class _DprTwoPage:
    def __init__(self, width: int, viewport_height: int, *, dpr: int = 2):
        self.width = int(width)
        self.viewport_height = int(viewport_height)
        self.dpr = int(dpr)
        self.scroll_y = 0
        self.scroll_requests: list[int] = []
        self.screenshot_options: list[dict[str, object]] = []
        self.waits: list[int] = []
        self.evaluated: list[str] = []

    async def set_viewport_size(self, viewport):
        self.width = int(viewport["width"])
        self.viewport_height = int(viewport["height"])

    async def evaluate(self, script, *args):
        self.evaluated.append(script)
        if "window.scrollTo" in script:
            requested = int(args[0]) if args else 0
            self.scroll_y = max(0, requested)
            self.scroll_requests.append(self.scroll_y)
            return self.scroll_y
        return None

    async def wait_for_timeout(self, milliseconds):
        self.waits.append(int(milliseconds))

    async def screenshot(self, **kwargs):
        self.screenshot_options.append(dict(kwargs))
        return _row_pattern_png(
            self.width * self.dpr,
            self.viewport_height * self.dpr,
            self.scroll_y * self.dpr,
        )


class ScreenshotSegmentCaptureTests(unittest.IsolatedAsyncioTestCase):
    async def test_capture_stitches_all_native_dpr2_rows_and_crops_the_tail(self):
        width = 4
        viewport_height = 777
        page_height = 2005
        page = _DprTwoPage(width, viewport_height)

        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "capture.png"
            result = await screenshot_capture._capture_segmented_screenshot(
                page,
                str(output_path),
                width=width,
                viewport_height=viewport_height,
                page_height=page_height,
                timeout_ms=1000,
                hide_repeated_chrome_script="hide chrome",
                restore_repeated_chrome_script="restore chrome",
            )

            self.assertIsNone(result)
            self.assertTrue(output_path.is_file())
            with Image.open(output_path) as stitched:
                self.assertEqual(stitched.size, (width * 2, page_height * 2))
                self.assertEqual(
                    [stitched.getpixel((0, y)) for y in range(stitched.height)],
                    [_row_colour(y) for y in range(stitched.height)],
                )

        self.assertTrue(page.screenshot_options)
        self.assertTrue(
            all(
                options.get("scale") == "device"
                and options.get("type") == "png"
                for options in page.screenshot_options
            )
        )

    async def test_capture_refuses_dpr1_page_instead_of_upscaling(self):
        page = _DprTwoPage(8, 12, dpr=1)

        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "dpr1.png"
            with self.assertRaises(screenshot_capture.ScreenshotCaptureError):
                await screenshot_capture._capture_segmented_screenshot(
                    page,
                    str(output_path),
                    width=8,
                    viewport_height=12,
                    page_height=10,
                    timeout_ms=1000,
                    hide_repeated_chrome_script="hide chrome",
                    restore_repeated_chrome_script="restore chrome",
                )
            self.assertFalse(output_path.exists())


class BrowserContextDprTests(unittest.TestCase):
    def _context_probe(self):
        browser = SimpleNamespace(
            new_context=AsyncMock(side_effect=RuntimeError("stop after context setup")),
            close=AsyncMock(),
        )
        playwright = SimpleNamespace(
            devices={"iPhone 15": {"device_scale_factor": 3}},
            chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)),
            stop=AsyncMock(),
        )
        manager = SimpleNamespace(start=AsyncMock(return_value=playwright))
        return manager, browser

    def test_both_site_wrappers_override_device_profiles_with_the_same_dpr2(self):
        cases = (
            (
                keylol_browser,
                keylol_browser.capture_keylol_webpage_screenshot,
                "https://keylol.com/t1047774-1-1",
                keylol_browser.KeylolBrowserCaptureError,
            ),
            (
                tieba_browser,
                tieba_browser.capture_tieba_webpage_screenshot,
                "https://tieba.baidu.com/p/10937213244",
                tieba_browser.TiebaBrowserCaptureError,
            ),
        )
        for module, capture, url, error_type in cases:
            for css_width in (320, 390, 440):
                with self.subTest(site=module.__name__, css_width=css_width):
                    manager, browser = self._context_probe()
                    with patch.object(module, "async_playwright", return_value=manager):
                        with self.assertRaises(error_type):
                            asyncio.run(
                                capture(
                                    url,
                                    viewport_width=css_width,
                                    viewport_height=844,
                                    timeout_ms=5000,
                                )
                            )

                    kwargs = browser.new_context.await_args.kwargs
                    self.assertEqual(kwargs["device_scale_factor"], DEVICE_SCALE_FACTOR)
                    self.assertEqual(kwargs["viewport"]["width"], css_width)
                    self.assertEqual(kwargs["screen"]["width"], css_width)


if __name__ == "__main__":
    unittest.main()
