import asyncio
import unittest

from safe_media import SafeHtml, SafeImage
from keylol_embeds import normalize_steam_widget_url, render_embed


WIDGET = b"""<!doctype html><html><head><meta property='og:title' content='Test Game'></head>
<body><div class='game_description_snippet'>A short description</div>
<div class='discount_block'><div class='discount_pct'>-75%</div><div class='discount_final_price'>$2.49</div></div>
<div class='game_header_image_ctn'><img src='https://shared.cloudflare.steamstatic.com/store_item_assets/steam/apps/123/header.jpg?t=1'></div>
</body></html>"""
PNG = b"\x89PNG\r\n\x1a\n" + b"fixture"


class FakeDownloader:
    async def fetch_html(self, url, *, allowed_url, max_bytes, **_kwargs):
        self.html_url = url
        self.html_policy = allowed_url
        return SafeHtml(WIDGET, "text/html")

    async def fetch_image(self, url, *, allowed_url, max_bytes, **_kwargs):
        self.image_url = url
        self.image_policy = allowed_url
        return SafeImage(PNG, "image/png")


class KeylolEmbedTests(unittest.TestCase):
    def test_only_exact_steam_widget_urls_are_supported(self):
        self.assertEqual(
            normalize_steam_widget_url("https://store.steampowered.com/widget/123/?utm_source=keylol"),
            "https://store.steampowered.com/widget/123/",
        )
        for value in (
            "https://store.steampowered.com/app/123/",
            "https://store.steampowered.com/widget/123/?x=1",
            "https://evil.example/widget/123/",
            "https://user:pass@store.steampowered.com/widget/123/",
        ):
            with self.subTest(value=value):
                self.assertIsNone(normalize_steam_widget_url(value))

    def test_steam_widget_becomes_static_card_with_safe_image(self):
        downloader = FakeDownloader()
        result = asyncio.run(render_embed("https://store.steampowered.com/widget/123/", downloader))
        self.assertTrue(result.loaded)
        self.assertFalse(result.fallback)
        self.assertIn("Test Game", result.html)
        self.assertIn("data:image/png;base64,", result.html)
        self.assertNotIn("<script", result.html.lower())
        self.assertTrue(downloader.html_policy(downloader.html_url))
        self.assertTrue(downloader.image_policy(downloader.image_url))

    def test_unknown_and_failed_embeds_are_visible_fallbacks(self):
        unknown = asyncio.run(render_embed("https://example.org/frame", FakeDownloader()))
        self.assertIn("外部嵌入内容", unknown.html)
        self.assertNotIn("视频内容", unknown.html)

        class Failed(FakeDownloader):
            async def fetch_html(self, *args, **kwargs):
                return None

        failed = asyncio.run(render_embed("https://store.steampowered.com/widget/123/", Failed()))
        self.assertFalse(failed.loaded)
        self.assertTrue(failed.fallback)
        self.assertIn("Steam 商店内容", failed.html)


if __name__ == "__main__":
    unittest.main()
