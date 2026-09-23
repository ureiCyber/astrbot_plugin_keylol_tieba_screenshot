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
            side_effect=main.TiebaBrowserCaptureError(
                "贴吧 Cookie 中未找到 BDUSS。",
                stage="cookie_parse",
                reason="missing_bduss",
                cookie_present=True,
                bduss_found=False,
                stoken_found=True,
            )
        )
        html = AsyncMock(return_value="html.png")
        with patch.object(
            plugin, "_render_tieba_browser_screenshot", browser
        ), patch.object(plugin, "_render_tieba_html_screenshot", html), patch.object(
            main.logger, "warning"
        ) as warning, patch.object(main.logger, "info") as info:
            result = asyncio.run(
                plugin._render_tieba_screenshot(
                    "https://tieba.baidu.com/p/10937213244", "STOKEN=secret-stoken"
                )
            )

        self.assertEqual(result, "html.png")
        browser.assert_awaited_once_with(
            "https://tieba.baidu.com/p/10937213244", "STOKEN=secret-stoken"
        )
        html.assert_awaited_once_with(
            "https://tieba.baidu.com/p/10937213244", "STOKEN=secret-stoken"
        )
        warning_text = "\n".join(str(call.args[0]) for call in warning.call_args_list)
        self.assertIn("stage=cookie_parse", warning_text)
        self.assertIn("reason=missing_bduss", warning_text)
        self.assertIn("bduss_found=False", warning_text)
        self.assertIn("stoken_found=True", warning_text)
        self.assertIn("engine=auto，将回退兼容模式", warning_text)
        self.assertNotIn("secret-stoken", warning_text)
        self.assertTrue(
            any(
                "source_renderer=html_fallback" in str(call.args[0])
                for call in info.call_args_list
            )
        )

    def test_playwright_success_log_contains_css_dpr_and_raw_physical_dimensions(self):
        plugin = self._plugin(tieba_render_engine="playwright", content_width=440)
        result = SimpleNamespace(
            status=main.TiebaBrowserCaptureStatus.OK,
            failed_image_count=0,
            image_path="browser.png",
            source_url="https://tieba.baidu.com/p/10937213244",
            cookie_present=True,
            bduss_found=True,
            stoken_found=True,
            viewport_css_width=440,
            device_pixel_ratio=2,
            raw_png_width=880,
            raw_png_height=1234,
        )
        with patch.object(
            main, "capture_tieba_webpage_screenshot", AsyncMock(return_value=result)
        ), patch.object(main.logger, "info") as info:
            output = asyncio.run(
                plugin._render_tieba_browser_screenshot(
                    "https://tieba.baidu.com/p/10937213244", "BDUSS=secret"
                )
            )

        self.assertEqual(output, "browser.png")
        text = "\n".join(str(call.args[0]) for call in info.call_args_list)
        self.assertIn("engine=playwright", text)
        self.assertIn("final_url=https://tieba.baidu.com/p/10937213244", text)
        self.assertIn("viewport_css_width=440", text)
        self.assertIn("device_pixel_ratio=2", text)
        self.assertIn("raw_png_width=880", text)
        self.assertIn("raw_png_height=1234", text)
        self.assertNotIn("secret", text)

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
