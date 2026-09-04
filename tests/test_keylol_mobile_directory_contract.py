"""Contract tests for the iPhone-page and directory capture mode.

These tests deliberately use static scripts and small HTML fixtures.  They do
not open a real browser or send requests to Keylol; the authenticated browser
smoke test belongs outside the unit-test suite.
"""

import inspect
import unittest
import asyncio
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from PIL import Image

import keylol_browser


class KeylolIphonePageContractTests(unittest.TestCase):
    def test_normalized_url_selects_discuz_mobile_page(self):
        normalized = keylol_browser.normalize_keylol_browser_url(
            "https://keylol.com/t1048330-1-1?foo=bar&mobile=yes"
        )
        self.assertIn("foo=bar", normalized)
        self.assertNotIn("mobile=", normalized)
        self.assertNotIn("mobile=no", normalized)

    def test_mobile_document_redirect_is_allowed_but_page_two_is_not(self):
        self.assertTrue(
            keylol_browser._is_allowed_thread_document(
                "https://keylol.com/t1048330-1-1?mobile=2", "1048330"
            )
        )
        self.assertFalse(
            keylol_browser._is_allowed_thread_document(
                "https://keylol.com/t1048330-2-1?mobile=2", "1048330"
            )
        )

    def test_capture_creates_an_isolated_iphone_context(self):
        source = inspect.getsource(keylol_browser.capture_keylol_webpage_screenshot)
        self.assertIn("new_context", source)
        self.assertIn('"iPhone 15"', source)
        self.assertIn("iPhone", source)
        self.assertIn('device["viewport"]', source)
        self.assertIn('device["service_workers"]', source)
        self.assertIn('device["accept_downloads"]', source)

    def test_transform_has_footer_metadata_and_no_top_banner(self):
        script = keylol_browser._TRANSFORM_SCRIPT
        self.assertNotIn("keylol-capture-banner", script)
        self.assertIn("keylol-capture-footer", script)
        self.assertIn("sourceUrl", script)
        # The footer must be appended after the copied post, not before it.
        self.assertGreater(script.index("content.append(footer)"), script.index("const footer"))


class KeylolDirectoryContractTests(unittest.TestCase):
    def test_directory_discovery_script_targets_real_toc_items(self):
        script = keylol_browser._TOC_DISCOVERY_SCRIPT
        for marker in ("#threadindex .tindex li", "threadindex", "viewthread", "cp", "tid", "viewpid"):
            with self.subTest(marker=marker):
                self.assertIn(marker, script)

    def test_directory_limit_defaults_to_12_and_is_configurable_from_1_to_20(self):
        self.assertEqual(keylol_browser.DEFAULT_BROWSER_TOC_SECTIONS, 12)
        self.assertEqual(keylol_browser.MAX_BROWSER_TOC_SECTIONS, 20)
        for value, expected in ((None, 12), (1, 1), (3, 3), (20, 20), (99, 20), (0, 1)):
            with self.subTest(value=value):
                self.assertEqual(keylol_browser._bounded_toc_sections(value), expected)

    def test_directory_request_contract_is_same_host_and_cp_scoped(self):
        # The browser route must contain these guards before it can issue the
        # three AJAX requests used to render separate directory screenshots.
        for value, expected in (
            (
                "https://keylol.com/forum.php?mod=viewthread&threadindex=yes&tid=1048330&viewpid=21577232&cp=1&inajax=1&ajaxtarget=pid21577232",
                True,
            ),
            (
                "https://evil.example/forum.php?mod=viewthread&threadindex=yes&tid=1048330&viewpid=21577232&cp=1&inajax=1&ajaxtarget=pid21577232",
                False,
            ),
            (
                "https://keylol.com/forum.php?mod=viewthread&threadindex=yes&tid=1048330&viewpid=21577232&cp=4&inajax=1&ajaxtarget=pid21577232",
                False,
            ),
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    keylol_browser._is_allowed_toc_request(
                        value, "1048330", "21577232", "1"
                    ),
                    expected,
                )


class KeylolMultiImageCompatibilityTests(unittest.TestCase):
    def test_capture_result_and_legacy_helper_both_remain_available(self):
        self.assertTrue(hasattr(keylol_browser, "KeylolBrowserCaptureResult"))
        self.assertTrue(hasattr(keylol_browser, "capture_keylol_screenshot"))
        self.assertTrue(hasattr(keylol_browser, "capture_keylol_webpage_screenshot"))

    def test_directory_mode_exposes_separate_output_paths(self):
        capture_source = inspect.getsource(keylol_browser.capture_keylol_webpage_screenshot)
        result_fields = keylol_browser.KeylolBrowserCaptureResult.__dataclass_fields__
        self.assertIn("split_toc_sections", capture_source)
        self.assertIn("max_toc_sections", capture_source)
        self.assertIn("image_paths", result_fields)
        self.assertIn("section_titles", result_fields)

    def test_keylol_command_sends_all_directory_captures_in_one_result_chain(self):
        # Importing the existing render-mode test installs the minimal
        # AstrBot stubs, so this remains a pure unit test.
        try:
            from test_main_render_mode import main
        except ModuleNotFoundError:
            from tests.test_main_render_mode import main

        class Event:
            message_str = "/keylol https://keylol.com/t1048330-1-1"

            def image_result(self, path):
                return ("image", path)

            def chain_result(self, chain):
                return ("chain", list(chain))

            def plain_result(self, text):
                return ("plain", text)

        async def collect(generator):
            return [item async for item in generator]

        plugin = main.KeylolScreenshotPlugin(object(), {"keylol_render_engine": "playwright"})
        with patch.object(
            plugin,
            "_render_screenshots",
            new=AsyncMock(return_value=["directory-1.png", "directory-2.png"]),
        ):
            result = asyncio.run(
                collect(plugin.keylol(Event(), "https://keylol.com/t1048330-1-1"))
            )
        self.assertEqual(
            len(result),
            1,
        )
        kind, chain = result[0]
        self.assertEqual(kind, "chain")
        self.assertEqual(
            [component.path for component in chain],
            ["directory-1.png", "directory-2.png"],
        )


class _FakeTilePage:
    """Minimal page double for the fixed-viewport tile stitcher."""

    def __init__(self, width=4, viewport_height=3):
        self.width = width
        self.viewport_height = viewport_height
        self.scroll_y = 0
        self.scrolls = []
        self.evaluated = []
        self.waits = []

    async def evaluate(self, script, *args):
        self.evaluated.append(script)
        if "window.scrollTo" in script:
            self.scroll_y = int(args[0])
            self.scrolls.append(self.scroll_y)
            return self.scroll_y
        return None

    async def wait_for_timeout(self, milliseconds):
        self.waits.append(milliseconds)

    async def screenshot(self, **_kwargs):
        # Each row encodes its absolute document y coordinate.  The second
        # tile overlaps the previous tile by one row and must be cropped.
        image = Image.new("RGB", (self.width, self.viewport_height))
        pixels = image.load()
        for row in range(self.viewport_height):
            value = min(255, (self.scroll_y + row + 1) * 10)
            for x in range(self.width):
                pixels[x, row] = (value, value, value)
        payload = BytesIO()
        image.save(payload, format="PNG")
        image.close()
        return payload.getvalue()


class KeylolTileStitchContractTests(unittest.TestCase):
    def test_tiles_scroll_hide_repeated_chrome_crop_overlap_and_restore(self):
        page = _FakeTilePage()
        with TemporaryDirectory() as directory:
            output = str(Path(directory) / "stitched.png")
            asyncio.run(
                keylol_browser._capture_mobile_page_tiles(
                    page,
                    output,
                    width=4,
                    viewport_height=3,
                    page_height=5,
                    timeout_ms=1000,
                )
            )
            with Image.open(output) as stitched:
                self.assertEqual(stitched.size, (4, 5))
                # Absolute rows 1..5; row 3 occurs once despite the tile
                # overlap, proving the final tile was cropped at its top.
                self.assertEqual(
                    [stitched.getpixel((0, row))[0] for row in range(5)],
                    [10, 20, 30, 40, 50],
                )
        self.assertEqual(page.scrolls, [0, 2])
        self.assertEqual(page.waits, [80, 80])
        self.assertTrue(any("data-keylol-capture-repeated-chrome" in script for script in page.evaluated))
        self.assertTrue(any("keylol-hide-repeated-chrome" in script for script in page.evaluated))
        self.assertTrue(any("removeAttribute" in script and "scrollTo(0, 0)" in script for script in page.evaluated))

    def test_tile_stitcher_restores_page_even_when_a_tile_fails(self):
        class FailingPage(_FakeTilePage):
            async def screenshot(self, **_kwargs):
                raise RuntimeError("synthetic screenshot failure")

        page = FailingPage()
        with TemporaryDirectory() as directory:
            with self.assertRaises(RuntimeError):
                asyncio.run(
                    keylol_browser._capture_mobile_page_tiles(
                        page,
                        str(Path(directory) / "failed.png"),
                        width=4,
                        viewport_height=3,
                        page_height=5,
                        timeout_ms=1000,
                    )
                )
        self.assertTrue(any("removeAttribute" in script for script in page.evaluated))


if __name__ == "__main__":
    unittest.main()
