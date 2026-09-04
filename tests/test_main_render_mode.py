import asyncio
import importlib
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch


def _install_astrbot_stubs() -> None:
    """Provide the small AstrBot surface needed to import ``main.py``."""

    if "astrbot.api" in sys.modules:
        return

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event = types.ModuleType("astrbot.api.event")
    message_components = types.ModuleType("astrbot.api.message_components")
    star = types.ModuleType("astrbot.api.star")

    class _Config(dict):
        pass

    class _Logger:
        def info(self, *_args, **_kwargs):
            pass

        def warning(self, *_args, **_kwargs):
            pass

        def exception(self, *_args, **_kwargs):
            pass

    class _EventMessageType:
        GROUP_MESSAGE = "group_message"

    class _Filter:
        EventMessageType = _EventMessageType

        @staticmethod
        def event_message_type(_message_type):
            return lambda function: function

        @staticmethod
        def command(_name):
            return lambda function: function

    class _Star:
        def __init__(self, _context):
            pass

    class _Image:
        type = "Image"

        def __init__(self, path):
            self.path = path

        @classmethod
        def fromFileSystem(cls, path):
            return cls(path)

    class _Plain:
        type = "Plain"

        def __init__(self, text):
            self.text = text

    api.AstrBotConfig = _Config
    api.logger = _Logger()
    event.AstrMessageEvent = object
    event.filter = _Filter()
    message_components.Image = _Image
    message_components.Plain = _Plain
    star.Context = object
    star.Star = _Star

    astrbot.api = api
    api.message_components = message_components
    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.event": event,
            "astrbot.api.message_components": message_components,
            "astrbot.api.star": star,
        }
    )


def _load_main_module():
    _install_astrbot_stubs()
    package_root = __import__("pathlib").Path(__file__).resolve().parents[1]
    package = types.ModuleType("astrbotplug")
    package.__path__ = [str(package_root)]
    package.__package__ = "astrbotplug"
    sys.modules.setdefault("astrbotplug", package)
    return importlib.import_module("astrbotplug.main")


main = _load_main_module()


class MainRenderModeTests(unittest.TestCase):
    def _plugin(self, **values):
        config = {
            "keylol_render_engine": "auto",
            "browser_capture_timeout_ms": 120000,
            **values,
        }
        return main.KeylolScreenshotPlugin(object(), config)

    def test_keylol_render_engine_normalizes_supported_and_unknown_values(self):
        for value, expected in (
            ("auto", "auto"),
            (" PLAYWRIGHT ", "playwright"),
            ("HTML", "html"),
            ("unsupported", "auto"),
            (None, "auto"),
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    self._plugin(keylol_render_engine=value)._keylol_render_engine(),
                    expected,
                )

    def test_browser_capture_timeout_is_clamped_and_invalid_values_use_default(self):
        for value, expected in (
            (1, 15000),
            (15000, 15000),
            (90000, 90000),
            (999999, 120000),
            ("not-a-number", 120000),
            (None, 120000),
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    self._plugin(browser_capture_timeout_ms=value)
                    ._browser_capture_timeout_ms(),
                    expected,
                )

    def test_auto_mode_browser_success_does_not_call_html_renderer(self):
        plugin = self._plugin(keylol_render_engine="AUTO")
        browser = AsyncMock(return_value="browser.png")
        html = AsyncMock(return_value="html.png")
        with patch.object(plugin, "_render_keylol_browser_screenshot", browser), patch.object(
            plugin, "_render_keylol_html_screenshot", html
        ):
            result = asyncio.run(plugin._render_screenshot("https://keylol.com/t1-1-1", "sid=x"))

        self.assertEqual(result, "browser.png")
        browser.assert_awaited_once_with("https://keylol.com/t1-1-1", "sid=x")
        html.assert_not_awaited()

    def test_auto_mode_browser_failure_falls_back_to_html_renderer(self):
        plugin = self._plugin(keylol_render_engine="auto")
        browser = AsyncMock(side_effect=main.KeylolBrowserCaptureError("unavailable"))
        html = AsyncMock(return_value="html.png")
        with patch.object(plugin, "_render_keylol_browser_screenshot", browser), patch.object(
            plugin, "_render_keylol_html_screenshot", html
        ):
            result = asyncio.run(plugin._render_screenshot("https://keylol.com/t1-1-1", "sid=x"))

        self.assertEqual(result, "html.png")
        browser.assert_awaited_once_with("https://keylol.com/t1-1-1", "sid=x")
        html.assert_awaited_once_with("https://keylol.com/t1-1-1", "sid=x")

    def test_forced_playwright_converts_browser_failure_to_keylol_page_error(self):
        plugin = self._plugin(keylol_render_engine="playwright")
        browser = AsyncMock(side_effect=main.KeylolBrowserCaptureError("unavailable"))
        html = AsyncMock(return_value="html.png")
        with patch.object(plugin, "_render_keylol_browser_screenshot", browser), patch.object(
            plugin, "_render_keylol_html_screenshot", html
        ):
            with self.assertRaises(main.KeylolPageError) as caught:
                asyncio.run(
                    plugin._render_screenshot("https://keylol.com/t1-1-1", "sid=x")
                )

        self.assertEqual(str(caught.exception), "unavailable")
        browser.assert_awaited_once_with("https://keylol.com/t1-1-1", "sid=x")
        html.assert_not_awaited()

    def test_html_mode_skips_browser_renderer(self):
        plugin = self._plugin(keylol_render_engine="html")
        browser = AsyncMock(return_value="browser.png")
        html = AsyncMock(return_value="html.png")
        with patch.object(plugin, "_render_keylol_browser_screenshot", browser), patch.object(
            plugin, "_render_keylol_html_screenshot", html
        ):
            result = asyncio.run(plugin._render_screenshot("https://keylol.com/t1-1-1", "sid=x"))

        self.assertEqual(result, "html.png")
        browser.assert_not_awaited()
        html.assert_awaited_once_with("https://keylol.com/t1-1-1", "sid=x")


if __name__ == "__main__":
    unittest.main()
