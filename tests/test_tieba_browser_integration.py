"""Offline integration checks for Tieba's native browser screenshot path.

These tests launch installed Edge/Chrome builds and fulfill the Tieba
thread document and a first-party JS bundle locally. No public request is used.
"""

from __future__ import annotations

import asyncio
from io import BytesIO
import importlib
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image

try:
    from playwright.async_api import Page, Route
except ImportError:  # pragma: no cover - browser integration is optional
    Page = Route = None

try:
    from test_main_render_mode import main
except ModuleNotFoundError:  # pragma: no cover - package-style test runners
    from tests.test_main_render_mode import main


_THREAD_URL = "https://tieba.baidu.com/p/10937213244"
_COOKIE = "BDUSS=integration-bduss-secret; STOKEN=integration-stoken-secret"
_CHANNELS = ("msedge", "chrome")
_BUNDLE_URL = "https://tb2.bdstatic.com/tb/mobile/main.js"
_DELAYED_BUNDLE = r"""
window.setTimeout(() => {
  const article = document.createElement("article");
  article.setAttribute("data-post-no", "1");
  article.innerHTML = '<section data-role="post-content"><p>延迟生成的主楼正文</p><div class="spacer"></div></section>';
  document.body.append(article);
}, 350);
"""

_DELAYED_SEMANTIC_POST = r"""<!doctype html>
<html><head>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Offline Tieba native renderer fixture</title>
  <style>
    html, body { margin: 0; width: 440px; font: 16px Arial, sans-serif; }
    article { background: #fafafa; color: #20242a; }
    [data-role="post-content"] { min-height: 1250px; padding: 18px; }
    .spacer { height: 1150px; background: linear-gradient(#fff, #f3f6f9); }
  </style>
</head><body>
  <script src="https://tb2.bdstatic.com/tb/mobile/main.js"></script>
</body></html>"""

_LOGIN_PAGE = r"""<!doctype html>
<html><head><title>请先登录_百度贴吧</title></head><body>
  <h1>请先登录百度贴吧</h1>
  <form action="https://passport.baidu.com/v2/?login">
    <label>账号 <input name="user"></label>
    <label>密码 <input type="password" name="password"></label>
  </form>
</body></html>"""


def _image_component(payload: bytes):
    return SimpleNamespace(type="Image", payload=payload)


class TiebaBrowserIntegrationTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.browser_module = importlib.import_module(
            main.capture_tieba_webpage_screenshot.__module__
        )

    async def _available_local_channels(self) -> list[str]:
        module = self.browser_module
        if module.async_playwright is None or Page is None or Route is None:
            self.skipTest("Playwright is not installed")

        playwright = await module.async_playwright().start()
        available = []
        try:
            # Probe the local Windows browsers in a stable Edge-then-Chrome
            # order; each available channel is then exercised by the capture.
            for channel in _CHANNELS:
                browser = None
                try:
                    browser = await playwright.chromium.launch(
                        headless=True, channel=channel
                    )
                except Exception:
                    continue
                else:
                    available.append(channel)
                finally:
                    if browser is not None:
                        await browser.close()
        finally:
            await playwright.stop()

        if not available:
            self.skipTest("Neither local Edge nor Chrome is available")
        return available

    def _fulfill_thread_document(self, body: str):
        """Replace only the approved thread's continue with an offline response."""
        self.assertIsNotNone(Route)
        requests = []

        async def continue_with_fixture(route, **kwargs):
            request = route.request
            if request.url == _THREAD_URL:
                requests.append(request.url)
                await route.fulfill(
                    status=200,
                    content_type="text/html; charset=utf-8",
                    body=body,
                )
                return
            if request.url == _BUNDLE_URL:
                requests.append(request.url)
                self.assertNotIn('cookie', kwargs.get('headers', {}))
                await route.fulfill(status=200, content_type='application/javascript', body=_DELAYED_BUNDLE)
                return
            await route.abort()

        return patch.object(Route, "continue_", new=continue_with_fixture), requests

    async def _capture_through_plugin(self, channel: str, plugin, *, html: str):
        module = self.browser_module
        original_capture = main.capture_tieba_webpage_screenshot
        results = []

        async def capture_local(*args, **kwargs):
            kwargs["browser_channel"] = channel
            result = await original_capture(*args, **kwargs)
            results.append(result)
            return result

        route_patch, requests = self._fulfill_thread_document(html)
        with (
            patch.object(main, "capture_tieba_webpage_screenshot", new=capture_local),
            route_patch,
        ):
            if html == _LOGIN_PAGE:
                image = await plugin._render_tieba_screenshot(_THREAD_URL, _COOKIE)
                return image, results, requests
            image = await plugin._render_tieba_browser_screenshot(_THREAD_URL, _COOKIE)
            return image, results, requests

    async def test_native_capture_reaches_send_boundary_at_880_pixels(self):
        channels = await self._available_local_channels()
        module = self.browser_module

        for channel in channels:
            with self.subTest(channel=channel), tempfile.TemporaryDirectory() as directory:
                debug_directory = Path(directory) / "debug"
                plugin = main.KeylolScreenshotPlugin(
                    object(),
                    {
                        "tieba_render_engine": "playwright",
                        "content_width": 440,
                        "browser_capture_timeout_ms": 15000,
                    },
                )
                wait_calls = []
                original_wait = Page.wait_for_function

                async def trace_ready_wait(page, expression, *args, **kwargs):
                    if expression == module.FIRST_POST_READY_SCRIPT:
                        wait_calls.append(expression)
                    return await original_wait(page, expression, *args, **kwargs)

                with patch.object(module, "DEBUG_DIRECTORY", debug_directory), patch.object(
                    Page, "wait_for_function", new=trace_ready_wait
                ), patch.object(
                    main.Comp.Image,
                    "fromBytes",
                    side_effect=_image_component,
                    create=True,
                ):
                    capture_path, results, requests = await self._capture_through_plugin(
                        channel, plugin, html=_DELAYED_SEMANTIC_POST
                    )
                    self.assertTrue(requests)
                    self.assertIn(_BUNDLE_URL, requests)
                    self.assertEqual(len(results), 1)
                    self.assertEqual(results[0].viewport_css_width, 440)
                    self.assertEqual(results[0].device_pixel_ratio, 2)
                    self.assertEqual(results[0].raw_png_width, 880)
                    self.assertTrue(
                        wait_calls,
                        "delayed semantic post must use the bounded readiness wait",
                    )
                    self.assertTrue(Path(capture_path).is_file())

                    chain = await plugin._prepare_image_chain([capture_path])

                self.assertEqual(len(chain), 1)
                with Image.open(BytesIO(chain[0].payload)) as image:
                    self.assertEqual(image.format, "JPEG")
                    self.assertEqual(image.width, 880)
                self.assertFalse(Path(capture_path).exists())
                self.assertFalse(plugin._owned_capture_paths)
                self.assertFalse(
                    debug_directory.exists(),
                    "a successful native capture must not create failure artifacts",
                )

    async def _local_html_render_adapter(
        self, browser, output_directory: Path, raw_dimensions: list[tuple[int, int, int]]
    ):
        """Render production HTML/options in a real local browser without network."""
        paths = []

        async def abort_request(route):
            await route.abort()

        async def render(document, _render_args, *, return_url, options):
            self.assertFalse(return_url)
            self.assertEqual(options["scale"], "device")
            self.assertEqual(options["device_scale_factor_level"], "ultra")
            width = int(options["viewport_width"])
            height = int(options["viewport_height"])
            dpr = {"normal": 1.0, "high": 1.3, "ultra": 1.8}[
                options["device_scale_factor_level"]
            ]
            page = await browser.new_page(
                viewport={"width": width, "height": height},
                device_scale_factor=dpr,
            )
            try:
                await page.route("**/*", abort_request)
                await page.set_content(
                    document, wait_until="load", timeout=options["timeout"]
                )
                self.assertEqual(await page.evaluate("window.innerWidth"), width)
                self.assertAlmostEqual(await page.evaluate("window.devicePixelRatio"), dpr)
                path = output_directory / f"tieba-html-{len(paths)}.png"
                await page.screenshot(
                    path=str(path),
                    type=options["type"],
                    full_page=options["full_page"],
                    animations=options["animations"],
                    caret=options["caret"],
                    scale=options["scale"],
                    timeout=options["timeout"],
                )
                with Image.open(path) as image:
                    raw_dimensions.append((width, image.width, image.height))
                paths.append(path)
                return str(path)
            finally:
                await page.close()

        return render, paths

    @staticmethod
    def _fixture_article(body_html: str):
        article_type = importlib.import_module(
            main.fetch_tieba_article.__module__
        ).Article
        return article_type(
            title="Offline Tieba API and HTML renderer fixture",
            author="fixture author",
            published_at="2026-09-24 12:00",
            source_url=_THREAD_URL,
            body_html=body_html,
            has_locked_resources=False,
            is_authenticated=True,
        )

    async def test_html_ultra_scale_keeps_390_and_440_css_widths_through_production_path(self):
        """Run fetch→real build_render_html→local DPR browser→safety encoder.

        AstrBot's hosted T2I endpoint is kept offline. Its options are passed to
        a local html_render adapter that applies the documented ultra=1.8
        setting in Chromium, blocks every network request, and exercises the
        actual plugin trimming and JPEG safety path.
        """
        channels = await self._available_local_channels()
        module = self.browser_module
        target_dimensions = {390: 702, 440: 792}

        for channel in channels:
            playwright = await module.async_playwright().start()
            browser = await playwright.chromium.launch(headless=True, channel=channel)
            try:
                for css_width, physical_width in target_dimensions.items():
                    with self.subTest(channel=channel, css_width=css_width), tempfile.TemporaryDirectory() as directory:
                        output_directory = Path(directory)
                        raw_dimensions = []
                        plugin = main.KeylolScreenshotPlugin(
                            object(),
                            {
                                "tieba_render_engine": "html",
                                "content_width": css_width,
                            },
                        )
                        article = self._fixture_article(
                            '<p>本地 API 主楼正文 fixture。</p>'
                            '<div style="height:180px;background:#dcecff">'
                            "产生可见正文底部以验证自适应裁剪。"
                            "</div>"
                        )
                        fetch = AsyncMock(return_value=article)
                        local_render, paths = await self._local_html_render_adapter(
                            browser, output_directory, raw_dimensions
                        )
                        plugin.html_render = local_render
                        with patch.object(main, "fetch_tieba_article", fetch), patch.object(
                            main,
                            "capture_tieba_webpage_screenshot",
                            new=AsyncMock(side_effect=AssertionError("HTML mode used Playwright")),
                        ), patch.object(
                            main.Comp.Image,
                            "fromBytes",
                            side_effect=_image_component,
                            create=True,
                        ):
                            image_path = await plugin._render_tieba_screenshot(
                                _THREAD_URL, _COOKIE
                            )
                            with Image.open(image_path) as trimmed:
                                trimmed_size = trimmed.size
                            chain = await plugin._prepare_image_chain([image_path])

                        fetch.assert_awaited_once_with(
                            _THREAD_URL,
                            cookie=_COOKIE,
                            request_timeout_seconds=25,
                            inline_images=True,
                        )
                        self.assertEqual(raw_dimensions[0][0:2], (css_width, physical_width))
                        self.assertLess(
                            trimmed_size[1], raw_dimensions[0][2],
                            "default adaptive_height should trim viewport-only whitespace",
                        )
                        self.assertEqual(trimmed_size[0], physical_width)
                        self.assertEqual(len(chain), 1)
                        with Image.open(BytesIO(chain[0].payload)) as image:
                            self.assertEqual(image.format, "JPEG")
                            self.assertEqual(image.width, physical_width)
                            self.assertLessEqual(len(chain[0].payload), 10 * 1024 * 1024)
                        self.assertEqual(len(paths), 1)
            finally:
                await browser.close()
                await playwright.stop()

    async def test_overlimit_html_output_is_scaled_only_by_send_safety(self):
        channels = await self._available_local_channels()
        channel = channels[0]
        module = self.browser_module

        with tempfile.TemporaryDirectory() as directory:
            output_directory = Path(directory)
            raw_dimensions = []
            plugin = main.KeylolScreenshotPlugin(
                object(),
                {"tieba_render_engine": "html", "content_width": 390},
            )
            article = self._fixture_article(
                '<div style="height:10000px;background:#eeeeee">'
                "真实本地 renderer 超长 HTML 安全缩放 fixture。"
                "</div>"
            )
            fetch = AsyncMock(return_value=article)
            playwright = await module.async_playwright().start()
            browser = await playwright.chromium.launch(headless=True, channel=channel)
            try:
                local_render, paths = await self._local_html_render_adapter(
                    browser, output_directory, raw_dimensions
                )
                plugin.html_render = local_render
                with patch.object(main, "fetch_tieba_article", fetch), patch.object(
                    main.Comp.Image,
                    "fromBytes",
                    side_effect=_image_component,
                    create=True,
                ):
                    image_path = await plugin._render_tieba_screenshot(
                        _THREAD_URL, _COOKIE
                    )
                    with Image.open(image_path) as rendered:
                        self.assertEqual(rendered.width, 702)
                        self.assertGreater(rendered.height, 16_384)
                    chain = await plugin._prepare_image_chain([image_path])
            finally:
                await browser.close()
                await playwright.stop()

            self.assertTrue(paths)
            self.assertEqual(raw_dimensions[0][0:2], (390, 702))
            self.assertGreater(raw_dimensions[0][2], 16_384)
            self.assertEqual(len(chain), 1)
            encoded = chain[0].payload
            self.assertLessEqual(len(encoded), 10 * 1024 * 1024)
            with Image.open(BytesIO(encoded)) as image:
                self.assertEqual(image.format, "JPEG")
                self.assertLess(image.width, 702)
                self.assertGreater(image.width, 390)
                self.assertLessEqual(max(image.size), 16_384)
                self.assertLessEqual(image.width * image.height, 20_000_000)
                self.assertTrue(
                    all(value == 1 for table in image.quantization.values() for value in table),
                    "the safety resize should retain quality 100 when that fits",
                )
            self.assertEqual(len(paths), 1)

    async def test_login_page_saves_failure_artifacts_and_explicit_playwright_reports_error(self):
        channels = await self._available_local_channels()
        channel = channels[0]
        module = self.browser_module

        with tempfile.TemporaryDirectory() as directory:
            debug_directory = Path(directory) / "debug"
            plugin = main.KeylolScreenshotPlugin(
                object(),
                {
                    "tieba_render_engine": "playwright",
                    "content_width": 440,
                    "browser_capture_timeout_ms": 15000,
                },
            )
            html_renderer = AsyncMock(return_value="html.png")
            plugin._render_tieba_html_screenshot = html_renderer
            errors = []
            original_capture = main.capture_tieba_webpage_screenshot

            async def capture_local(*args, **kwargs):
                kwargs["browser_channel"] = channel
                try:
                    return await original_capture(*args, **kwargs)
                except module.TiebaBrowserCaptureError as error:
                    errors.append(error)
                    raise

            route_patch, requests = self._fulfill_thread_document(_LOGIN_PAGE)
            with patch.object(module, "DEBUG_DIRECTORY", debug_directory), route_patch, patch.object(
                main, "capture_tieba_webpage_screenshot", new=capture_local
            ), patch.object(main.logger, "warning") as warning:
                with self.assertRaises(main.TiebaPageError) as caught:
                    await plugin._render_tieba_screenshot(_THREAD_URL, _COOKIE)

            self.assertEqual(str(caught.exception), "贴吧页面无法显示主楼。")
            html_renderer.assert_not_awaited()
            self.assertTrue(requests)
            self.assertEqual(len(errors), 1)
            error = errors[0]
            self.assertEqual(error.reason, "tieba_login_required")
            self.assertEqual(error.stage, "page_check")
            artifacts = error.diagnostics["debug_artifacts"]
            self.assertEqual(artifacts["status"], "ok")
            artifact_paths = [Path(artifacts["html_path"]), Path(artifacts["screenshot_path"])]
            self.assertTrue(all(path.is_file() for path in artifact_paths))
            for secret in ("integration-bduss-secret", "integration-stoken-secret"):
                self.assertNotIn(secret, repr(error.diagnostics))
                self.assertTrue(all(secret not in path.name for path in artifact_paths))
            saved_html = artifact_paths[0].read_text(encoding="utf-8")
            self.assertNotIn("integration-bduss-secret", saved_html)
            self.assertNotIn("integration-stoken-secret", saved_html)
            warning_text = "\n".join(str(call.args[0]) for call in warning.call_args_list)
            self.assertIn("tieba_login_required", warning_text)
            self.assertIn("engine=playwright，不回退", warning_text)
            self.assertNotIn("integration-bduss-secret", warning_text)
            self.assertNotIn("integration-stoken-secret", warning_text)


if __name__ == "__main__":
    unittest.main()
