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

    async def test_login_page_saves_failure_artifacts_then_auto_falls_back(self):
        channels = await self._available_local_channels()
        channel = channels[0]
        module = self.browser_module

        with tempfile.TemporaryDirectory() as directory:
            debug_directory = Path(directory) / "debug"
            plugin = main.KeylolScreenshotPlugin(
                object(),
                {
                    "tieba_render_engine": "auto",
                    "content_width": 440,
                    "browser_capture_timeout_ms": 15000,
                },
            )
            fallback = AsyncMock(return_value="fallback.png")
            plugin._render_tieba_html_screenshot = fallback
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
                image_path = await plugin._render_tieba_screenshot(_THREAD_URL, _COOKIE)

            self.assertEqual(image_path, "fallback.png")
            fallback.assert_awaited_once_with(_THREAD_URL, _COOKIE)
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
            self.assertNotIn("integration-bduss-secret", warning_text)
            self.assertNotIn("integration-stoken-secret", warning_text)


if __name__ == "__main__":
    unittest.main()
