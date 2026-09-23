"""Contracts for the controlled Baidu Tieba browser renderer.

Most tests use static checks and doubles; one behavior test evaluates the
transform script in an installed Chromium browser without network access.
"""

from __future__ import annotations

import asyncio
import inspect
import unittest
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image

import tieba_browser
import tieba_page


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

    def test_cookie_parser_ignores_unrelated_malformed_segments(self):
        parser = tieba_browser.parse_tieba_cookie_header
        pairs = parser(
            "BAIDUID=tracking; !!!=odd-name; unrelated-bare; =no-name; "
            "BDUSS=bduss-value; STOKEN=stoken-value; Secure; $Version=1"
        )
        self.assertEqual(
            pairs,
            [("BDUSS", "bduss-value"), ("STOKEN", "stoken-value")],
        )

    def test_cookie_parser_accepts_many_unrelated_browser_cookies(self):
        unrelated = "; ".join(
            f"BAIDU_FIELD_{index}=value-{index}" for index in range(20)
        )
        pairs = tieba_browser.parse_tieba_cookie_header(
            f"{unrelated}; BDUSS=bduss-value; STOKEN=stoken-value"
        )
        self.assertEqual(
            pairs,
            [("BDUSS", "bduss-value"), ("STOKEN", "stoken-value")],
        )

    def test_cookie_parser_rejects_nul_in_bduss(self):
        with self.assertRaises(tieba_browser.TiebaBrowserCookieError) as caught:
            tieba_browser.parse_tieba_cookie_header("STOKEN=y; BDUSS=bad\x00value")
        self.assertEqual(caught.exception.reason, "nul_value")
        self.assertEqual(caught.exception.segment_index, 1)

    def test_cookie_parser_reports_missing_bduss_separately(self):
        with self.assertRaises(tieba_browser.TiebaBrowserCookieError) as caught:
            tieba_browser.parse_tieba_cookie_header("STOKEN=stoken-value; BAIDUID=x")
        self.assertEqual(str(caught.exception), "贴吧 Cookie 中未找到 BDUSS。")
        self.assertEqual(caught.exception.stage, "cookie_parse")
        self.assertEqual(caught.exception.reason, "missing_bduss")
        self.assertFalse(caught.exception.bduss_found)
        self.assertTrue(caught.exception.stoken_found)

    def test_browser_and_api_parsers_extract_the_same_bduss(self):
        raw = "BAIDUID=tracking; malformed; BDUSS=shared-bduss; STOKEN=shared-stoken"
        browser_pairs = dict(tieba_browser.parse_tieba_cookie_header(raw))
        api_credentials = tieba_page.parse_tieba_cookie(raw)
        self.assertEqual(browser_pairs["BDUSS"], api_credentials.bduss)
        self.assertEqual(browser_pairs["STOKEN"], api_credentials.stoken)

    def test_capture_injects_only_bduss_and_stoken_into_tieba_context(self):
        page = SimpleNamespace(
            main_frame=object(),
            url="https://tieba.baidu.com/p/123",
            route=AsyncMock(),
            goto=AsyncMock(),
            close=AsyncMock(),
            set_default_timeout=lambda _timeout: None,
        )

        async def evaluate(script, *_args):
            if script == tieba_browser._TRANSFORM_SCRIPT:
                return {"title": "fixture", "imageCount": 0, "nodeCount": 0}
            if script == tieba_browser._SCROLL_SCRIPT:
                return {"pageHeight": 100, "tooMany": False, "tooTall": False}
            if script == tieba_browser._FINALIZE_IMAGES_SCRIPT:
                return {"pageHeight": 100, "captureHeight": 100, "loaded": 0, "failed": 0}
            return {}

        page.evaluate = evaluate
        context = SimpleNamespace(
            add_cookies=AsyncMock(),
            new_page=AsyncMock(return_value=page),
            close=AsyncMock(),
        )
        browser = SimpleNamespace(
            new_context=AsyncMock(return_value=context),
            close=AsyncMock(),
        )
        playwright = SimpleNamespace(
            chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)),
            devices={},
            stop=AsyncMock(),
        )
        starter = SimpleNamespace(start=AsyncMock(return_value=playwright))

        async def write_png(_page, output_path, **_kwargs):
            with Image.new("RGB", (780, 200), "white") as image:
                image.save(output_path, format="PNG")

        with TemporaryDirectory() as directory, patch.object(
            tieba_browser, "async_playwright", return_value=starter
        ), patch.object(
            tieba_browser, "_capture_mobile_page_tiles", new=AsyncMock(side_effect=write_png)
        ):
            result = asyncio.run(
                tieba_browser.capture_tieba_webpage_screenshot(
                    "https://tieba.baidu.com/p/123",
                    cookie="BDUSS=bduss-value; OTHER=other-value; malformed; STOKEN=stoken-value",
                    output_path=Path(directory) / "capture.png",
                )
            )

        installed = context.add_cookies.await_args.args[0]
        self.assertEqual({item["name"] for item in installed}, {"BDUSS", "STOKEN"})
        self.assertEqual({item["value"] for item in installed}, {"bduss-value", "stoken-value"})
        self.assertEqual({item["url"] for item in installed}, {"https://tieba.baidu.com/", "https://www.tieba.baidu.com/"})
        self.assertEqual((result.raw_png_width, result.raw_png_height), (780, 200))


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

    def test_legacy_emoticon_host_is_limited_to_the_trusted_image_path(self):
        accepts = tieba_browser._is_allowed_image_request
        self.assertTrue(
            accepts(
                "https://static.tieba.baidu.com/tb/editor/images/client/"
                "image_emoticon25.png"
            )
        )
        self.assertTrue(
            accepts(
                "https://tb2.bdstatic.com/tb/editor/images/client/"
                "image_emoticon25.png"
            )
        )
        for value in (
            "http://static.tieba.baidu.com/tb/editor/images/client/image_emoticon25.png",
            "https://static.tieba.baidu.com/other/image.png",
            "https://static.tieba.baidu.com/tb/editor/images/client/image_emoticon25.png?x=1",
            "http://example.com/image.png",
            "https://localhost/image.png",
            "https://192.168.1.10/image.png",
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
        self.screenshot_scales = []

    async def evaluate(self, script, *args):
        self.evaluated.append(script)
        if "window.scrollTo" in script:
            self.scroll_y = int(args[0])
            self.scrolls.append(self.scroll_y)
            return self.scroll_y
        return None

    async def wait_for_timeout(self, milliseconds):
        self.waits.append(milliseconds)

    async def screenshot(self, **kwargs):
        self.screenshot_scales.append(kwargs.get("scale"))
        image = Image.new("RGB", (self.width * 2, self.viewport_height * 2))
        pixels = image.load()
        for row in range(self.viewport_height * 2):
            value = min(255, (self.scroll_y + row // 2 + 1) * 10)
            for x in range(self.width * 2):
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
                self.assertEqual(stitched.size, (8, 10))
                self.assertEqual(
                    [stitched.getpixel((0, row))[0] for row in range(10)],
                    [10, 10, 20, 20, 30, 30, 40, 40, 50, 50],
                )
        self.assertEqual(page.scrolls, [0, 2])
        self.assertEqual(page.waits, [80, 80])
        self.assertEqual(page.screenshot_scales, ["device", "device"])
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

    def test_native_dpr_dimension_failure_is_classified_as_capture(self):
        page = _FakeTilePage(width=5)
        with TemporaryDirectory() as directory:
            with self.assertRaises(tieba_browser.TiebaBrowserCaptureError) as caught:
                asyncio.run(
                    self.capture_tiles(
                        page,
                        str(Path(directory) / "failed-dimensions.png"),
                        width=4,
                        viewport_height=3,
                        page_height=3,
                        timeout_ms=1000,
                    )
                )
        self.assertEqual(caught.exception.stage, "capture")
        self.assertEqual(caught.exception.reason, "dimensions")


class TiebaBrowserRuntimeBehaviorTests(unittest.TestCase):
    def test_transform_normalizes_only_the_legacy_emoticon_http_url(self):
        playwright_factory = tieba_browser.async_playwright
        if playwright_factory is None:
            self.skipTest("Playwright is not installed")

        async def evaluate_transform():
            playwright = await playwright_factory().start()
            browser = context = page = None
            try:
                for launch_options in (
                    {"headless": True, "channel": "chrome"},
                    {"headless": True, "channel": "msedge"},
                    {"headless": True},
                ):
                    try:
                        browser = await playwright.chromium.launch(**launch_options)
                        break
                    except Exception:
                        continue
                if browser is None:
                    raise unittest.SkipTest("No installed Chromium browser is available")

                context = await browser.new_context(
                    viewport={"width": 390, "height": 844},
                    is_mobile=True,
                    has_touch=True,
                )
                page = await context.new_page()
                await page.set_content(
                    '''<!doctype html><html><head></head><body>
                    <div class="l_post" data-field='{"content":{"post_no":1}}'>
                      <div class="d_post_content">
                        <img alt="表情" src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
                          data-src="http://static.tieba.baidu.com/tb/editor/images/client/image_emoticon25.png">
                        <img alt="公网 HTTP" src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
                          data-src="http://example.com/image.png">
                        <img alt="错误路径" src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
                          data-src="http://static.tieba.baidu.com/other/image.png">
                      </div>
                      <div class="post-tail-wrap"><span class="tail-info">1楼</span></div>
                    </div>
                    </body></html>'''
                )
                info = await page.evaluate(
                    tieba_browser._TRANSFORM_SCRIPT,
                    {
                        "sourceUrl": "https://tieba.baidu.com/p/123",
                        "suppliedTitle": "测试帖",
                        "suppliedAuthor": "",
                        "suppliedPublishedAt": "",
                    },
                )
                state = await page.evaluate(
                    """() => ({
                      candidates: [...document.querySelectorAll('img[data-tieba-candidates]')]
                        .map((image) => JSON.parse(image.dataset.tiebaCandidates)),
                      failures: [...document.querySelectorAll('.tieba-browser-image-failed')]
                        .map((node) => node.textContent)
                    })"""
                )
                return info, state
            finally:
                for resource in (page, context, browser):
                    if resource is not None:
                        try:
                            await resource.close()
                        except Exception:
                            pass
                await playwright.stop()

        info, state = asyncio.run(evaluate_transform())
        expected = (
            "https://static.tieba.baidu.com/tb/editor/images/client/"
            "image_emoticon25.png"
        )
        self.assertEqual(state["candidates"], [[expected]])
        self.assertEqual(len(state["failures"]), 2)
        self.assertEqual(info["imageCount"], 1)
        self.assertEqual(info["missingImageCount"], 2)


if __name__ == "__main__":
    unittest.main()
