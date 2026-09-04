"""Cookie diagnostics must not disclose account state to other users."""

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

try:
    from test_main_render_mode import main
except ModuleNotFoundError:  # pragma: no cover - package-style test runners
    from tests.test_main_render_mode import main


def event_for(*, admin, private):
    return SimpleNamespace(
        is_admin=lambda: admin,
        is_private_chat=lambda: private,
        plain_result=lambda text: text,
    )


async def collect(handler, event):
    return [result async for result in handler(event)]


class CookieCheckPrivacyTests(unittest.TestCase):
    def test_untrusted_contexts_never_read_credentials_or_call_network(self):
        for command, accessor, api in (
            ("keylol_check", "_cookie", "fetch_article"),
            ("tieba_check", "_tieba_cookie", "check_tieba_cookie"),
        ):
            for admin, private in ((False, False), (False, True), (True, False)):
                with self.subTest(command=command, admin=admin, private=private):
                    plugin = main.KeylolScreenshotPlugin(object(), {})
                    network = AsyncMock()
                    with (
                        patch.object(plugin, accessor) as read_cookie,
                        patch.object(main, api, network),
                    ):
                        results = asyncio.run(
                            collect(
                                getattr(plugin, command),
                                event_for(admin=admin, private=private),
                            )
                        )
                    self.assertEqual(
                        results, ["Cookie 验证仅限 AstrBot 管理员在私聊中执行。"]
                    )
                    read_cookie.assert_not_called()
                    network.assert_not_awaited()

    def test_private_admin_can_check_keylol(self):
        plugin = main.KeylolScreenshotPlugin(
            object(), {"keylol_cookie": "test-cookie-placeholder"}
        )
        article = SimpleNamespace(has_locked_resources=False, title="测试帖子")
        network = AsyncMock(return_value=article)
        with patch.object(main, "fetch_article", network):
            results = asyncio.run(
                collect(plugin.keylol_check, event_for(admin=True, private=True))
            )
        network.assert_awaited_once()
        self.assertIn("Cookie 有效", results[0])

    def test_private_admin_can_check_tieba(self):
        plugin = main.KeylolScreenshotPlugin(
            object(), {"tieba_cookie": "test-cookie-placeholder"}
        )
        network = AsyncMock(return_value="test-account-placeholder")
        with patch.object(main, "check_tieba_cookie", network):
            results = asyncio.run(
                collect(plugin.tieba_check, event_for(admin=True, private=True))
            )
        network.assert_awaited_once()
        self.assertEqual(
            results, ["贴吧 Cookie 有效，当前账号：test-account-placeholder"]
        )

    def test_private_admin_can_see_missing_configuration(self):
        plugin = main.KeylolScreenshotPlugin(object(), {})
        for command, field in (
            ("keylol_check", "keylol_cookie"),
            ("tieba_check", "tieba_cookie"),
        ):
            with self.subTest(command=command):
                results = asyncio.run(
                    collect(
                        getattr(plugin, command), event_for(admin=True, private=True)
                    )
                )
                self.assertEqual(results, [f"尚未配置 {field}。"])


if __name__ == "__main__":
    unittest.main()
