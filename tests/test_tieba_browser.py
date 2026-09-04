"""Static contracts for the controlled Baidu Tieba browser renderer.

These tests intentionally do not launch a real browser or make network
requests.  The renderer is expected to keep the same safety and fixed
viewport guarantees as the Keylol renderer while using Tieba's ``/p/<tid>``
URLs and client image hosts.
"""

from __future__ import annotations

import asyncio
import inspect
import unittest
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image

import tieba_browser


def _pick(*names: str, required: bool = True):
    """Resolve a public/internal spelling used by the browser module."""

    for name in names:
        value = getattr(tieba_browser, name, None)
        if value is not None:
            return value
    if required:
        raise AssertionError(f"tieba_browser is missing one of: {', '.join(names)}")
    return None


def _cookie_pairs(value):
    """Convert the supported parser result shapes to name/value pairs."""

    if isinstance(value, dict):
        return list(value.items())
    if hasattr(value, "bduss") or hasattr(value, "stoken"):
        return [("BDUSS", value.bduss), ("STOKEN", value.stoken)]
    return list(value)


class TiebaBrowserUrlTests(unittest.TestCase):
    def test_p_url_is_normalized_to_https_and_discards_query(self):
        normalize = _pick("normalize_tieba_browser_url", "normalize_tieba_url")
        self.assertEqual(
            normalize("<http://www.tieba.baidu.com/p/10937213244?see_lz=1>"),
            "https://tieba.baidu.com/p/10937213244",
        )

    def test_url_rejects_wrong_host_credentials_ports_fragments_and_non_p_paths(self):
        normalize = _pick("normalize_tieba_browser_url", "normalize_tieba_url")
        error = _pick(
            "TiebaBrowserUrlError",
            "TiebaBrowserCaptureError",
            "TiebaPageError",
        )
        rejected = (
            "https://evil.example/p/10937213244",
            "https://user:pass@tieba.baidu.com/p/10937213244",
            "https://tieba.baidu.com:443/p/10937213244",
            "https://tieba.baidu.com/p/10937213244#post",
            "https://tieba.baidu.com/f?kw=test",
            "https://tieba.baidu.com/p/abc",
            "https://tieba.baidu.com/p/10937213244/2",
            "",
            "   ",
        )
        for value in rejected:
            with self.subTest(value=value):
                with self.assertRaises(error):
                    normalize(value)


class TiebaBrowserCookieTests(unittest.TestCase):
    def test_cookie_parser_keeps_only_bduss_and_stoken(self):
        parser = _pick(
            "parse_tieba_browser_cookie_header",
            "parse_tieba_cookie_header",
            "parse_tieba_browser_cookie",
            "parse_tieba_cookie",
        )
        pairs = _cookie_pairs(
            parser(
                "Cookie: BDUSS=bduss-value; STOKEN=stoken-value; "
                "BAIDUID=tracking; Path=/; Secure; HttpOnly"
            )
        )
        self.assertEqual(
            [(str(name).upper(), value) for name, value in pairs],
            [("BDUSS", "bduss-value"), ("STOKEN", "stoken-value")],
        )

    def test_cookie_parser_rejects_header_injection_newlines(self):
        parser = _pick(
            "parse_tieba_browser_cookie_header",
            "parse_tieba_cookie_header",
            "parse_tieba_browser_cookie",
            "parse_tieba_cookie",
        )
        error = _pick("TiebaBrowserCaptureError", "TiebaPageError")
        for value in ("BDUSS=ok\nInjected: yes", "STOKEN=ok\rInjected: yes"):
            with self.subTest(value=repr(value)):
                with self.assertRaises(error):
                    parser(value)


class TiebaBrowserRoutingContractTests(unittest.TestCase):
    def test_safe_url_and_thread_document_are_https_same_host_and_thread_scoped(self):
        safe_url = _pick(
            "_is_safe_https_url",
            "_safe_https_url",
            "_is_safe_tieba_https_url",
        )
        signature = inspect.signature(safe_url)
        allowed_hosts = {"tieba.baidu.com", "www.tieba.baidu.com"}

        def safe(value):
            if len(signature.parameters) >= 2:
                return safe_url(value, allowed_hosts)
            return safe_url(value)

        self.assertTrue(safe("https://tieba.baidu.com/p/10937213244"))
        for value in (
            "http://tieba.baidu.com/p/10937213244",
            "https://evil.example/p/10937213244",
            "https://user:pass@tieba.baidu.com/p/10937213244",
            "https://tieba.baidu.com:443/p/10937213244",
        ):
            with self.subTest(value=value):
                self.assertFalse(safe(value))

        document = _pick(
            "_is_allowed_thread_document",
            "_is_allowed_tieba_thread_document",
        )
        self.assertTrue(
            document("https://tieba.baidu.com/p/10937213244", "10937213244")
        )
        for value, thread_id in (
            ("https://tieba.baidu.com/p/10937213245", "10937213244"),
            ("https://evil.example/p/10937213244", "10937213244"),
            ("http://tieba.baidu.com/p/10937213244", "10937213244"),
        ):
            with self.subTest(value=value, thread_id=thread_id):
                self.assertFalse(document(value, thread_id))

    def test_image_or_static_resource_route_rejects_external_and_unsafe_hosts(self):
        route = _pick(
            "_is_allowed_image_request",
            "_is_allowed_tieba_image_request",
            "_is_allowed_static_request",
            "_is_allowed_tieba_static_request",
        )
        # The first two are image routes; the latter two are static-resource
        # routes.  Try the route's actual contract with a representative URL.
        params = inspect.signature(route).parameters
        if len(params) >= 2:
            allowed = {
                "tieba.baidu.com",
                "www.tieba.baidu.com",
                "tiebapic.baidu.com",
                "imgsrc.baidu.com",
                "tb2.bdstatic.com",
            }
            def accepts(value):
                return route(value, allowed)
        else:
            def accepts(value):
                return route(value)

        valid = (
            "https://tiebapic.baidu.com/forum/pic/item/example.jpg",
            "https://imgsrc.baidu.com/forum/pic/item/example.png",
            "https://tb2.bdstatic.com/tb/static-common/example.css",
        )
        # At least one representative endpoint must be accepted; this keeps
        # the test compatible with implementations that split image/static
        # routing into separate helpers.
        self.assertTrue(any(accepts(value) for value in valid))
        for value in (
            "http://tiebapic.baidu.com/forum/pic/item/example.jpg",
            "https://evil.example/forum/pic/item/example.jpg",
            "https://tiebapic.baidu.com/forum/pic/item/example.exe",
            "https://user:pass@tiebapic.baidu.com/forum/pic/item/example.jpg",
        ):
            with self.subTest(value=value):
                self.assertFalse(accepts(value))


class TiebaBrowserScriptContractTests(unittest.TestCase):
    def test_transform_script_selects_main_floor_hides_second_floor_and_handles_lazy_media(self):
        script = _pick("_TRANSFORM_SCRIPT", "_TIEBA_TRANSFORM_SCRIPT")
        for marker in ("post_no", "floor", "data-field", "l_post", "j_l_post"):
            with self.subTest(marker=marker):
                self.assertIn(marker, script)
        # Tieba's authoritative floor marker is nested in the data-field
        # payload (``content.post_no``); a generic first DOM node is not enough
        # because the first visible node can be an advertisement/reply.
        self.assertIn("content.post_no", script)
        self.assertRegex(script, r"(?i)(display\s*=\s*[\"']none|style\.display)")
        for marker in (
            "data-src",
            "data-original",
            "src",
            "lazy",
            "footer",
            "sourceUrl",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, script)
        self.assertTrue(
            "图片加载失败" in script or "图片地址不可用" in script,
            "transform script must expose a user-visible image failure hint",
        )

    def test_scroll_and_finalize_scripts_are_bounded_and_report_failures(self):
        scroll = _pick("_SCROLL_SCRIPT", "_TIEBA_SCROLL_SCRIPT")
        finalize = _pick("_FINALIZE_IMAGES_SCRIPT", "_TIEBA_FINALIZE_IMAGES_SCRIPT")
        for marker in (
            "maxImages",
            "maxHeight",
            "perImageTimeoutMs",
            "scrollIntoView",
            "tooMany",
            "tooTall",
            "attempt < 2",
            "failed",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, scroll)
        for marker in ("图片加载失败", "footer", "pageHeight"):
            with self.subTest(marker=marker):
                self.assertIn(marker, finalize)
        self.assertTrue(all("document.cookie" not in script.lower() for script in (scroll, finalize)))


class _FakeTilePage:
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


class TiebaBrowserTileContractTests(unittest.TestCase):
    def setUp(self):
        self.capture_tiles = _pick(
            "_capture_mobile_page_tiles",
            "_capture_tieba_mobile_page_tiles",
            "_capture_tieba_page_tiles",
        )

    def test_fixed_viewport_tiles_scroll_crop_overlap_and_restore(self):
        page = _FakeTilePage()
        with TemporaryDirectory() as directory:
            output = str(Path(directory) / "stitched.png")
            asyncio.run(
                self.capture_tiles(
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
                self.assertEqual(
                    [stitched.getpixel((0, row))[0] for row in range(5)],
                    [10, 20, 30, 40, 50],
                )
        self.assertEqual(page.scrolls, [0, 2])
        self.assertTrue(any("scrollTo(0, 0)" in script for script in page.evaluated))

    def test_screenshot_failure_still_restores_page_state(self):
        class FailingPage(_FakeTilePage):
            async def screenshot(self, **_kwargs):
                raise RuntimeError("synthetic screenshot failure")

        page = FailingPage()
        with TemporaryDirectory() as directory:
            with self.assertRaises(Exception):
                asyncio.run(
                    self.capture_tiles(
                        page,
                        str(Path(directory) / "failed.png"),
                        width=4,
                        viewport_height=3,
                        page_height=5,
                        timeout_ms=1000,
                    )
                )
        self.assertTrue(any("scrollTo(0, 0)" in script for script in page.evaluated))


if __name__ == "__main__":
    unittest.main()
