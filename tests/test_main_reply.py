"""Regression tests for quoting the message that triggered a screenshot."""

from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image as PILImage

try:
    from test_main_render_mode import main
except ModuleNotFoundError:  # pragma: no cover - package discovery fallback
    from tests.test_main_render_mode import main


class _Reply:
    type = "Reply"

    def __init__(self, id):
        self.id = id


class _ImageComponent(main.Comp.Image):
    def __init__(self, path):
        self.path = path


class _Event:
    def __init__(self, message="", message_id=..., inbound_reply_id=None):
        self.message_str = message
        message_fields = {
            "group_id": "group-1",
            "message": (
                [SimpleNamespace(type="Reply", id=inbound_reply_id)]
                if inbound_reply_id is not None
                else []
            ),
        }
        if message_id is not ...:
            message_fields["message_id"] = message_id
        self.message_obj = SimpleNamespace(**message_fields)
        self.stopped = False

    def chain_result(self, chain):
        return ("chain", list(chain))

    def plain_result(self, text):
        return ("plain", text)

    def stop_event(self):
        self.stopped = True

    def is_stopped(self):
        return self.stopped


def _image(path):
    return _ImageComponent(path)


async def _collect(generator):
    return [item async for item in generator]


class ScreenshotReplyTests(unittest.IsolatedAsyncioTestCase):
    def plugin(self, **config):
        return main.KeylolScreenshotPlugin(
            object(),
            {
                "auto_detect_enabled": True,
                "dedupe_seconds": 0,
                "tieba_cookie": "BDUSS=fixture",
                **config,
            },
        )

    async def test_keylol_quotes_trigger_message_and_keeps_every_real_directory_image(self):
        with TemporaryDirectory() as directory:
            paths = []
            for index in range(3):
                path = Path(directory) / f"directory-{index}.png"
                with PILImage.new("RGB", (780 + index * 10, 120), (55, 110, 170)) as image:
                    image.save(path, format="PNG")
                paths.append(str(path))

            event = _Event(message_id=7321, inbound_reply_id="incoming-reply-99")
            plugin = self.plugin()
            encoded_payloads = []

            def from_bytes(payload):
                encoded_payloads.append(payload)
                return _image(f"normalized-{len(encoded_payloads)}")

            with (
                patch.object(plugin, "_render_screenshots", AsyncMock(return_value=paths)),
                patch.object(main.Comp.Image, "fromBytes", side_effect=from_bytes, create=True),
                patch.object(main.Comp, "Reply", _Reply, create=True),
            ):
                results = await _collect(plugin.keylol(event, "https://keylol.com/t123-1-1"))

        self.assertEqual(len(results), 1)
        kind, chain = results[0]
        self.assertEqual(kind, "chain")
        self.assertEqual([component.type for component in chain], ["Reply", "Image", "Image", "Image"])
        self.assertEqual(chain[0].id, "7321")
        self.assertEqual(
            [component.path for component in chain[1:]],
            ["normalized-1", "normalized-2", "normalized-3"],
        )
        self.assertEqual(len(encoded_payloads), 3)
        with PILImage.open(BytesIO(encoded_payloads[0])) as encoded_image:
            self.assertEqual(encoded_image.format, "JPEG")
            self.assertEqual(encoded_image.size, (780, 120))

    async def test_auto_detection_replies_once_before_mixed_success_and_failure_content(self):
        event = _Event(
            "https://keylol.com/t123-1-1 https://tieba.baidu.com/p/456",
            message_id="  triggering-message-17  ",
            inbound_reply_id="unrelated-inbound-reply-5",
        )
        plugin = self.plugin(max_links_per_message=3)
        images = [_image("auto-1"), _image("auto-2")]

        with (
            patch.object(plugin, "_render_screenshots", AsyncMock(return_value=["toc-1.png", "toc-2.png"])),
            patch.object(plugin, "_prepare_image_chain", AsyncMock(return_value=images)),
            patch.object(
                plugin,
                "_render_tieba_screenshot",
                AsyncMock(side_effect=main.TiebaPageError("fixture failure")),
            ),
            patch.object(main.Comp, "Reply", _Reply, create=True),
        ):
            results = await _collect(plugin.detect_keylol_link(event))

        self.assertTrue(event.stopped)
        self.assertEqual(len(results), 1)
        kind, chain = results[0]
        self.assertEqual(kind, "chain")
        self.assertEqual([component.type for component in chain], ["Reply", "Image", "Image", "Plain"])
        self.assertEqual(chain[0].id, "triggering-message-17")
        self.assertEqual([component.path for component in chain[1:3]], ["auto-1", "auto-2"])
        self.assertIn("贴吧截图失败", chain[3].text)

    async def test_tieba_quotes_numeric_triggering_message_id(self):
        event = _Event(message_id=4082)
        plugin = self.plugin()
        image = _image("tieba.png")

        with (
            patch.object(plugin, "_render_tieba_screenshot", AsyncMock(return_value="tieba.png")),
            patch.object(plugin, "_prepare_image_chain", AsyncMock(return_value=[image])),
            patch.object(main.Comp, "Reply", _Reply, create=True),
        ):
            results = await _collect(plugin.tieba(event, "https://tieba.baidu.com/p/456"))

        self.assertEqual(len(results), 1)
        self.assertEqual([component.type for component in results[0][1]], ["Reply", "Image"])
        self.assertEqual(results[0][1][0].id, "4082")

    async def test_missing_none_and_blank_ids_keep_normal_image_chain(self):
        for message_id in (..., None, " \t "):
            with self.subTest(message_id=message_id):
                event = _Event(message_id=message_id)
                plugin = self.plugin()
                image = _image("keylol.png")
                with (
                    patch.object(plugin, "_render_screenshots", AsyncMock(return_value=["keylol.png"])),
                    patch.object(plugin, "_prepare_image_chain", AsyncMock(return_value=[image])),
                    patch.object(main.Comp, "Reply", _Reply, create=True),
                ):
                    results = await _collect(plugin.keylol(event, "https://keylol.com/t123-1-1"))

                self.assertEqual(len(results), 1)
                self.assertEqual(results[0][0], "chain")
                self.assertEqual([component.type for component in results[0][1]], ["Image"])

    async def test_auto_detection_error_only_response_does_not_add_reply(self):
        event = _Event("https://keylol.com/t123-1-1", message_id="error-trigger")
        plugin = self.plugin()
        with (
            patch.object(
                plugin,
                "_render_screenshots",
                AsyncMock(side_effect=main.KeylolPageError("fixture failure")),
            ),
            patch.object(
                main.Comp, "Reply", side_effect=_Reply, create=True
            ) as reply_factory,
        ):
            results = await _collect(plugin.detect_keylol_link(event))

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][0], "chain")
        self.assertEqual([component.type for component in results[0][1]], ["Plain"])
        self.assertIn("其乐截图失败", results[0][1][0].text)
        reply_factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
