"""Regression coverage for Tieba's API/HTML default and opt-in browser path."""

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image


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
            ("auto", "html"),
            (" PLAYWRIGHT ", "playwright"),
            ("HTML", "html"),
            ("unsupported", "html"),
            (None, "html"),
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    self._plugin(tieba_render_engine=value)._tieba_render_engine(),
                    expected,
                )

    def test_missing_engine_configuration_defaults_to_html(self):
        plugin = main.KeylolScreenshotPlugin(object(), {})
        self.assertEqual(plugin._tieba_render_engine(), "html")

    def test_webui_default_is_html_and_keeps_auto_as_a_legacy_option(self):
        schema = json.loads(
            (Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text(
                encoding="utf-8"
            )
        )
        setting = schema["tieba_render_engine"]
        self.assertEqual(setting["default"], "html")
        self.assertEqual(setting["options"], ["html", "playwright", "auto"])
        self.assertIn("auto：旧版兼容", setting["hint"])

    def test_auto_mode_routes_directly_to_html_without_calling_browser(self):
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

        self.assertEqual(result, "html.png")
        browser.assert_not_awaited()
        html.assert_awaited_once_with(
            "https://tieba.baidu.com/p/10937213244", "BDUSS=secret"
        )

    def test_auto_mode_legacy_value_uses_html_even_if_browser_would_fail(self):
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
            main.logger, "info"
        ) as info:
            result = asyncio.run(
                plugin._render_tieba_screenshot(
                    "https://tieba.baidu.com/p/10937213244", "STOKEN=secret-stoken"
                )
            )

        self.assertEqual(result, "html.png")
        browser.assert_not_awaited()
        html.assert_awaited_once_with(
            "https://tieba.baidu.com/p/10937213244", "STOKEN=secret-stoken"
        )
        self.assertTrue(info.call_args_list)

    def test_html_options_request_real_ultra_device_scale_and_keylol_stays_css(self):
        plugin = self._plugin()
        for width, height in ((390, 844), (440, 950)):
            with self.subTest(width=width):
                options = plugin._tieba_html_screenshot_options(width, height)
                self.assertEqual(options["scale"], "device")
                self.assertEqual(options["device_scale_factor_level"], "ultra")
                self.assertEqual(options["viewport_width"], width)
                self.assertEqual(options["viewport_height"], height)

        # The helper shared with Keylol keeps its existing CSS-pixel contract.
        self.assertEqual(plugin._mobile_screenshot_options(390, 844)["scale"], "css")

    def test_html_renderer_receives_api_cookie_and_tieba_dpr_options(self):
        plugin = self._plugin(content_width=390, adaptive_height=False)
        article = SimpleNamespace(title="fixture")
        fetch = AsyncMock(return_value=article)
        render = AsyncMock(return_value="html.png")
        with patch.object(main, "fetch_tieba_article", fetch), patch.object(
            main, "build_render_html", return_value="<html>fixture</html>"
        ), patch.object(plugin, "html_render", render, create=True):
            result = asyncio.run(
                plugin._render_tieba_html_screenshot(
                    "https://tieba.baidu.com/p/10937213244",
                    "BDUSS=api-bduss; STOKEN=optional-stoken",
                )
            )

        self.assertEqual(result, "html.png")
        fetch.assert_awaited_once_with(
            "https://tieba.baidu.com/p/10937213244",
            cookie="BDUSS=api-bduss; STOKEN=optional-stoken",
            request_timeout_seconds=25,
            inline_images=True,
        )
        options = render.await_args.kwargs["options"]
        self.assertEqual(options["scale"], "device")
        self.assertEqual(options["device_scale_factor_level"], "ultra")
        self.assertEqual(options["viewport_width"], 390)

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

    def test_explicit_playwright_success_uses_browser_and_skips_api_html(self):
        plugin = self._plugin(tieba_render_engine="playwright")
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

    def test_html_success_log_names_api_renderer_and_reports_actual_dpr(self):
        plugin = self._plugin(tieba_render_engine="html", content_width=390)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "tieba-html.png"
            with Image.new("RGB", (702, 1000), "white") as image:
                image.save(path, format="PNG")
            html = AsyncMock(return_value=str(path))
            with patch.object(plugin, "_render_tieba_html_screenshot", html), patch.object(
                main.logger, "info"
            ) as info:
                result = asyncio.run(
                    plugin._render_tieba_screenshot(
                        "https://tieba.baidu.com/p/10937213244", "BDUSS=secret"
                    )
                )

        self.assertEqual(result, str(path))
        log_text = "\n".join(str(call.args[0]) for call in info.call_args_list)
        self.assertIn("source_renderer=tieba_api_html", log_text)
        self.assertIn("render_scale=1.8", log_text)
        self.assertIn("renderer_dpr=1.8", log_text)
        self.assertNotIn("html_fallback", log_text)
        self.assertNotIn("native_dpr2", log_text)
        self.assertNotIn("secret", log_text)

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
