"""Execute the renderer scripts in Chromium against offline post fixtures."""
import asyncio
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse
import unittest
from unittest.mock import AsyncMock, patch

from bs4 import BeautifulSoup
from PIL import Image

import keylol_browser as browser
from keylol_embeds import render_embed
from safe_media import SafeHtml


def tiny_png():
    data = BytesIO()
    with Image.new('RGB', (4, 4), 'blue') as image:
        image.save(data, format='PNG')
    return data.getvalue()


class BrowserResourceBehaviorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if browser.async_playwright is None:
            self.skipTest('Playwright is not installed')
        self.playwright = await browser.async_playwright().start()
        self.chrome = None
        for options in ({}, {'channel': 'chrome'}, {'channel': 'msedge'}):
            try:
                self.chrome = await self.playwright.chromium.launch(headless=True, **options)
                break
            except Exception:
                continue
        if self.chrome is None:
            await self.playwright.stop()
            self.skipTest('No installed Chromium browser')
        self.context = await self.chrome.new_context(**{
            **{k: v for k, v in self.playwright.devices['iPhone 15'].items() if k != 'default_browser_type'},
            'device_scale_factor': 1,
        })
        self.page = await self.context.new_page()
        self.requests = []
        self.aborted_requests = []
        self.fixture = (Path(__file__).parent / 'fixtures/keylol_resources.html').read_text(encoding='utf-8')

        async def route_handler(route, request):
            self.requests.append((request.url, request.resource_type))
            if request.url == 'https://keylol.com/t1-1-1':
                await route.fulfill(content_type='text/html; charset=utf-8', body=self.fixture)
            else:
                self.aborted_requests.append((request.url, request.resource_type))
                await route.abort()
        await self.page.route('**/*', route_handler)
        await self.page.goto('https://keylol.com/t1-1-1', wait_until='domcontentloaded', timeout=5000)

    async def asyncTearDown(self):
        if getattr(self, 'chrome', None):
            await self.chrome.close()
            await self.playwright.stop()

    async def transform(self):
        return await self.page.evaluate(browser._TRANSFORM_SCRIPT, {
            'sourceUrl': 'https://keylol.com/t1-1-1', 'viewportWidth': 390,
            'suppliedTitle': '', 'suppliedAuthor': '', 'suppliedPublishedAt': '',
            'sectionTitle': '', 'hideToc': True,
        })

    async def test_structural_collapses_expand_before_image_scan_and_nested_images_load(self):
        info = await self.transform()
        self.assertEqual(info['autoExpandedCollapseCount'], 2)
        self.assertEqual(await self.page.locator('#ordinary.sff_collapsed, #nested.sff_collapsed').count(), 0)
        self.assertEqual(info['externalSources'], ['https://images.example.com/screen.png'])
        self.assertEqual(info['imageCount'], 1)
        self.assertEqual(info['externalImageCount'], 1)
        self.assertTrue(await self.page.locator('#external').is_visible())
        self.assertIn('screen.png', await self.page.locator('#external').get_attribute('data-keylol-candidates'))
        await self.page.route('https://images.example.com/screen.png', lambda route: route.fulfill(content_type='image/png', body=tiny_png()))
        state = await self.page.evaluate(browser._SCROLL_SCRIPT, {'maxImages': 500, 'maxHeight': 100000, 'perImageTimeoutMs': 200})
        stats = await self.page.evaluate(browser._FINALIZE_IMAGES_SCRIPT)
        self.assertEqual(state['loaded'], 1)
        self.assertEqual(stats['loaded'], 1)
        self.assertEqual(stats['failed'], 0)

    async def test_permissions_and_peek_remain_untouched_without_ajax(self):
        info = await self.transform()
        for name in ('showhide', 'reply', 'peek', 'permission', 'spoiler', 'login', 'not-structural'):
            with self.subTest(name=name):
                self.assertIn('sff_collapsed', await self.page.locator('#' + name).get_attribute('class'))
        self.assertIsNone(await self.page.evaluate('window.clicked'))
        self.assertFalse(any('/permission' in url for url, _ in self.requests))
        self.assertEqual(info['imageCount'], 1)
        await self.page.locator('#external').evaluate('(image) => image.remove()')
        stats = await self.page.evaluate(browser._FINALIZE_IMAGES_SCRIPT)
        self.assertEqual(stats['failed'], 0, 'Protected images do not lower screenshot quality')

    async def test_unknown_iframe_is_not_a_video_and_steam_candidate_is_preserved(self):
        info = await self.transform()
        self.assertEqual(len(info['embeds']), 2)
        self.assertIn('/widget/2369390/', info['embeds'][0]['url'])
        cards = await self.page.locator('[data-keylol-embed]').all_text_contents()
        self.assertTrue(all('外部嵌入内容' in text for text in cards))
        self.assertTrue(all('视频内容' not in text for text in cards))
        self.assertEqual(await self.page.locator('iframe').count(), 0)

    async def test_failed_proxy_image_is_card_even_when_placeholder_loaded(self):
        await self.transform()
        state = await self.page.evaluate(browser._SCROLL_SCRIPT, {'maxImages': 500, 'maxHeight': 100000, 'perImageTimeoutMs': 100})
        stats = await self.page.evaluate(browser._FINALIZE_IMAGES_SCRIPT)
        self.assertEqual(state['failed'], 1)
        self.assertEqual(stats['failed'], 1)
        self.assertEqual(stats['failedExternal'], 1)
        self.assertIn('图片加载失败', await self.page.inner_text('.message'))

    async def test_http_private_localhost_candidates_cannot_be_fulfilled(self):
        await self.page.locator('.message').first.evaluate('''node => node.innerHTML = '<img src="http://public.example.com/a.png"><img src="https://127.0.0.1/a.png"><img src="https://192.168.1.2/a.png"><img src="https://localhost/a.png">' ''')
        info = await self.transform()
        self.assertEqual(info['missingImageCount'], 1)
        self.assertTrue(all(not browser.is_public_https_url(url) for url in info['externalSources']))
        self.assertEqual(info['externalImageCount'], 4)

    async def test_live_embed_fixture_runs_through_static_provider_cards_without_media_requests(self):
        thread_url = 'https://keylol.com/t1050511-1-1?mobile=no'
        embed_fixture = BeautifulSoup(
            (Path(__file__).parent / 'fixtures/keylol_t1050511_embeds.html').read_text(encoding='utf-8'),
            'html.parser',
        )
        post_shell = embed_fixture.select_one('#post_1')
        self.assertIsNotNone(post_shell)
        post_number = post_shell.select_one('#postnum1').extract()
        post_content = post_shell.select_one('#postmessage_1').extract()
        article = embed_fixture.new_tag('article', id='post_1', attrs={'class': ['plc']})
        message = embed_fixture.new_tag('div', attrs={'class': ['message']})
        article.append(post_number)
        message.append(post_content)
        article.append(message)
        post_shell.replace_with(article)
        fixture_html = str(embed_fixture)

        async def serve_embed_fixture(route, _request):
            await route.fulfill(content_type='text/html; charset=utf-8', body=fixture_html)

        await self.page.route(thread_url, serve_embed_fixture)
        self.requests.clear()
        self.aborted_requests.clear()
        await self.page.goto(thread_url, wait_until='domcontentloaded', timeout=5000)

        steam_fixture = (
            Path(__file__).parent / 'fixtures/steam_widget_provider_error.html'
        ).read_bytes()

        class FixtureDownloader:
            def __init__(self):
                self.html_calls = []
                self.image_calls = []

            async def fetch_html(self, url, *, allowed_url, max_bytes, **_kwargs):
                self.html_calls.append((url, allowed_url(url), max_bytes))
                return SafeHtml(steam_fixture, 'text/html')

            async def fetch_image(self, url, *, allowed_url, max_bytes, **_kwargs):
                self.image_calls.append((url, allowed_url(url), max_bytes))
                return None

        downloader = FixtureDownloader()
        info = await self.page.evaluate(browser._TRANSFORM_SCRIPT, {
            'sourceUrl': thread_url, 'viewportWidth': 390,
            'suppliedTitle': '', 'suppliedAuthor': '', 'suppliedPublishedAt': '',
            'sectionTitle': '', 'hideToc': True,
        })
        self.assertEqual(len(info['embeds']), 7)

        results = []
        for embed in info['embeds']:
            result = await render_embed(embed['url'], downloader)
            results.append(result)
            installed = await self.page.evaluate(
                browser._INSTALL_EMBED_SCRIPT,
                {'index': int(embed['index']), 'html': result.html},
            )
            self.assertTrue(installed)

        self.assertEqual([result.status for result in results].count('provider_error'), 1)
        self.assertEqual([result.status for result in results].count('success'), 6)
        self.assertEqual(len(downloader.html_calls), 1)
        self.assertTrue(all(allowed for _, allowed, _ in downloader.html_calls))
        self.assertEqual(downloader.image_calls, [], 'No Steam error image or video poster is present')

        self.assertEqual(await self.page.locator('[data-keylol-embed-provider="steam"]').count(), 1)
        self.assertEqual(await self.page.locator('[data-keylol-embed-provider="keylol_video"]').count(), 6)
        self.assertIn('错误', await self.page.locator('.keylol-steam-widget-error-title').inner_text())
        self.assertIn('无法读取这件物品的信息。', await self.page.locator('.keylol-steam-widget-error-message').inner_text())
        self.assertEqual(await self.page.locator('.media-card-video').count(), 6)
        self.assertEqual(await self.page.locator('.keylol-browser-embed-fallback').count(), 0)
        self.assertNotIn('外部嵌入内容', await self.page.locator('article.plc').inner_text())
        self.assertEqual(await self.page.locator('iframe, object, embed, video, script').count(), 0)

        installed_html = await self.page.locator('[data-keylol-embed]').evaluate_all(
            '(nodes) => nodes.map((node) => node.outerHTML)'
        )
        for card_html in installed_html:
            card = BeautifulSoup(card_html, 'html.parser')
            self.assertIsNone(card.find(['script', 'iframe', 'object', 'embed', 'video']))
            for node in card.find_all(True):
                self.assertFalse(any(name.lower().startswith('on') for name in node.attrs))

        embed_urls = {embed['url'] for embed in info['embeds']}
        aborted_documents = {
            url for url, resource_type in self.aborted_requests if resource_type == 'document'
        }
        self.assertTrue(embed_urls.issubset(aborted_documents))
        requested_hosts = {urlparse(url).hostname for url, _ in self.requests}
        self.assertNotIn('shared.akamai.steamstatic.com', requested_hosts)
        self.assertNotIn('video.akamai.steamstatic.com', requested_hosts)


class ExternalFulfillTests(unittest.IsolatedAsyncioTestCase):
    async def test_downloaded_image_is_fulfilled_without_browser_headers_or_cookie(self):
        route = SimpleNamespace(fulfill=AsyncMock(), abort=AsyncMock(), continue_=AsyncMock())
        downloader = SimpleNamespace(fetch_image=AsyncMock(return_value=SimpleNamespace(data=tiny_png(), content_type='image/png')))
        await browser._fulfill_external_image(route, downloader, 'https://images.example.com/a.png')
        downloader.fetch_image.assert_awaited_once_with('https://images.example.com/a.png')
        route.continue_.assert_not_awaited()
        self.assertEqual(route.fulfill.await_args.kwargs['body'], tiny_png())
        self.assertNotIn('cookie', str(route.fulfill.await_args.kwargs).lower())

    async def test_download_failure_aborts_request_for_existing_image_error_path(self):
        route = SimpleNamespace(fulfill=AsyncMock(), abort=AsyncMock())
        downloader = SimpleNamespace(fetch_image=AsyncMock(return_value=None))
        await browser._fulfill_external_image(route, downloader, 'https://images.example.com/a.png')
        route.abort.assert_awaited_once()
        route.fulfill.assert_not_awaited()


class CaptureQualityTests(unittest.IsolatedAsyncioTestCase):
    async def capture_fixture(self, *, embed_loaded=False):
        page = SimpleNamespace(main_frame=object(), url='https://keylol.com/t1-1-1',
                               route=AsyncMock(), goto=AsyncMock(), close=AsyncMock(), set_default_timeout=lambda _: None)
        async def evaluate(script, *args):
            if script == browser._TOC_DISCOVERY_SCRIPT:
                return []
            if script == browser._TRANSFORM_SCRIPT:
                return {'title': 'fixture', 'imageCount': 0, 'embeds': [{'index': 0, 'url': 'https://store.steampowered.com/widget/10/'}], 'autoExpandedCollapseCount': 3,
                        'externalSources': ['https://images.example.com/a.png', 'https://127.0.0.1/a.png']}
            if script == browser._INSTALL_EMBED_SCRIPT:
                return True
            if script == browser._FINALIZE_IMAGES_SCRIPT:
                return {'loaded': 0, 'failed': 0, 'pageHeight': 100, 'captureHeight': 100}
            return {}
        page.evaluate = AsyncMock(side_effect=evaluate)
        context = SimpleNamespace(new_page=AsyncMock(return_value=page), close=AsyncMock())
        chromium = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
        playwright = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=chromium)), devices={}, stop=AsyncMock())
        starter = SimpleNamespace(start=AsyncMock(return_value=playwright))
        with patch.object(browser, 'async_playwright', return_value=starter), patch.object(browser, '_capture_mobile_page_tiles', new=AsyncMock()), patch.object(browser, 'render_embed', new=AsyncMock(return_value=SimpleNamespace(html='Steam 商店内容', loaded=embed_loaded))):
            result = await browser.capture_keylol_webpage_screenshot('https://keylol.com/t1-1-1', output_path='unused-fixture.png')
        return result, page

    async def test_embed_fallback_is_partial_with_no_failed_images(self):
        result, _ = await self.capture_fixture()
        self.assertEqual(result.status, browser.KeylolBrowserCaptureStatus.PARTIAL)
        self.assertEqual(result.failed_image_count, 0)
        self.assertEqual(result.embed_count, 1)
        self.assertEqual(result.loaded_embed_count, 0)
        self.assertEqual(result.fallback_embed_count, 1)
        self.assertEqual(result.auto_expanded_collapse_count, 3)

    async def test_complete_embed_and_expanded_collapses_remain_ok(self):
        result, _ = await self.capture_fixture(embed_loaded=True)
        self.assertEqual(result.status, browser.KeylolBrowserCaptureStatus.OK)
        self.assertEqual(result.loaded_embed_count, 1)
        self.assertEqual(result.fallback_embed_count, 0)

    async def test_real_capture_route_only_proxies_approved_body_images(self):
        _, page = await self.capture_fixture()
        handler = page.route.await_args.args[1]
        for url, resource_type, outcome in (
            ('https://images.example.com/a.png', 'image', 'proxy'),
            ('https://other.example.com/a.png', 'image', 'abort'),
            ('https://127.0.0.1/a.png', 'image', 'abort'),
            ('https://images.example.com/a.png', 'script', 'abort'),
            ('https://images.example.com/a.png', 'fetch', 'abort'),
            ('https://store.steampowered.com/widget/10/', 'document', 'abort'),
            ('https://blob.keylol.com/forum/a.png', 'image', 'continue'),
        ):
            with self.subTest(url=url, resource_type=resource_type):
                route = SimpleNamespace(continue_=AsyncMock(), abort=AsyncMock())
                request = SimpleNamespace(url=url, resource_type=resource_type, method='GET', frame=page.main_frame,
                                          all_headers=AsyncMock(return_value={'cookie': 'sid=sentinel; BDUSS=sentinel'}))
                with patch.object(browser, '_fulfill_external_image', new=AsyncMock()) as proxy:
                    await handler(route, request)
                if outcome == 'proxy':
                    proxy.assert_awaited_once()
                    route.continue_.assert_not_awaited()
                elif outcome == 'continue':
                    route.continue_.assert_awaited_once_with(headers={})
                else:
                    route.abort.assert_awaited_once()
                    proxy.assert_not_awaited()
                    route.continue_.assert_not_awaited()
