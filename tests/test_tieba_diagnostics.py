from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from tieba_diagnostics import (
    classify_tieba_failure,
    sanitize_diagnostics,
    save_failure_artifacts,
)


def snapshot(**overrides):
    value = {
        "url": "https://tieba.baidu.com/p/123?see_lz=1",
        "title": "测试帖子_百度贴吧",
        "ready_state": "complete",
        "html_length": 4_000,
        "body_text_length": 1_200,
        "body_text": "帖子内容",
        "has_first_post": False,
        "has_post_features": False,
        "has_loading_indicator": False,
        "has_login_form": False,
        "has_verify_widget": False,
        "has_app_gate": False,
        "selector_counts": {"[data-post-id]": 0, ".l_post": 0},
        "iframe_count": 0,
    }
    value.update(overrides)
    return value


class TiebaFailureClassificationTests(unittest.TestCase):
    def test_success_has_no_failure_reason(self):
        self.assertEqual(classify_tieba_failure(snapshot(has_first_post=True)), "")

    def test_structural_failure_pages_have_specific_reasons(self):
        cases = (
            (snapshot(has_login_form=True), None, "tieba_login_required"),
            (snapshot(has_verify_widget=True), None, "tieba_verify_required"),
            (
                snapshot(has_verify_widget=True, body_text="检测到异常访问，触发风控"),
                None,
                "tieba_risk_control",
            ),
            (snapshot(has_app_gate=True), None, "tieba_app_redirect"),
            (snapshot(), 404, "tieba_post_not_found"),
            (snapshot(), 403, "tieba_permission_denied"),
            (snapshot(title="百度安全验证"), 403, "tieba_verify_required"),
            (snapshot(), 410, "tieba_post_not_found"),
            (snapshot(), 429, "tieba_risk_control"),
        )
        for page, status, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(classify_tieba_failure(page, status=status), expected)

    def test_weak_keywords_in_navigation_or_post_content_do_not_misclassify(self):
        page = snapshot(
            title="普通帖子_百度贴吧",
            body_text="登录 下载APP 安全验证 风控 帖子正文很长" * 60,
            body_text_length=2_900,
            html_length=25_000,
            has_post_features=True,
        )
        self.assertEqual(classify_tieba_failure(page), "tieba_dom_changed")
        self.assertEqual(classify_tieba_failure(snapshot(body_text="登录 下载APP")), "tieba_dom_changed")
        self.assertEqual(classify_tieba_failure(snapshot(title="打开客户端")), "tieba_app_redirect")
        self.assertEqual(classify_tieba_failure(snapshot(), status=503), "tieba_http_error")

    def test_title_and_short_page_evidence_classify_weak_signals(self):
        cases = (
            (snapshot(title="百度安全验证"), "tieba_verify_required"),
            (
                snapshot(title="百度贴吧访问异常", body_text="触发风控，请稍后重试"),
                "tieba_risk_control",
            ),
            (snapshot(title="请先登录_百度贴吧", body_text="请先登录"), "tieba_login_required"),
            (snapshot(title="打开贴吧客户端"), "tieba_app_redirect"),
            (snapshot(title="帖子不存在_百度贴吧"), "tieba_post_not_found"),
            (snapshot(title="无权查看该帖子_百度贴吧"), "tieba_permission_denied"),
        )
        for page, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(classify_tieba_failure(page), expected)

    def test_loaded_async_timeout_and_dom_change_are_distinguished(self):
        self.assertEqual(
            classify_tieba_failure(snapshot(has_loading_indicator=True), wait_timed_out=True),
            "tieba_first_post_timeout",
        )
        self.assertEqual(
            classify_tieba_failure(snapshot(has_post_features=True)),
            "tieba_dom_changed",
        )
        self.assertEqual(
            classify_tieba_failure(
                snapshot(has_post_features=True), wait_timed_out=True
            ),
            "tieba_dom_changed",
        )
        self.assertEqual(
            classify_tieba_failure(
                snapshot(first_floor_seen=True, body_text="", body_text_length=0),
                wait_timed_out=True,
            ),
            "tieba_first_post_timeout",
        )
        self.assertEqual(
            classify_tieba_failure(snapshot(ready_state="loading", html_length=10_000), wait_timed_out=True),
            "tieba_page_not_loaded",
        )

    def test_blank_and_empty_document_classification(self):
        self.assertEqual(
            classify_tieba_failure(
                snapshot(
                    html_length=0,
                    body_text="",
                    body_text_length=0,
                    ready_state="complete",
                )
            ),
            "tieba_blank_page",
        )
        self.assertEqual(
            classify_tieba_failure(
                snapshot(
                    html_length=0,
                    body_text="",
                    body_text_length=0,
                    ready_state="loading",
                )
            ),
            "tieba_page_not_loaded",
        )
        self.assertEqual(
            classify_tieba_failure(
                snapshot(html_length=80, body_text="异常页面", body_text_length=4)
            ),
            "tieba_dom_changed",
        )

    def test_blocked_document_routes_classify_without_reading_query_text(self):
        page = snapshot(url="https://tieba.baidu.com/p/123")
        cases = (
            (
                "https://passport.baidu.com/v2/?next=tieba.baidu.com/p/123",
                "tieba_login_required",
            ),
            (
                "https://wappass.baidu.com/passport/?redirect=arbitrary",
                "tieba_login_required",
            ),
            (
                "https://wappass.baidu.com/captcha/?q=unrelated",
                "tieba_verify_required",
            ),
            (
                "https://tieba.baidu.com/p/123?next=wappass.baidu.com/captcha",
                "tieba_dom_changed",
            ),
        )
        for blocked_url, expected in cases:
            with self.subTest(blocked_url=blocked_url):
                self.assertEqual(
                    classify_tieba_failure(
                        page,
                        blocked_document_url=blocked_url,
                    ),
                    expected,
                )


class TiebaDiagnosticsPrivacyTests(unittest.TestCase):
    def test_log_metadata_redacts_secrets_and_url_query_values(self):
        secret_bduss = "bduss-secret-123"
        secret_stoken = "stoken-secret-456"
        page = snapshot(
            url=(
                "https://tieba.baidu.com/p/123?see_lz=1&BDUSS="
                + secret_bduss
            ),
            title=f"帖子 {secret_bduss} STOKEN={secret_stoken}",
            body_text=f"登录正文 {secret_bduss}，安全验证。",
            selector_counts={"[data-id='" + secret_stoken + "']": 2},
        )
        safe = sanitize_diagnostics(
            page,
            [("BDUSS", secret_bduss), ("STOKEN", secret_stoken)],
        )
        serialized = repr(safe)
        self.assertIn("tieba.baidu.com/p/123", serialized)
        self.assertIn("see_lz=%5BREDACTED%5D", serialized)
        self.assertNotIn(secret_bduss, serialized)
        self.assertNotIn(secret_stoken, serialized)
        self.assertNotIn("body_text", safe)
        self.assertTrue(safe["contains_login"])
        self.assertTrue(safe["contains_verify"])
        self.assertEqual(safe["selector_counts"]["[data-id='[REDACTED]']"], 2)
        self.assertEqual(safe["first_floor_seen"], False)
        self.assertEqual(safe["selected_selector"], "")

    def test_diagnostics_presence_flags_are_broader_than_classifier_keywords(self):
        safe = sanitize_diagnostics(
            snapshot(
                title="帖子详情",
                body_text="打开客户端，验证 APP",
                selected_selector="[data-post-id]",
            ),
            [],
        )
        self.assertTrue(safe["contains_app"])
        self.assertTrue(safe["contains_verify"])
        self.assertEqual(safe["selected_selector"], "[data-post-id]")


class TiebaFailureArtifactTests(unittest.TestCase):
    def test_artifacts_are_timestamped_redacted_and_viewport_only(self):
        secret = "bduss&artifact-secret"

        class Page:
            screenshot_options = None

            async def content(self):
                return (
                    '<html><body>visible BDUSS=' + secret
                    + ' STOKEN="inline-token"</body></html>'
                    + '<!-- bduss%26artifact-secret bduss&amp;artifact-secret -->'
                )

            async def screenshot(self, *, path, full_page, timeout):
                self.screenshot_options = (full_page, timeout)
                Path(path).write_bytes(b"viewport-png")

        with tempfile.TemporaryDirectory() as directory:
            page = Page()
            result = asyncio.run(
                save_failure_artifacts(
                    page,
                    pairs=[("BDUSS", secret)],
                    directory=directory,
                )
            )
            self.assertEqual(result["status"], "ok")
            self.assertTrue(result["html_saved"])
            self.assertTrue(result["screenshot_saved"])
            html_path = Path(result["html_path"])
            screenshot_path = Path(result["screenshot_path"])
            self.assertRegex(html_path.name, r"^tieba_failure_\d{8}T\d{6}_\d+_[0-9a-f]{12}\.html$")
            content = html_path.read_text(encoding="utf-8")
            self.assertNotIn(secret, content)
            self.assertNotIn("bduss%26artifact-secret", content)
            self.assertNotIn("bduss&amp;artifact-secret", content)
            self.assertNotIn("inline-token", content)
            self.assertIn("BDUSS=[REDACTED]", content)
            self.assertTrue(screenshot_path.is_file())
            self.assertEqual(page.screenshot_options, (False, 2_350))

    def test_artifact_errors_are_returned_as_partial_status(self):
        class BrokenPage:
            async def content(self):
                raise RuntimeError("sensitive error detail")

            async def screenshot(self, **_kwargs):
                raise RuntimeError("another sensitive error")

        with tempfile.TemporaryDirectory() as directory:
            result = asyncio.run(
                save_failure_artifacts(BrokenPage(), pairs=[], directory=directory)
            )
        self.assertEqual(result["status"], "partial")
        self.assertFalse(result["html_saved"])
        self.assertFalse(result["screenshot_saved"])
        self.assertNotIn("sensitive", repr(result))


if __name__ == "__main__":
    unittest.main()
