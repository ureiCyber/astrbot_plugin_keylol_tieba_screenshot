"""Capture-level routing, error attribution and fallback diagnostics contracts."""
import asyncio
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import tieba_browser


class CaptureFailureTests(unittest.TestCase):
    def capture(self, *, snapshot, transform_error=None):
        page = SimpleNamespace(
            main_frame=object(), url="https://tieba.baidu.com/p/123",
            route=AsyncMock(), goto=AsyncMock(return_value=SimpleNamespace(status=200)),
            close=AsyncMock(), set_default_timeout=lambda _: None,
            content=AsyncMock(return_value='<title>BDUSS=secret-bduss</title>'),
            screenshot=AsyncMock(side_effect=RuntimeError('secret-bduss')),
        )

        async def evaluate(script, *_):
            if script == tieba_browser.PAGE_SNAPSHOT_SCRIPT:
                return snapshot
            if script == tieba_browser._TRANSFORM_SCRIPT:
                raise transform_error or RuntimeError('NO_FIRST_POST')
            return {}

        page.evaluate = evaluate
        context = SimpleNamespace(add_cookies=AsyncMock(), new_page=AsyncMock(return_value=page), close=AsyncMock())
        browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
        runtime = SimpleNamespace(devices={}, chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)), stop=AsyncMock())
        with TemporaryDirectory() as directory, patch.object(tieba_browser, 'DEBUG_DIRECTORY', Path(directory)), patch.object(
            tieba_browser, 'async_playwright', return_value=SimpleNamespace(start=AsyncMock(return_value=runtime))
        ):
            with self.assertRaises(tieba_browser.TiebaBrowserCaptureError) as caught:
                asyncio.run(tieba_browser.capture_tieba_webpage_screenshot(
                    'https://tieba.baidu.com/p/123', cookie='BDUSS=secret-bduss; STOKEN=secret-stoken'))
            error = caught.exception
            artifacts = error.diagnostics['debug_artifacts']
            self.assertTrue(artifacts['html_saved'])
            self.assertNotIn('secret-bduss', Path(artifacts['html_path']).read_text(encoding='utf-8'))
            self.assertNotIn('secret-bduss', json.dumps(error.diagnostics))
        page.close.assert_awaited_once()
        return error, page.route.await_args.args[1], page.main_frame

    def test_unrelated_transform_error_is_not_missing_first_post(self):
        error, _, _ = self.capture(snapshot={'has_first_post': True, 'ready_state': 'complete'},
                                   transform_error=RuntimeError('TypeError: broken transform secret-bduss'))
        self.assertEqual(error.reason, 'tieba_transform_failed')
        self.assertEqual(error.stage, 'transform')

    def test_verification_failure_retains_page_diagnostics_and_original_reason(self):
        error, _, _ = self.capture(snapshot={
            'url': 'https://tieba.baidu.com/p/123?token=secret-stoken', 'title': '百度安全验证',
            'body_text': '登录 验证 打开APP', 'body_text_length': 13,
            'html_length': 200, 'ready_state': 'complete', 'has_verify_widget': True,
            'selector_counts': {'[data-field]': 0},
        })
        self.assertEqual(error.reason, 'tieba_verify_required')
        self.assertEqual(error.diagnostics['selector_counts'], {'[data-field]': 0})
        self.assertTrue(error.diagnostics['contains_login'])
        self.assertNotIn('body_text', error.diagnostics)

    def test_runtime_route_allows_bundles_but_blocks_mutations_and_foreign_reads(self):
        _, handler, frame = self.capture(snapshot={'has_first_post': True})

        async def check():
            for url, resource, method, allowed, strips_cookie in (
                ('https://tb2.bdstatic.com/tb/mobile/main.js', 'script', 'GET', True, True),
                ('https://tieba.baidu.com/static/main.js', 'script', 'GET', True, True),
                ('https://tieba.baidu.com/p/123', 'fetch', 'GET', True, False),
                ('https://tieba.baidu.com/p/123', 'fetch', 'POST', False, False),
                ('https://tieba.baidu.com/p/456', 'fetch', 'GET', False, False),
                ('https://tieba.baidu.com/f/commit/post/add', 'fetch', 'POST', False, False),
                ('https://evil.example/main.js', 'script', 'GET', False, False),
                ('https://passport.baidu.com/login', 'document', 'GET', False, False),
            ):
                request = SimpleNamespace(url=url, resource_type=resource, method=method, frame=frame,
                    all_headers=AsyncMock(return_value={'cookie': 'BDUSS=secret-bduss', 'accept': '*/*'}))
                route = SimpleNamespace(continue_=AsyncMock(), abort=AsyncMock())
                await handler(route, request)
                self.assertEqual(route.continue_.await_count, int(allowed), (url, method))
                self.assertEqual(route.abort.await_count, int(not allowed), (url, method))
                if strips_cookie:
                    self.assertNotIn('cookie', route.continue_.await_args.kwargs['headers'])
        asyncio.run(check())
