import asyncio
import unittest
from pathlib import Path
from urllib.parse import urlencode
from bs4 import BeautifulSoup
from unittest.mock import patch

from safe_media import SafeHtml, SafeImage
from keylol_embeds import (
    _steam_image_redirect_allowed,
    classify_embed,
    normalize_steam_widget_url,
    render_embed,
)


WIDGET = b"""<!doctype html><html><head><meta property='og:title' content='Test Game'></head>
<body><div class='game_description_snippet'>A short description</div>
<div class='discount_block'><div class='discount_pct'>-75%</div><div class='discount_final_price'>$2.49</div></div>
<div class='game_header_image_ctn'><img src='https://shared.cloudflare.steamstatic.com/store_item_assets/steam/apps/123/header.jpg?t=1'></div>
</body></html>"""
PNG = b"\x89PNG\r\n\x1a\n" + b"fixture"
UNSET = object()


class FakeDownloader:
    html_payload = WIDGET
    html_content_type = "text/html"
    html_response = UNSET
    image_response = UNSET
    html_diagnostics = {}
    image_diagnostics = {}

    async def fetch_html(self, url, *, allowed_url, max_bytes, **kwargs):
        self.html_url = url
        self.html_policy = allowed_url
        self.html_request_diagnostics = kwargs.get("diagnostics")
        if self.html_request_diagnostics is not None:
            self.html_request_diagnostics.update(self.html_diagnostics)
            self.html_request_diagnostics.setdefault("final_url", url)
        if self.html_response is not UNSET:
            return self.html_response
        return SafeHtml(self.html_payload, self.html_content_type)

    async def fetch_image(self, url, *, allowed_url, max_bytes, **kwargs):
        self.image_url = url
        self.image_policy = allowed_url
        self.image_request_diagnostics = kwargs.get("diagnostics")
        if self.image_request_diagnostics is not None:
            self.image_request_diagnostics.update(self.image_diagnostics)
            self.image_request_diagnostics.setdefault("final_url", url)
        if self.image_response is not UNSET:
            return self.image_response
        return SafeImage(PNG, "image/png")


class KeylolEmbedTests(unittest.TestCase):
    def steam_fixture(self, language):
        fixture_path = (
            Path(__file__).parent / "fixtures" / f"steam_widget_3575980_{language}.html"
        )
        return fixture_path.read_bytes()

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
        self.assertEqual(result.status, "success")
        self.assertTrue(result.loaded)
        self.assertFalse(result.fallback)
        self.assertIn("Test Game", result.html)
        self.assertIn("data:image/png;base64,", result.html)
        self.assertNotIn("<script", result.html.lower())
        self.assertTrue(downloader.html_policy(downloader.html_url))
        self.assertTrue(downloader.image_policy(downloader.image_url))

    def test_current_widget_heading_wins_over_unrelated_document_og_title(self):
        downloader = FakeDownloader()
        downloader.html_payload = self.steam_fixture("en").replace(
            b"<title>",
            b'<meta property="og:title" content="Unrelated document title"><title>',
            1,
        )

        result = asyncio.run(
            render_embed("https://store.steampowered.com/widget/3575980/", downloader)
        )

        self.assertEqual(result.status, "success")
        self.assertEqual(result.reason, "success")
        self.assertIn(
            "Fantasy Maiden Wars - DREAM OF THE STRAY DREAMER",
            result.diagnostics["fields"]["name"],
        )
        self.assertNotEqual(result.diagnostics["fields"]["name"], "Unrelated document title")
        self.assertNotIn("Unrelated document title", result.html)

    def test_legacy_og_title_requires_same_app_artwork(self):
        title_only = FakeDownloader()
        title_only.html_payload = b"""<html><head>
          <meta property="og:title" content="Unrelated generic title">
        </head><body></body></html>"""
        rejected = asyncio.run(
            render_embed("https://store.steampowered.com/widget/123/", title_only)
        )
        self.assertEqual(rejected.status, "fetch_fallback")
        self.assertEqual(rejected.reason, "parse_missing_title")
        self.assertNotIn("Unrelated generic title", rejected.html)

        # Keep the older widget shape working when it has a same-AppID header,
        # description, and pricing block alongside its Open Graph title.
        legacy = FakeDownloader()
        accepted = asyncio.run(
            render_embed("https://store.steampowered.com/widget/123/", legacy)
        )
        self.assertEqual(accepted.status, "success")
        self.assertEqual(accepted.reason, "success")
        self.assertEqual(accepted.diagnostics["fields"]["name"], "Test Game")
        self.assertTrue(legacy.image_policy(legacy.image_url))
        self.assertIn("A short description", accepted.html)
        self.assertIn("$2.49", accepted.html)

    def test_live_3575980_chinese_and_english_widgets_render_full_static_cards(self):
        expected_description = (
            "Command a legion of more than 70 beloved Touhou Project heroines on an "
            "expansive quest to uncover the cause of mysterious incidents blossoming "
            "throughout Gensokyo. Deploy units on grid-based maps to battle monsters "
            "with flashy Spell Cards, and conquer each stage with strategic mastery."
        )
        for language, host, prefix in (
            ("zh", "shared.st.dl.eccdnx.com", "购买"),
            ("en", "shared.cdn.queniuqe.com", "Buy"),
        ):
            with self.subTest(language=language):
                downloader = FakeDownloader()
                downloader.html_payload = self.steam_fixture(language)
                downloader.html_diagnostics = {
                    "http_status": 200,
                    "final_url": "https://store.steampowered.com/widget/3575980/",
                    "content_type": "text/html",
                    "redirect_count": 0,
                    "body_length": len(downloader.html_payload),
                }
                downloader.image_diagnostics = {
                    "http_status": 200,
                    "content_type": "image/png",
                    "redirect_count": 0,
                    "body_length": len(PNG),
                }
                result = asyncio.run(
                    render_embed("https://store.steampowered.com/widget/3575980/", downloader)
                )

                self.assertEqual(result.status, "success")
                self.assertEqual(result.reason, "success")
                self.assertTrue(result.loaded)
                self.assertFalse(result.fallback)
                self.assertNotIn("静态截图无法完整加载", result.html)
                self.assertNotIn(f"{prefix} Fantasy Maiden Wars", result.html)
                self.assertIn("Fantasy Maiden Wars - DREAM OF THE STRAY DREAMER", result.html)
                self.assertIn(expected_description, result.html)
                self.assertIn("-30%", result.html)
                self.assertIn("¥ 128.00", result.html)
                self.assertIn("¥ 89.60", result.html)
                self.assertIn(">STEAM</span>", result.html)
                self.assertIn("在 Steam 上购买", result.html)
                self.assertIn("data:image/png;base64,", result.html)
                self.assertNotIn("<script", result.html.lower())
                self.assertTrue(downloader.image_policy(downloader.image_url))
                self.assertIn(host, downloader.image_url)

                diagnostics = result.diagnostics
                self.assertEqual(diagnostics["reason"], "success")
                self.assertEqual(diagnostics["html_length"], len(downloader.html_payload))
                self.assertEqual(
                    diagnostics["request"],
                    {
                        "http_status": 200,
                        "final_url": downloader.html_url,
                        "content_type": "text/html",
                        "redirect_count": 0,
                        "body_length": len(downloader.html_payload),
                    },
                )
                self.assertEqual(
                    diagnostics["image_request"]["content_type"], "image/png"
                )
                self.assertEqual(diagnostics["image_request"]["redirect_count"], 0)
                self.assertEqual(
                    diagnostics["image_request"]["final_url"], downloader.image_url
                )
                self.assertIn("Fantasy Maiden Wars", diagnostics["title"])
                self.assertTrue(
                    any(node.get("id") == "widget" for node in diagnostics["root_nodes"])
                )
                fields = diagnostics["fields"]
                self.assertIn("Fantasy Maiden Wars - DREAM OF THE STRAY DREAMER", fields["name"])
                self.assertEqual(fields["description"], expected_description)
                self.assertEqual(fields["price"], "¥ 89.60")
                self.assertEqual(fields["original_price"], "¥ 128.00")
                self.assertEqual(fields["discount"], "-30%")
                self.assertIn("capsule_184x69.jpg", fields["image_url"])
                self.assertEqual(fields["app_id"], "3575980")

    def test_steam_image_allowlist_accepts_only_named_same_app_artwork(self):
        allowed = (
            "https://shared.cdn.queniuqe.com/store_item_assets/steam/apps/3575980/"
            "79f7ec4b9b4b3153dfd1d41a7fabef01d1ba4c5c/capsule_184x69.jpg?t=1761286177",
            "https://shared.st.dl.eccdnx.com/steam/apps/3575980/capsule_231x87.jpg",
            "https://shared.cloudflare.steamstatic.com/steam/apps/3575980/header.jpg",
        )
        denied = (
            "http://shared.cdn.queniuqe.com/steam/apps/3575980/header.jpg",
            "https://shared.cdn.queniuqe.com/steam/apps/4813850/header.jpg",
            "https://shared.cdn.queniuqe.com/steam/apps/3575980/other.jpg",
            "https://shared.cdn.queniuqe.com/steam/apps/3575980/capsule_460x215.jpg",
            "https://shared.cdn.queniuqe.com/steam/apps/3575980/"
            "79f7ec4b9b4b3153dfd1d41a7fabef01d1ba4c5/capsule_184x69.jpg",
            "https://shared.cdn.queniuqe.com/steam/apps/3575980/"
            "79f7ec4b9b4b3153dfd1d41a7fabef01d1ba4c5g/capsule_184x69.jpg",
            "https://shared.cdn.queniuqe.com/steam/apps/3575980/"
            "%37%39f7ec4b9b4b3153dfd1d41a7fabef01d1ba4c5c/capsule_184x69.jpg",
            "https://shared.cdn.queniuqe.com/steam/apps/3575980/../3575980/header.jpg",
            "https://shared.cdn.queniuqe.com.evil.example/steam/apps/3575980/header.jpg",
            "https://user@shared.cdn.queniuqe.com/steam/apps/3575980/header.jpg",
            "https://shared.cdn.queniuqe.com:443/steam/apps/3575980/header.jpg",
            "https://shared.cdn.queniuqe.com/steam/apps/3575980/header.jpg?redirect=https://evil.example/",
        )
        for url in allowed:
            with self.subTest(allowed=url):
                self.assertTrue(_steam_image_redirect_allowed(url, "3575980"))
        for url in denied:
            with self.subTest(denied=url):
                self.assertFalse(_steam_image_redirect_allowed(url, "3575980"))

    def test_missing_secondary_fields_keeps_recognized_product_title(self):
        downloader = FakeDownloader()
        downloader.html_payload = b"""<html><body><div id="widget">
          <div id="header" class="header_container"><h1 class="main_text">
            <a href="https://store.steampowered.com/app/3575980/Game/">Buy A Recognized Game</a>
          </h1></div>
        </div></body></html>"""

        result = asyncio.run(
            render_embed("https://store.steampowered.com/widget/3575980/", downloader)
        )

        self.assertEqual(result.status, "success")
        self.assertEqual(result.reason, "image_url_unrecognized")
        self.assertIn("A Recognized Game", result.html)
        self.assertIn(">STEAM</span>", result.html)
        self.assertNotIn("Steam 商店内容（静态截图无法完整加载）", result.html)
        self.assertFalse(hasattr(downloader, "image_url"))

    def test_image_download_failure_preserves_product_text(self):
        downloader = FakeDownloader()
        downloader.html_payload = self.steam_fixture("en")
        downloader.image_response = None

        result = asyncio.run(
            render_embed("https://store.steampowered.com/widget/3575980/", downloader)
        )

        self.assertEqual(result.status, "success")
        self.assertEqual(result.reason, "image_fetch_failed")
        self.assertIn("Fantasy Maiden Wars - DREAM OF THE STRAY DREAMER", result.html)
        self.assertIn("¥ 89.60", result.html)
        self.assertIn("Command a legion of more than 70", result.html)
        self.assertNotIn("Steam 商店内容（静态截图无法完整加载）", result.html)
        self.assertNotIn("data:image/png;base64,", result.html)

    def test_unrecognized_image_url_keeps_product_text_without_fetching_it(self):
        downloader = FakeDownloader()
        downloader.html_payload = self.steam_fixture("en").replace(
            b"https://shared.cdn.queniuqe.com/store_item_assets/",
            b"https://untrusted.example/store_item_assets/",
        )

        result = asyncio.run(
            render_embed("https://store.steampowered.com/widget/3575980/", downloader)
        )

        self.assertEqual(result.status, "success")
        self.assertEqual(result.reason, "image_url_unrecognized")
        self.assertIn("Fantasy Maiden Wars - DREAM OF THE STRAY DREAMER", result.html)
        self.assertIn("¥ 89.60", result.html)
        self.assertNotIn("Steam 商店内容（静态截图无法完整加载）", result.html)
        self.assertFalse(hasattr(downloader, "image_url"))

    def test_steam_provider_error_is_rebuilt_from_confirmed_error_dom(self):
        fixture_path = Path(__file__).parent / "fixtures" / "steam_widget_provider_error.html"
        downloader = FakeDownloader()
        downloader.html_payload = fixture_path.read_bytes()

        result = asyncio.run(
            render_embed("https://store.steampowered.com/widget/4813850/", downloader)
        )

        self.assertEqual(result.status, "provider_error")
        self.assertEqual(result.reason, "provider_error")
        self.assertTrue(result.loaded)
        self.assertFalse(result.fallback)
        self.assertIn("错误", result.html)
        self.assertIn("无法读取这件物品的信息。", result.html)
        self.assertNotIn("静态截图无法完整加载", result.html)
        self.assertNotRegex(result.html, r"#\d+")
        self.assertTrue(downloader.html_policy(downloader.html_url))

        rendered = BeautifulSoup(result.html, "html.parser")
        self.assertIsNone(rendered.find(["script", "iframe", "object", "embed"]))
        for node in rendered.find_all(True):
            self.assertFalse(any(attribute.lower().startswith("on") for attribute in node.attrs))

    def test_steam_provider_error_text_is_plain_text_and_escaped(self):
        downloader = FakeDownloader()
        downloader.html_payload = """<html><body><div id="widget">
          <div class="header_container"><h1 class="main_text"><a>错误</a></h1></div>
          <div class="desc">无法读取这件物品的信息。 <img src=x onerror="alert(1)">
            <script>alert(2)</script></div>
        </div></body></html>""".encode("utf-8")

        result = asyncio.run(
            render_embed("https://store.steampowered.com/widget/123/", downloader)
        )

        self.assertEqual(result.status, "provider_error")
        self.assertEqual(result.reason, "provider_error")
        self.assertIn('data-keylol-embed-status="provider_error"', result.html)
        self.assertNotIn("<script", result.html.lower())
        self.assertNotIn("onerror", result.html.lower())
        rendered = BeautifulSoup(result.html, "html.parser")
        self.assertIsNone(rendered.find(["script", "iframe", "object", "embed", "img"]))

    def test_steam_product_without_artwork_keeps_legacy_partial_metrics(self):
        downloader = FakeDownloader()
        downloader.html_payload = b"<html><body><div class='game_name'>Title only</div></body></html>"

        result = asyncio.run(
            render_embed("https://store.steampowered.com/widget/123/", downloader)
        )

        self.assertEqual(result.status, "success")
        self.assertTrue(result.partial)
        self.assertFalse(result.loaded)
        self.assertTrue(result.fallback)
        self.assertIn("部分商店内容无法加载", result.html)

    def test_steam_empty_html_is_fetch_fallback(self):
        downloader = FakeDownloader()
        downloader.html_payload = b"<html><body></body></html>"

        result = asyncio.run(
            render_embed("https://store.steampowered.com/widget/123/", downloader)
        )

        self.assertEqual(result.status, "fetch_fallback")
        self.assertEqual(result.reason, "parse_missing_title")
        self.assertIn(
            "Steam 商店内容（静态截图无法完整加载）",
            BeautifulSoup(result.html, "html.parser").get_text("", strip=True),
        )

    def test_steam_rejected_redirect_and_untrusted_final_url_fall_back(self):
        class RedirectAttempt(FakeDownloader):
            async def fetch_html(self, url, *, allowed_url, max_bytes, **_kwargs):
                self.html_url = url
                self.html_policy = allowed_url
                self.redirect_allowed = allowed_url("https://127.0.0.1/private")
                diagnostics = _kwargs.get("diagnostics")
                if diagnostics is not None and not self.redirect_allowed:
                    diagnostics["reason"] = "redirect_rejected"
                return None if not self.redirect_allowed else SafeHtml(WIDGET, "text/html")

        rejected = RedirectAttempt()
        rejected_result = asyncio.run(
            render_embed("https://store.steampowered.com/widget/123/", rejected)
        )
        self.assertFalse(rejected.redirect_allowed)
        self.assertEqual(rejected_result.status, "fetch_fallback")
        self.assertEqual(rejected_result.reason, "redirect_rejected")

        class UntrustedFinalUrl(FakeDownloader):
            async def fetch_html(self, url, *, allowed_url, max_bytes, **_kwargs):
                self.html_policy = allowed_url
                return {
                    "data": WIDGET,
                    "content_type": "text/html",
                    "final_url": "https://127.0.0.1/private",
                }

        redirected = UntrustedFinalUrl()
        redirected_result = asyncio.run(
            render_embed("https://store.steampowered.com/widget/123/", redirected)
        )
        self.assertFalse(redirected.html_policy("https://127.0.0.1/private"))
        self.assertEqual(redirected_result.status, "fetch_fallback")
        self.assertEqual(redirected_result.reason, "redirect_rejected")

    def test_steam_timeout_is_fetch_fallback(self):
        class Slow(FakeDownloader):
            called = False

            async def fetch_html(self, url, *, allowed_url, max_bytes, **_kwargs):
                self.called = True
                if not allowed_url(url):
                    return None
                await asyncio.sleep(0.05)
                return SafeHtml(WIDGET, "text/html")

        slow = Slow()
        with patch("keylol_embeds.STEAM_WIDGET_TIMEOUT_SECONDS", 0.001):
            result = asyncio.run(
                render_embed("https://store.steampowered.com/widget/123/", slow)
            )

        self.assertTrue(slow.called)
        self.assertEqual(result.status, "fetch_fallback")
        self.assertEqual(result.reason, "request_failed")
        self.assertTrue(result.fallback)

    def test_unknown_and_failed_embeds_are_visible_fallbacks(self):
        unknown = asyncio.run(render_embed("https://example.org/frame", FakeDownloader()))
        self.assertEqual(unknown.status, "fetch_fallback")
        self.assertIn("外部嵌入内容", unknown.html)
        self.assertNotIn("视频内容", unknown.html)

        class Failed(FakeDownloader):
            called = False

            async def fetch_html(self, url, *, allowed_url, max_bytes, **_kwargs):
                self.called = True
                if not allowed_url(url):
                    return None
                raise OSError("fixture network failure")

        failed_downloader = Failed()
        failed = asyncio.run(render_embed("https://store.steampowered.com/widget/123/", failed_downloader))
        self.assertTrue(failed_downloader.called)
        self.assertEqual(failed.status, "fetch_fallback")
        self.assertEqual(failed.reason, "request_failed")
        self.assertFalse(failed.loaded)
        self.assertTrue(failed.fallback)
        self.assertIn("Steam 商店内容", failed.html)

    def test_steam_invalid_content_type_is_fetch_fallback(self):
        downloader = FakeDownloader()
        downloader.html_content_type = "application/json"

        result = asyncio.run(
            render_embed("https://store.steampowered.com/widget/123/", downloader)
        )

        self.assertEqual(result.status, "fetch_fallback")
        self.assertEqual(result.reason, "content_type_rejected")
        self.assertTrue(result.fallback)
        self.assertIn("Steam 商店内容", result.html)

    def test_steam_diagnostics_distinguish_http_status_and_rejected_body(self):
        bad_status = FakeDownloader()
        bad_status.html_response = {
            "data": WIDGET,
            "content_type": "text/html",
            "status": 503,
            "final_url": "https://store.steampowered.com/widget/123/",
        }
        status_result = asyncio.run(
            render_embed("https://store.steampowered.com/widget/123/", bad_status)
        )
        self.assertEqual(status_result.status, "fetch_fallback")
        self.assertEqual(status_result.reason, "http_status")

        bad_body = FakeDownloader()
        bad_body.html_response = SafeHtml(b"", "text/html")
        body_result = asyncio.run(
            render_embed("https://store.steampowered.com/widget/123/", bad_body)
        )
        self.assertEqual(body_result.status, "fetch_fallback")
        self.assertEqual(body_result.reason, "body_rejected")

    def test_provider_error_heading_with_same_app_link_is_not_a_product_title(self):
        downloader = FakeDownloader()
        downloader.html_payload = b"""<html><body><div id="widget">
          <div class="header_container"><h1 class="main_text">
            <a href="https://store.steampowered.com/app/4813850/">Error</a>
          </h1></div>
          <div class="desc">Unable to read information for this item.</div>
        </div></body></html>"""

        result = asyncio.run(
            render_embed("https://store.steampowered.com/widget/4813850/", downloader)
        )

        self.assertEqual(result.status, "provider_error")
        self.assertEqual(result.reason, "provider_error")
        self.assertNotIn('data-keylol-embed-status="success"', result.html)
        self.assertNotIn("Steam 商店内容（静态截图无法完整加载）", result.html)


class KeylolVideoEmbedTests(unittest.TestCase):
    player = "https://keylol.com/source/plugin/onexin_html5player/open/videojs/html5player.html"
    source = "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/4813850/extras/fe2551175eefbb67e60b6252d07064a4.webm?t=1790221803"
    poster = "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/4813850/header.jpg"

    class Downloader:
        def __init__(self, result=None):
            self.result = result
            self.requests = []

        async def fetch_html(self, url, *, allowed_url, max_bytes):
            self.requests.append(("html", url))
            return None

        async def fetch_image(self, url, *, allowed_url, max_bytes):
            self.requests.append(("image", url))
            self.policy = allowed_url
            return self.result

    def render(self, *, params=None, downloader=None, url=None, fetch=True):
        downloader = downloader or self.Downloader()
        url = url or self.player + "?" + urlencode(params or {"mp4": self.source})
        result = asyncio.run(render_embed(url, downloader, fetch=fetch))
        return result, downloader

    def assert_static_video(self, result):
        self.assertEqual(result.status, "success")
        self.assertEqual(result.provider, "keylol_video")
        self.assertTrue(result.loaded)
        self.assertFalse(result.fallback)
        self.assertIn("视频内容", result.html)
        self.assertNotIn("外部嵌入内容", result.html)
        body = BeautifulSoup(result.html, "html.parser")
        self.assertIsNone(body.find(["script", "iframe", "object", "embed", "video", "source"]))
        self.assertFalse(any(name.lower().startswith("on") for node in body.find_all(True) for name in node.attrs))

    def test_real_post_contains_one_steam_widget_and_six_static_videos(self):
        fixture = (Path(__file__).parent / "fixtures/keylol_t1050511_embeds.html").read_text(encoding="utf-8")
        urls = [node["src"] for node in BeautifulSoup(fixture, "html.parser").find_all("iframe")]
        self.assertEqual([classify_embed(url) for url in urls], ["steam_widget"] + ["keylol_video"] * 6)
        for url in urls[1:]:
            with self.subTest(url=url):
                result, downloader = self.render(url=url)
                self.assert_static_video(result)
                self.assertIn("Steam 视频", result.html)
                self.assertEqual(downloader.requests, [])

    def test_trusted_poster_is_inlined_and_redirect_is_pinned_to_that_image(self):
        for poster in (self.poster, "https://img.keylol.com/data/attachment/forum/202609/24/preview.png"):
            with self.subTest(poster=poster):
                result, downloader = self.render(
                    params={"mp4": self.source, "poster": poster},
                    downloader=self.Downloader(SafeImage(PNG, "image/png")),
                )
                self.assert_static_video(result)
                self.assertIn("data:image/png;base64,", result.html)
                self.assertEqual(downloader.requests, [("image", poster)])
                self.assertTrue(downloader.policy(poster))
                for redirect in (self.source, "https://127.0.0.1/p.png", "https://elsewhere.org/p.png", poster + "?redirect=1"):
                    self.assertFalse(downloader.policy(redirect))

    def test_legacy_uppercase_header_remains_allowed_but_capsules_and_china_cdn_do_not(self):
        uppercase_header = self.poster.replace("header.jpg", "Header.JPG")
        result, downloader = self.render(
            params={"mp4": self.source, "poster": uppercase_header},
            downloader=self.Downloader(SafeImage(PNG, "image/png")),
        )
        self.assert_static_video(result)
        self.assertIn("data:image/png;base64,", result.html)
        self.assertEqual(downloader.requests, [("image", uppercase_header)])
        self.assertTrue(downloader.policy(uppercase_header))

        rejected_posters = (
            "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/"
            "4813850/capsule_184x69.jpg",
            "https://shared.cdn.queniuqe.com/store_item_assets/steam/apps/4813850/"
            "79f7ec4b9b4b3153dfd1d41a7fabef01d1ba4c5c/header.jpg",
            "https://shared.st.dl.eccdnx.com/store_item_assets/steam/apps/4813850/"
            "79f7ec4b9b4b3153dfd1d41a7fabef01d1ba4c5c/capsule_184x69.jpg",
        )
        for poster in rejected_posters:
            with self.subTest(poster=poster):
                result, downloader = self.render(
                    params={"mp4": self.source, "poster": poster}
                )
                self.assert_static_video(result)
                self.assertNotIn("<img", result.html)
                self.assertEqual(downloader.requests, [])

    def test_preview_failure_or_invalid_image_keeps_a_video_card(self):
        for response in (
            None,
            SafeImage(b"<script>alert(1)</script>", "image/png"),
            SafeImage(PNG, "text/html"),
            {"data": PNG, "content_type": "image/png", "final_url": "https://127.0.0.1/p.png"},
        ):
            with self.subTest(response=response):
                result, downloader = self.render(
                    params={"mp4": self.source, "poster": self.poster},
                    downloader=self.Downloader(response),
                )
                self.assert_static_video(result)
                self.assertNotIn("<img", result.html)
                self.assertEqual(downloader.requests, [("image", self.poster)])

    def test_query_is_not_a_video_or_poster_proxy(self):
        for malicious in (
            "https://127.0.0.1/private.mp4", "https://[::1]/x.m3u8",
            "https://192.168.1.1/a.ts", "https://localhost/x.webm",
            "https://untrusted.org/video.mp4", "https://user:secret@keylol.com/a.mp4",
            'javascript:alert(1)', 'https://evil.org/\"><script>alert(1)</script>',
        ):
            with self.subTest(malicious=malicious):
                result, downloader = self.render(params={"mp4": malicious, "poster": malicious})
                self.assert_static_video(result)
                self.assertNotIn("Steam 视频", result.html)
                self.assertEqual(downloader.requests, [])

    def test_steam_video_itself_cannot_be_used_as_a_poster(self):
        result, downloader = self.render(params={"mp4": self.source, "poster": self.source})
        self.assert_static_video(result)
        self.assertEqual(downloader.requests, [])

    def test_third_party_hls_and_another_apps_header_are_not_downloaded(self):
        hls_player = self.player.replace("/videojs/", "/tcplayer/")
        for url in (
            hls_player + "?" + urlencode({"m3u8": "https://untrusted.org/watch.m3u8"}),
            self.player + "?" + urlencode({"mp4": self.source, "poster": self.poster.replace("4813850", "123")}),
        ):
            with self.subTest(url=url):
                result, downloader = self.render(url=url)
                self.assert_static_video(result)
                self.assertEqual(downloader.requests, [])

    def test_duplicate_unknown_or_malformed_query_does_not_fetch(self):
        for query in (
            urlencode({"mp4": self.source, "poster": self.poster}) + "&poster=" + self.poster,
            urlencode({"mp4": self.source, "poster": self.poster, "redirect": "https://127.0.0.1/"}),
            "mp4=%FF", "&".join("mp4=x" for _ in range(10)),
        ):
            with self.subTest(query=query):
                result, downloader = self.render(url=self.player + "?" + query)
                self.assert_static_video(result)
                self.assertEqual(downloader.requests, [])

    def test_video_stays_recognized_when_network_fetches_are_disabled(self):
        result, downloader = self.render(params={"mp4": self.source, "poster": self.poster}, fetch=False)
        self.assert_static_video(result)
        self.assertEqual(downloader.requests, [])

    def test_only_exact_local_wrapper_paths_are_classified_as_video(self):
        for value in (
            self.player.replace("keylol.com", "keylol.com.evil.org"),
            self.player.replace("keylol.com", "user:pass@keylol.com"),
            self.player.replace("keylol.com", "keylol.com:443"),
            self.player.replace("keylol.com", "127.0.0.1"),
            self.player.replace("https:", "http:"),
            self.player.replace("html5player.html", "other.html"),
            self.player + "#fragment", self.player + "\n",
            "https://www.onexin.com/embed/abc123", "https://example.org/frame",
        ):
            with self.subTest(value=value):
                self.assertEqual(classify_embed(value), "unknown")
                result, downloader = self.render(url=value)
                self.assertIn("外部嵌入内容", result.html)
                self.assertEqual(downloader.requests, [])


if __name__ == "__main__":
    unittest.main()
