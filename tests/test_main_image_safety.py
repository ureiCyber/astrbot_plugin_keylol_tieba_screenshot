"""Exercise the real final encoding boundary for every public send route."""

import asyncio
import importlib
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event as ThreadEvent
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image

try:
    from test_main_render_mode import main
except ModuleNotFoundError:
    from tests.test_main_render_mode import main


def write_png(path, size=(780, 120)):
    with Image.new("RGB", size, (55, 110, 170)) as image:
        image.save(path, format="PNG")
    return str(path)


def component(payload):
    return SimpleNamespace(type="Image", payload=payload)


class Event:
    def __init__(self, message=""):
        self.message_str = message
        self.message_obj = SimpleNamespace(group_id="fixture", message=[])
        self.stopped = False

    def chain_result(self, chain):
        return ("chain", list(chain))

    def plain_result(self, message):
        return ("plain", message)

    def stop_event(self):
        self.stopped = True


async def collect(generator):
    return [item async for item in generator]


class ImageChainSafetyTests(unittest.IsolatedAsyncioTestCase):
    def plugin(self, **config):
        return main.KeylolScreenshotPlugin(object(), {"tieba_cookie": "BDUSS=fixture", **config})

    def assert_jpeg(self, payload, size):
        with Image.open(BytesIO(payload)) as image:
            self.assertEqual(image.format, "JPEG")
            self.assertEqual(image.size, size)
            self.assertLessEqual(max(image.size), 16384)
            self.assertLessEqual(image.width * image.height, 20_000_000)
            self.assertTrue(all(v == 1 for t in image.quantization.values() for v in t))
        self.assertLessEqual(len(payload), 10 * 1024 * 1024)

    async def test_every_command_engine_normalizes_before_sending(self):
        for site in ("keylol", "tieba"):
            for engine in ("playwright", "html", "auto"):
                with self.subTest(site=site, engine=engine), TemporaryDirectory() as directory:
                    plugin = self.plugin(**{f"{site}_render_engine": engine})
                    size = (780 if engine == "playwright" else 390, 120)
                    path = write_png(Path(directory) / "render.png", size)
                    browser_name = ("_render_keylol_browser_screenshots" if site == "keylol"
                                    else "_render_tieba_browser_screenshot")
                    html_name = ("_render_keylol_html_screenshots" if site == "keylol"
                                 else "_render_tieba_html_screenshot")
                    rendered = [path] if site == "keylol" else path
                    error = main.KeylolBrowserCaptureError if site == "keylol" else main.TiebaBrowserCaptureError
                    browser = AsyncMock(return_value=rendered)
                    if engine == "auto":
                        browser.side_effect = error("fixture fallback")
                    with (
                        patch.object(plugin, browser_name, browser),
                        patch.object(plugin, html_name, AsyncMock(return_value=rendered)),
                        patch.object(main.Comp.Image, "fromBytes", side_effect=component, create=True),
                    ):
                        results = await collect(getattr(plugin, site)(Event(), "https://example.invalid/fixture"))
                    self.assertEqual(len(results), 1)
                    self.assertEqual(results[0][0], "chain")
                    self.assert_jpeg(results[0][1][0].payload, size)

    async def test_auto_detection_sends_both_sites_through_the_same_boundary(self):
        with TemporaryDirectory() as directory:
            paths = [write_png(Path(directory) / f"{index}.png", (640 + index * 140, 50))
                     for index in range(2)]
            plugin = self.plugin(max_links_per_message=3)
            event = Event("https://keylol.com/t123-1-1 https://tieba.baidu.com/p/123")
            with (
                patch.object(plugin, "_render_screenshots", AsyncMock(return_value=[paths[0]])),
                patch.object(plugin, "_render_tieba_screenshot", AsyncMock(return_value=paths[1])),
                patch.object(main.Comp.Image, "fromBytes", side_effect=component, create=True),
            ):
                results = await collect(plugin.detect_keylol_link(event))
            self.assertTrue(event.stopped)
            self.assertEqual(len(results), 1)
            self.assertEqual(len(results[0][1]), 2)
            for item, width in zip(results[0][1], (640, 780)):
                self.assert_jpeg(item.payload, (width, 50))

    async def test_toc_images_each_use_their_own_safe_dimensions_and_owned_pngs_are_removed(self):
        with TemporaryDirectory() as directory:
            plugin = self.plugin()
            sizes = [(640, 40), (780, 30_000), (880, 50)]
            paths = [write_png(Path(directory) / f"{i}.png", size) for i, size in enumerate(sizes)]
            plugin._owned_capture_paths.update(paths)
            with patch.object(main.Comp.Image, "fromBytes", side_effect=component, create=True):
                chain = await plugin._prepare_image_chain(paths)
            for item, size in zip(chain, ((640, 40), (425, 16384), (880, 50))):
                self.assert_jpeg(item.payload, size)
            self.assertTrue(all(not Path(path).exists() for path in paths))
            self.assertFalse(plugin._owned_capture_paths)

    async def test_browser_result_paths_are_registered_and_then_cleaned_by_the_real_send_boundary(self):
        for site in ("keylol", "tieba"):
            with self.subTest(site=site), TemporaryDirectory() as directory:
                plugin = self.plugin()
                count = 3 if site == "keylol" else 1
                paths = [write_png(Path(directory) / f"{i}.png") for i in range(count)]
                capture_result = SimpleNamespace(
                    status=(main.KeylolBrowserCaptureStatus.OK if site == "keylol"
                            else main.TiebaBrowserCaptureStatus.OK),
                    image_path=paths[0], image_paths=tuple(paths),
                )
                article = SimpleNamespace(title="fixture", author="", published_at="", has_locked_resources=False)
                with (
                    patch.object(main, f"capture_{site}_webpage_screenshot", AsyncMock(return_value=capture_result)),
                    patch.object(main, "fetch_article", AsyncMock(return_value=article)),
                    patch.object(main.Comp.Image, "fromBytes", side_effect=component, create=True),
                ):
                    if site == "keylol":
                        result_paths = await plugin._render_keylol_browser_screenshots("fixture", "")
                    else:
                        result_paths = [await plugin._render_tieba_browser_screenshot("fixture", "")]
                    self.assertEqual(plugin._owned_capture_paths, set(paths))
                    chain = await plugin._prepare_image_chain(result_paths)
                self.assertEqual(len(chain), count)
                self.assertTrue(all(not Path(path).exists() for path in paths))
                self.assertFalse(plugin._owned_capture_paths)

    async def test_send_boundary_logs_renderer_and_safety_dimensions(self):
        with TemporaryDirectory() as directory:
            plugin = self.plugin()
            playwright_path = write_png(Path(directory) / "playwright.png", (880, 100))
            html_path = write_png(Path(directory) / "html.png", (440, 100))
            plugin._owned_capture_paths.add(playwright_path)
            with patch.object(main.logger, "info") as info, patch.object(
                main.Comp.Image, "fromBytes", side_effect=component, create=True
            ):
                await plugin._prepare_image_chain([playwright_path, html_path])

            text = "\n".join(str(call.args[0]) for call in info.call_args_list)
            self.assertIn("source_renderer=playwright", text)
            self.assertIn("source_renderer=html_fallback", text)
            self.assertIn("source_width=880, source_height=100", text)
            self.assertIn("source_width=440, source_height=100", text)
            self.assertIn("final_width=880, final_height=100", text)
            self.assertIn("final_width=440, final_height=100", text)
            self.assertIn("safety_resize=False", text)

    async def test_one_invalid_toc_image_prevents_the_whole_site_chain_and_cleans_all_owned_files(self):
        with TemporaryDirectory() as directory:
            plugin = self.plugin()
            paths = [write_png(Path(directory) / f"{i}.png") for i in range(3)]
            Path(paths[1]).write_bytes(b"invalid image")
            plugin._owned_capture_paths.update(paths)
            with (
                patch.object(plugin, "_render_screenshots", AsyncMock(return_value=paths)),
                patch.object(main.Comp.Image, "fromBytes", side_effect=component, create=True),
            ):
                results = await collect(plugin.keylol(Event(), "https://keylol.com/t123-1-1"))
            self.assertEqual([kind for kind, _ in results], ["plain"])
            self.assertTrue(all(not Path(path).exists() for path in paths))
            self.assertFalse(plugin._owned_capture_paths)

    async def test_send_boundary_revalidates_normalizer_output_and_preserves_core_html_file(self):
        with TemporaryDirectory() as directory:
            path = write_png(Path(directory) / "astrbot-html.png")
            plugin = self.plugin()
            with (
                patch.object(main, "_normalize_for_qq", return_value=b"invalid encoder result"),
                patch.object(main.Comp.Image, "fromBytes", create=True) as create,
            ):
                with self.assertRaises(Exception):
                    await plugin._prepare_image_chain([path])
                create.assert_not_called()
            self.assertTrue(Path(path).exists())

    async def test_empty_chain_is_rejected(self):
        with self.assertRaises(ValueError):
            await self.plugin()._prepare_image_chain([])

    async def test_unload_cleans_only_owned_browser_files(self):
        with TemporaryDirectory() as directory:
            owned = write_png(Path(directory) / "owned.png")
            cached = write_png(Path(directory) / "cached.png")
            plugin = self.plugin()
            plugin._owned_capture_paths.add(owned)
            await plugin.terminate()
            self.assertFalse(Path(owned).exists())
            self.assertTrue(Path(cached).exists())

    async def test_cancellation_before_encoding_cleans_owned_png(self):
        with TemporaryDirectory() as directory:
            path = write_png(Path(directory) / "owned.png")
            plugin = self.plugin()
            plugin._owned_capture_paths.add(path)
            plugin._render_slots = asyncio.Semaphore(0)
            pending = asyncio.create_task(plugin._prepare_image_chain([path]))
            await asyncio.sleep(0)
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
            self.assertFalse(Path(path).exists())

    async def test_browser_cancellation_cleans_allocated_png_but_keeps_caller_owned_output(self):
        for site in ("keylol", "tieba"):
            module = importlib.import_module(getattr(main, f"capture_{site}_webpage_screenshot").__module__)
            url = "https://keylol.com/t123-1-1" if site == "keylol" else "https://tieba.baidu.com/p/123"
            for own_output in (True, False):
                with self.subTest(site=site, own_output=own_output), TemporaryDirectory() as directory:
                    page = SimpleNamespace(
                        main_frame=object(), url=url, route=AsyncMock(), goto=AsyncMock(),
                        close=AsyncMock(), set_default_timeout=lambda _: None,
                    )

                    async def evaluate(script, *_):
                        if script == getattr(module, "_TOC_DISCOVERY_SCRIPT", None):
                            return []
                        if script == module._TRANSFORM_SCRIPT:
                            return {"title": "fixture", "imageCount": 0}
                        if script == module._FINALIZE_IMAGES_SCRIPT:
                            return {"pageHeight": 100, "captureHeight": 100, "loaded": 0, "failed": 0}
                        return {}

                    page.evaluate = AsyncMock(side_effect=evaluate)
                    context = SimpleNamespace(
                        add_cookies=AsyncMock(),
                        new_page=AsyncMock(return_value=page),
                        close=AsyncMock(),
                    )
                    browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
                    playwright = SimpleNamespace(
                        chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)),
                        devices={}, stop=AsyncMock(),
                    )
                    starter = SimpleNamespace(start=AsyncMock(return_value=playwright))
                    original_mkstemp = module.tempfile.mkstemp
                    allocated = []

                    def allocate(**kwargs):
                        fd, path = original_mkstemp(dir=directory, **kwargs)
                        allocated.append(path)
                        return fd, path

                    caller_path = Path(directory) / "caller.png"
                    caller_path.write_bytes(b"existing caller output")
                    with (
                        patch.object(module, "async_playwright", return_value=starter),
                        patch.object(module.tempfile, "mkstemp", side_effect=allocate),
                        patch.object(module, "_capture_mobile_page_tiles", AsyncMock(side_effect=asyncio.CancelledError)),
                    ):
                        with self.assertRaises(asyncio.CancelledError):
                            capture_kwargs = {
                                "cookie": "BDUSS=fixture",
                            } if site == "tieba" else {}
                            await getattr(module, f"capture_{site}_webpage_screenshot")(
                                url, output_path=None if own_output else caller_path,
                                **capture_kwargs,
                            )
                    self.assertEqual(bool(allocated), own_output)
                    self.assertTrue(all(not Path(path).exists() for path in allocated))
                    self.assertEqual(caller_path.read_bytes(), b"existing caller output")

    async def test_cancellation_during_encoding_keeps_file_until_worker_finishes(self):
        with TemporaryDirectory() as directory:
            path = write_png(Path(directory) / "owned.png")
            plugin = self.plugin()
            plugin._owned_capture_paths.add(path)
            started, release = ThreadEvent(), ThreadEvent()
            normalize = main._normalize_for_qq

            def delayed_normalize(payload):
                started.set()
                if not release.wait(5):
                    raise TimeoutError("test worker was not released")
                return normalize(payload)

            with (
                patch.object(main, "_normalize_for_qq", side_effect=delayed_normalize),
                patch.object(main.Comp.Image, "fromBytes", side_effect=component, create=True),
            ):
                pending = asyncio.create_task(plugin._prepare_image_chain([path]))
                try:
                    self.assertTrue(await asyncio.to_thread(started.wait, 5))
                    pending.cancel()
                    await asyncio.sleep(0)
                    self.assertFalse(pending.done())
                    self.assertTrue(Path(path).exists())
                finally:
                    release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await pending
            self.assertFalse(Path(path).exists())
            self.assertFalse(plugin._owned_capture_paths)

    async def test_unload_waits_for_active_encoder_before_cleaning_its_input(self):
        with TemporaryDirectory() as directory:
            path = write_png(Path(directory) / "owned.png")
            plugin = self.plugin()
            plugin._owned_capture_paths.add(path)
            started, release = ThreadEvent(), ThreadEvent()
            normalize = main._normalize_for_qq

            def delayed_normalize(payload):
                started.set()
                if not release.wait(5):
                    raise TimeoutError("test worker was not released")
                return normalize(payload)

            with (
                patch.object(main, "_normalize_for_qq", side_effect=delayed_normalize),
                patch.object(main.Comp.Image, "fromBytes", side_effect=component, create=True),
            ):
                pending = asyncio.create_task(plugin._prepare_image_chain([path]))
                try:
                    self.assertTrue(await asyncio.to_thread(started.wait, 5))
                    unload = asyncio.create_task(plugin.terminate())
                    await asyncio.sleep(0)
                    self.assertFalse(unload.done())
                    self.assertTrue(Path(path).exists())
                finally:
                    release.set()
                self.assertEqual(len(await pending), 1)
                await unload
            self.assertFalse(Path(path).exists())
            self.assertFalse(plugin._encoding_tasks)


if __name__ == "__main__":
    unittest.main()
