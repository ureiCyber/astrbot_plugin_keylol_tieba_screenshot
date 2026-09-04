"""Regression coverage for public directory posts with unrelated locked assets."""

import asyncio
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import AsyncMock, patch


# Reuse the lightweight AstrBot stubs installed by the existing main-module
# tests.  This keeps the regression test independent of a full AstrBot install.
try:
    from test_main_render_mode import main
except ModuleNotFoundError:  # pragma: no cover - package-style test runners
    from tests.test_main_render_mode import main


class BrowserLockedPublicRegressionTests(TestCase):
    def test_locked_public_post_still_uses_browser_and_returns_all_sections(self):
        """A missing cookie must not force a public TOC post into one HTML image."""

        article = SimpleNamespace(
            title="目录帖子",
            author="作者",
            published_at="2026-08-31",
            has_locked_resources=True,
        )
        capture_result = SimpleNamespace(
            status=main.KeylolBrowserCaptureStatus.OK,
            failed_image_count=0,
            image_path="section-1.png",
            image_paths=("section-1.png", "section-2.png", "section-3.png"),
        )
        fetch = AsyncMock(return_value=article)
        capture = AsyncMock(return_value=capture_result)
        plugin = main.KeylolScreenshotPlugin(
            object(),
            {
                "keylol_render_engine": "auto",
                "split_toc_sections": True,
                "max_toc_sections": 12,
            },
        )

        with patch.object(main, "fetch_article", fetch), patch.object(
            main, "capture_keylol_webpage_screenshot", capture
        ):
            paths = asyncio.run(
                plugin._render_keylol_browser_screenshots(
                    "https://keylol.com/t1048330-1-1", ""
                )
            )

        self.assertEqual(
            paths,
            ["section-1.png", "section-2.png", "section-3.png"],
        )
        fetch.assert_awaited_once()
        capture.assert_awaited_once()
        self.assertEqual(capture.await_args.kwargs["cookie"], "")
        self.assertTrue(capture.await_args.kwargs["split_toc_sections"])


if __name__ == "__main__":
    import unittest

    unittest.main()
