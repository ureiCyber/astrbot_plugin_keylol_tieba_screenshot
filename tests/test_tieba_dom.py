"""Real-browser regressions for first-floor selection, not HTML string matching."""
import asyncio
import unittest

import tieba_browser
from tieba_dom import PAGE_SNAPSHOT_SCRIPT


class TiebaDOMTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if tieba_browser.async_playwright is None:
            self.skipTest("Playwright is not installed")
        self.runtime = await tieba_browser.async_playwright().start()
        self.browser = None
        for options in ({"channel": "chrome"}, {"channel": "msedge"}, {}):
            try:
                self.browser = await self.runtime.chromium.launch(headless=True, **options)
                break
            except Exception:
                pass
        if self.browser is None:
            await self.runtime.stop()
            self.skipTest("No installed Chromium browser")
        self.page = await self.browser.new_page(viewport={"width": 440, "height": 844})
        await self.page.route("**/*", lambda route: route.abort())

    async def asyncTearDown(self):
        if self.browser is not None:
            await self.browser.close()
        await self.runtime.stop()

    async def test_priority_and_layout_variants_keep_only_proven_floor_one(self):
        examples = (
            ('<section data-field=\'{"content":{"post_no":1}}\'><div id="post_content_42">主楼</div></section>', 'field:'),
            ('<article data-field-json=\'{"floor":1}\'><div class="p_content">主楼</div></article>', 'field:'),
            ('<article data-floor="1" data-pid="42"><div data-role="post-content">主楼</div></article>', 'floor:'),
            ('<li data-post-no="1"><div class="post-content">主楼</div></li>', 'floor:'),
            ('<div class="l_post"><div class="d_post_content">主楼</div><span class="tail-info">来自客户端</span><span class="tail-info">1楼</span></div>', 'desktop_tail:'),
        )
        for fragment, selected in examples:
            with self.subTest(selected=selected, fragment=fragment):
                await self.page.set_content('<html><body><article data-floor="2"><div class="post-content">回复在前</div></article>' + fragment + '</body></html>')
                before = await self.page.evaluate(PAGE_SNAPSHOT_SCRIPT)
                self.assertTrue(before['has_first_post'])
                self.assertTrue(before['selected_selector'].startswith(selected))
                result = await self.page.evaluate(tieba_browser._TRANSFORM_SCRIPT, {"sourceUrl": "https://tieba.baidu.com/p/123"})
                self.assertTrue(result['selectedSelector'].startswith(selected))
                self.assertEqual(await self.page.locator('[data-tieba-capture-article="1"]').count(), 1)
                self.assertNotIn('回复在前', await self.page.locator('body').inner_text())
                stats = await self.page.evaluate(tieba_browser._FINALIZE_IMAGES_SCRIPT)
                self.assertGreater(stats['captureHeight'], 0)

    async def test_reply_advertisement_id_and_empty_floor_are_not_first_post(self):
        for fragment in (
            '<div class="l_post" data-field=\'{"content":{"post_no":2}}\'><div class="d_post_content">回复</div><span class="tail-info">1楼</span></div>',
            '<div data-pid="42"><div id="post_content_42">广告或回复</div></div>',
            '<div data-floor="1"><div class="post-content"></div></div>',
            '<div data-floor="1"><div>只有作者，没有正文</div></div>',
        ):
            with self.subTest(fragment=fragment):
                await self.page.set_content(fragment)
                self.assertFalse((await self.page.evaluate(PAGE_SNAPSHOT_SCRIPT))['has_first_post'])
                with self.assertRaisesRegex(Exception, 'NO_FIRST_POST'):
                    await self.page.evaluate(tieba_browser._TRANSFORM_SCRIPT, {"sourceUrl": "https://tieba.baidu.com/p/123"})

    async def test_bounded_wait_distinguishes_pending_floor_and_unknown_dom(self):
        for fragment, reason in (
            ('<article data-floor="1"><div class="post-content"></div><span class="loading">加载中</span></article>', 'tieba_first_post_timeout'),
            ('<h1>贴吧帖子</h1><div>没有支持的首帖结构</div>', 'tieba_dom_changed'),
        ):
            await self.page.set_content(fragment)
            with self.assertRaises(tieba_browser.TiebaBrowserCaptureError) as caught:
                await tieba_browser._wait_for_first_post(self.page, 100)
            self.assertEqual(caught.exception.reason, reason)

    async def test_late_first_post_waits_for_content_not_just_container(self):
        await self.page.set_content('<article data-floor="1"><div class="post-content"></div></article>')
        await self.page.evaluate("setTimeout(() => document.querySelector('.post-content').textContent = '异步主楼', 120)")
        result = await tieba_browser._wait_for_first_post(self.page, 2000)
        self.assertTrue(result['has_first_post'])


class TiebaScriptRouteTests(unittest.TestCase):
    def test_only_first_party_https_js_bundles_allowed(self):
        self.assertTrue(tieba_browser._is_allowed_script_request('https://tb2.bdstatic.com/tb/mobile/main.js?v=1'))
        self.assertTrue(tieba_browser._is_allowed_script_request('https://tieba.baidu.com/static/main.js'))
        for url in ('http://tb2.bdstatic.com/main.js', 'https://evil.example/main.js',
                    'https://tiebapic.baidu.com/main.js', 'https://tb2.bdstatic.com/jsonp?callback=x',
                    'https://user:pass@tb2.bdstatic.com/main.js'):
            self.assertFalse(tieba_browser._is_allowed_script_request(url), url)
