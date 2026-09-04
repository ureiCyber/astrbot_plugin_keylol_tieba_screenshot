"""Regression coverage for the Tieba render-engine selection in ``main``.

The tests reuse the AstrBot stubs and package import helper from the existing
Keylol render-mode tests.  Browser calls are mocked so this file only verifies
the plugin's routing, metadata, and result/error handling.
"""

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch


try:
    from test_main_render_mode import main
except ModuleNotFoundError:  # pragma: no cover - package-style test runners
    from tests.test_main_render_mode import main


class TiebaMainRenderModeTests(unittest.TestCase):
    def _plugin(self, **values):
        config = {
            "tieba_render_engine": "auto",
            "browser_capture_timeout_ms": 120000,
            **values,
        }
        return main.KeylolScreenshotPlugin(object(), config)

    def test_tieba_render_engine_normalizes_supported_and_unknown_values(self):
        for value, expected in (
            ("auto", "auto"),
            (" PLAYWRIGHT ", "playwright"),
            ("HTML", "html"),
            ("unsupported", "auto"),
            (None, "auto"),
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    self._plugin(tieba_render_engine=value)._tieba_render_engine(),
                    expected,
                )

    def test_auto_mode_browser_success_does_not_call_html_renderer(self):
        plugin = self._plugin(tieba_render_engine=" AUTO ")
        browser = AsyncMock(return_value="browser.png")
        html = AsyncMock(return_value="html.png")
        with patch.object(plugin, "_render_tieba_browser_screenshot", browser), patch.object(
            plugin, "_render_tieba_html_screenshot", html
        ):
            result = asyncio.run(
                plugin._render_tieba_screenshot(
                    "https://tieba.baidu.com/p/10937213244", "BDUSS=secret"
                )
            )

        self.assertEqual(result, "browser.png")
        browser.assert_awaited_once_with(
            "https://tieba.baidu.com/p/10937213244", "BDUSS=secret"
        )
        html.assert_not_awaited()

    def test_auto_mode_browser_failure_falls_back_to_html_renderer(self):
        plugin = self._plugin(tieba_render_engine="auto")
        browser = AsyncMock(
            side_effect=main.TiebaBrowserCaptureError("unavailable")
        )
        html = AsyncMock(return_value="html.png")
        with patch.object(plugin, "_render_tieba_browser_screenshot", browser), patch.object(
            plugin, "_render_tieba_html_screenshot", html
        ):
            result = asyncio.run(
                plugin._render_tieba_screenshot(
                    "https://tieba.baidu.com/p/10937213244", "BDUSS=secret"
                )
            )

        self.assertEqual(result, "html.png")
        browser.assert_awaited_once_with(
            "https://tieba.baidu.com/p/10937213244", "BDUSS=secret"
        )
        html.assert_awaited_once_with(
            "https://tieba.baidu.com/p/10937213244", "BDUSS=secret"
        )

    def test_forced_playwright_converts_browser_failure_to_tieba_page_error(self):
        plugin = self._plugin(tieba_render_engine="playwright")
        browser = AsyncMock(
            side_effect=main.TiebaBrowserCaptureError("unavailable")
        )
        html = AsyncMock(return_value="html.png")
        with patch.object(plugin, "_render_tieba_browser_screenshot", browser), patch.object(
            plugin, "_render_tieba_html_screenshot", html
        ):
            with self.assertRaises(main.TiebaPageError) as caught:
                asyncio.run(
                    plugin._render_tieba_screenshot(
                        "https://tieba.baidu.com/p/10937213244", "BDUSS=secret"
                    )
                )

        self.assertEqual(str(caught.exception), "unavailable")
        browser.assert_awaited_once_with(
            "https://tieba.baidu.com/p/10937213244", "BDUSS=secret"
        )
        html.assert_not_awaited()

    def test_html_mode_skips_browser_renderer(self):
        plugin = self._plugin(tieba_render_engine="html")
        browser = AsyncMock(return_value="browser.png")
        html = AsyncMock(return_value="html.png")
        with patch.object(plugin, "_render_tieba_browser_screenshot", browser), patch.object(
            plugin, "_render_tieba_html_screenshot", html
        ):
            result = asyncio.run(
                plugin._render_tieba_screenshot(
                    "https://tieba.baidu.com/p/10937213244", "BDUSS=secret"
                )
            )

        self.assertEqual(result, "html.png")
        browser.assert_not_awaited()
        html.assert_awaited_once_with(
            "https://tieba.baidu.com/p/10937213244", "BDUSS=secret"
        )

    def test_browser_renderer_uses_native_page_metadata_and_passes_browser_options(self):
        plugin = self._plugin(
            content_width=400,
            browser_capture_timeout_ms=20000,
            proxy_url="http://127.0.0.1:7890",
            request_timeout_seconds=31,
        )
        browser_result = SimpleNamespace(
            status=main.TiebaBrowserCaptureStatus.PARTIAL,
            failed_image_count=2,
            image_path="partial.png",
        )
        capture = AsyncMock(return_value=browser_result)
        with patch.object(
            main, "capture_tieba_webpage_screenshot", capture
        ), patch.object(
            main, "fetch_tieba_article", new=AsyncMock()
        ) as fetch:
            result = asyncio.run(
                plugin._render_tieba_browser_screenshot(
                    "https://tieba.baidu.com/p/10937213244", "BDUSS=secret"
                )
            )

        self.assertEqual(result, "partial.png")
        fetch.assert_not_awaited()
        capture.assert_awaited_once_with(
            "https://tieba.baidu.com/p/10937213244",
            cookie="BDUSS=secret",
            viewport_width=400,
            viewport_height=866,
            timeout_ms=20000,
            proxy_url="http://127.0.0.1:7890",
        )

    def test_browser_renderer_rejects_result_with_empty_image_path(self):
        plugin = self._plugin()
        capture = AsyncMock(
            return_value=SimpleNamespace(
                status=main.TiebaBrowserCaptureStatus.PARTIAL,
                failed_image_count=1,
                image_path="   ",
            )
        )
        with patch.object(
            main, "capture_tieba_webpage_screenshot", capture
        ):
            with self.assertRaises(main.TiebaBrowserCaptureError):
                asyncio.run(
                    plugin._render_tieba_browser_screenshot(
                        "https://tieba.baidu.com/p/10937213244", "BDUSS=secret"
                    )
                )


if __name__ == "__main__":
    unittest.main()
