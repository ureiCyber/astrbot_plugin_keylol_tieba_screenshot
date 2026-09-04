"""Regression tests for multi-image Keylol delivery.

The automatic URL listener stops event propagation.  AstrBot's scheduler checks
that stop flag after each value yielded by an async-generator handler, so a
handler which both calls ``stop_event`` and yields several results only gets its
first result delivered.  These tests keep that scheduler detail explicit while
checking that all directory images are carried by one result chain.
"""

import asyncio
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch


# Reuse the small AstrBot import stubs used by the existing main tests.  The
# production code also imports ``astrbot.api.message_components``; install the
# tiny surface needed by this contract before importing that test helper (the
# helper imports ``main`` as a module side effect).
if "astrbot.api" not in sys.modules:
    _astrbot = types.ModuleType("astrbot")
    _api = types.ModuleType("astrbot.api")
    _event = types.ModuleType("astrbot.api.event")
    _star = types.ModuleType("astrbot.api.star")

    class _Config(dict):
        pass

    class _Logger:
        def warning(self, *_args, **_kwargs):
            pass

        def exception(self, *_args, **_kwargs):
            pass

        def info(self, *_args, **_kwargs):
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

    _api.AstrBotConfig = _Config
    _api.logger = _Logger()
    _event.AstrMessageEvent = object
    _event.filter = _Filter()
    _star.Context = object
    _star.Star = _Star
    _astrbot.api = _api
    sys.modules.update(
        {
            "astrbot": _astrbot,
            "astrbot.api": _api,
            "astrbot.api.event": _event,
            "astrbot.api.star": _star,
        }
    )

if "astrbot.api.message_components" not in sys.modules:
    _message_components = types.ModuleType("astrbot.api.message_components")

    class _ProductionImage:
        type = "Image"

        @classmethod
        def fromFileSystem(cls, path):
            return _Image(path)

    class _ProductionPlain:
        type = "Plain"

        def __init__(self, text):
            self.text = text

    _message_components.Image = _ProductionImage
    _message_components.Plain = _ProductionPlain
    sys.modules["astrbot.api.message_components"] = _message_components

try:
    from test_main_render_mode import main
except ModuleNotFoundError:  # pragma: no cover - package discovery fallback
    from tests.test_main_render_mode import main


class _Event:
    message_str = "https://keylol.com/t1048330-1-1"

    def __init__(self):
        self.message_obj = type("Message", (), {"group_id": "group-1"})()
        self.stop_calls = 0
        self.stopped = False
        self.sent = []

    def stop_event(self):
        self.stop_calls += 1
        self.stopped = True

    def is_stopped(self):
        return self.stopped

    def image_result(self, path):
        return _ImageResult(path)

    def chain_result(self, chain):
        return ("chain", list(chain))

    def plain_result(self, text):
        return ("plain", text)

    async def send(self, message):
        self.sent.append(message)


class _Image:
    """Small image-component stand-in used by the message-chain contract."""

    type = "Image"

    def __init__(self, path):
        self.path = path


class _ImageResult:
    """Shape of ``event.image_result`` needed to build a chain in the plugin."""

    def __init__(self, path):
        self.chain = [_Image(path)]


async def _collect(async_generator):
    return [item async for item in async_generator]


class MultiImageDeliveryTests(unittest.TestCase):
    def _plugin(self):
        return main.KeylolScreenshotPlugin(
            object(),
            {
                "auto_detect_enabled": True,
                "dedupe_seconds": 0,
                "keylol_render_engine": "playwright",
            },
        )

    def test_auto_detection_yields_one_chain_with_all_three_images(self):
        """Automatic handling must not rely on later async-generator yields."""

        event = _Event()
        plugin = self._plugin()
        paths = ["directory-1.png", "directory-2.png", "directory-3.png"]

        with patch.object(
            plugin,
            "_render_screenshots",
            new=AsyncMock(return_value=paths),
        ):
            yielded = asyncio.run(_collect(plugin.detect_keylol_link(event)))

        self.assertEqual(event.stop_calls, 1)
        self.assertEqual(event.sent, [])
        self.assertEqual(len(yielded), 1)
        kind, chain = yielded[0]
        self.assertEqual(kind, "chain")
        self.assertEqual([component.path for component in chain], paths)
        self.assertEqual([component.type for component in chain], ["Image"] * 3)

    def test_scheduler_stop_event_consumes_only_first_yield(self):
        """Document AstrBot's scheduler behavior that caused the original bug."""

        event = _Event()

        async def handler():
            event.stop_event()
            for path in ("image-1.png", "image-2.png", "image-3.png"):
                yield ("image", path)

        async def simulate_scheduler():
            consumed = []
            async for result in handler():
                consumed.append(result)
                # This is the scheduler's post-downstream check.  In the real
                # pipeline RespondStage runs before this check.
                if event.is_stopped():
                    break
            return consumed

        consumed = asyncio.run(simulate_scheduler())
        self.assertEqual(consumed, [("image", "image-1.png")])

    def test_keylol_command_yields_all_three_images(self):
        """The explicit command remains a normal passive multi-result handler."""

        event = _Event()
        plugin = self._plugin()
        paths = ["directory-1.png", "directory-2.png", "directory-3.png"]

        with patch.object(
            plugin,
            "_render_screenshots",
            new=AsyncMock(return_value=paths),
        ):
            yielded = asyncio.run(
                _collect(plugin.keylol(event, "https://keylol.com/t1048330-1-1"))
            )

        self.assertEqual(len(yielded), 1)
        kind, chain = yielded[0]
        self.assertEqual(kind, "chain")
        self.assertEqual([component.path for component in chain], paths)
        self.assertEqual([component.type for component in chain], ["Image"] * 3)
        self.assertEqual(event.sent, [])


if __name__ == "__main__":
    unittest.main()
