import asyncio
import unittest
from unittest.mock import patch

from tieba_page import (
    Article,
    _MAX_IMAGE_REDIRECTS,
    _inline_tieba_images,
    TiebaPageError,
    _signed_form,
    account_name_from_userinfo,
    article_from_api,
    extract_tieba_thread_urls,
    normalize_tieba_url,
    parse_tieba_article,
    parse_tieba_cookie,
)


class _FakeContent:
    def __init__(self, payload: bytes):
        self.payload = payload

    async def iter_chunked(self, _size: int):
        yield self.payload


class _FakeResponse:
    def __init__(
        self,
        status: int,
        payload: bytes = b"",
        headers: dict[str, str] | None = None,
    ):
        self.status = status
        self.headers = headers or {}
        self.content = _FakeContent(payload)

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False


class _FakeSession:
    def __init__(self, responses: list[_FakeResponse]):
        self.responses = list(responses)
        self.requests: list[tuple[str, dict[str, object]]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False

    def get(self, url: str, **kwargs: object):
        self.requests.append((url, kwargs))
        return self.responses.pop(0)


def _tiny_png() -> bytes:
    return b"\x89PNG\r\n\x1a\n"


def _image_article(source: str) -> Article:
    return Article(
        "标题",
        "作者",
        "时间",
        "https://tieba.baidu.com/p/123",
        f'<img src="{source}">',
        False,
        True,
    )


SAMPLE = """
<!doctype html><html><head>
  <meta fname="测试">
  <title>备用标题【测试吧】_百度贴吧</title>
</head><body>
  <h3 class="core_title_txt" title="测试贴吧帖子">忽略的文本</h3>
  <div class="l_post j_l_post" data-field='{
    "author":{"user_name":"账号甲","user_nickname":"昵称甲"},
    "content":{"post_no":2}
  }'>
    <div class="j_d_post_content">二楼回复不得出现</div>
  </div>
  <div class="l_post j_l_post" data-field='{
    "author":{"user_name":"账号乙","user_nickname":"昵称乙"},
    "content":{"post_no":1}
  }'>
    <a class="p_author_name">楼主甲</a>
    <div class="j_d_post_content">
      第一行<br>
      <div class="replace_div">
        <img src="//tiebapic.baidu.com/forum/test.jpg" onerror="steal()">
        <div class="replace_tip">点击展开，查看完整图片</div>
      </div>
      <a href="/p/123?pid=456" onclick="steal()">站内链接</a>
      <a href="javascript:alert(1)">危险链接</a>
      <script>alert(1)</script>
    </div>
    <div class="post-tail-wrap">
      <span class="tail-info">1楼</span>
      <span class="tail-info">2026-08-13 20:30</span>
    </div>
  </div>
</body></html>
"""

API_SAMPLE = {
    "error_code": "0",
    "thread": {
        "title": "接口测试帖",
        "author": {"name_show": "楼主乙"},
    },
    "forum": {"name": "接口测试"},
    "post_list": [
        {
            "floor": 2,
            "author_id": 22,
            "time": 1_700_000_001,
            "content": [{"type": 0, "text": "二楼不得出现"}],
        },
        {
            "floor": 1,
            "author_id": 11,
            "time": 1_700_000_000,
            "content": [
                {"type": 0, "text": "第一行\n"},
                {
                    "type": 3,
                    "cdn_src": "https://imgsa.baidu.com/small.jpg",
                    "big_cdn_src": "https://imgsa.baidu.com/big.jpg",
                    "origin_src": "https://imgsa.baidu.com/origin.jpg",
                },
                {"type": 1, "text": "站内链接", "link": "/p/123"},
                {"type": 5, "src": "https://imgsa.baidu.com/cover.jpg"},
            ],
        },
    ],
    "user_list": [{"id": 11, "name_show": "备用作者"}],
}


class TiebaPageTests(unittest.TestCase):
    def test_normalizes_share_url_to_canonical_thread_url(self):
        self.assertEqual(
            normalize_tieba_url(
                "https://tieba.baidu.com/p/10937213244?share_from=qq"
            ),
            "https://tieba.baidu.com/p/10937213244",
        )

    def test_rejects_lookalike_domain(self):
        with self.assertRaises(TiebaPageError):
            normalize_tieba_url("https://evil-tieba.baidu.com/p/10937213244")

    def test_extracts_plain_and_qq_json_card_urls(self):
        card = {
            "app": "com.tencent.tuwen.lua",
            "meta": {
                "news": {
                    "jumpUrl": (
                        "https://tieba.baidu.com/p/10937213244?share_from=qq"
                    ),
                    "preview": "https://pic.ugcimg.cn/preview/jpg1",
                }
            },
        }
        urls = extract_tieba_thread_urls(
            "另一个链接 tieba.baidu.com/p/10930525581。",
            card,
        )
        self.assertEqual(
            urls,
            [
                "https://tieba.baidu.com/p/10930525581",
                "https://tieba.baidu.com/p/10937213244",
            ],
        )

    def test_decodes_cq_html_entities_in_card_payload(self):
        raw_card = (
            '{"jumpUrl":"https://tieba.baidu.com/p/10937213244'
            '?share_from=qq"&#44;"title":"测试"}'
        )
        self.assertEqual(
            extract_tieba_thread_urls(raw_card),
            ["https://tieba.baidu.com/p/10937213244"],
        )

    def test_parses_login_identifiers_without_logging_full_cookie(self):
        credentials = parse_tieba_cookie(
            "BAIDUID=ignored; BDUSS=abc==; STOKEN=token-value; OTHER=x"
        )
        self.assertEqual(credentials.bduss, "abc==")
        self.assertEqual(credentials.stoken, "token-value")

    def test_client_api_signature_is_deterministic(self):
        form = _signed_form(
            {
                "_client_type": "2",
                "_client_version": "12.64.1.1",
                "kz": "10937213244",
                "pn": "1",
                "rn": "2",
                "with_floor": "1",
            }
        )
        self.assertEqual(form["sign"], "d1eafe028fbc4606d8836ab00f69f286")

    def test_builds_article_from_client_json_floor_one(self):
        article = article_from_api(API_SAMPLE, "https://tieba.baidu.com/p/123")

        self.assertEqual(article.title, "接口测试帖")
        self.assertEqual(article.author, "楼主乙 · 接口测试吧")
        self.assertEqual(article.published_at, "2023-11-15 06:13:20")
        self.assertIn("第一行<br>", article.body_html)
        self.assertIn("https://imgsa.baidu.com/origin.jpg", article.body_html)
        self.assertIn("https://tieba.baidu.com/p/123", article.body_html)
        self.assertIn("视频内容，请打开原帖查看", article.body_html)
        self.assertNotIn("二楼不得出现", article.body_html)

    def test_rejects_client_api_error(self):
        with self.assertRaises(TiebaPageError):
            article_from_api(
                {"error_code": "4", "error_msg": "帖子不存在"},
                "https://tieba.baidu.com/p/123",
            )

    def test_rejects_client_api_without_floor_one(self):
        with self.assertRaises(TiebaPageError):
            article_from_api(
                {
                    "error_code": 0,
                    "thread": {"title": "缺失主楼"},
                    "post_list": [{"floor": 2, "content": []}],
                },
                "https://tieba.baidu.com/p/123",
            )

    def test_parses_only_floor_one_and_sanitizes_markup(self):
        article = parse_tieba_article(
            SAMPLE,
            "https://tieba.baidu.com/p/123?share_from=qq",
        )

        self.assertEqual(article.title, "测试贴吧帖子")
        self.assertEqual(article.author, "楼主甲 · 测试吧")
        self.assertEqual(article.published_at, "2026-08-13 20:30")
        self.assertEqual(article.source_url, "https://tieba.baidu.com/p/123")
        self.assertIn("第一行", article.body_html)
        self.assertIn(
            "https://tiebapic.baidu.com/forum/test.jpg",
            article.body_html,
        )
        self.assertIn("https://tieba.baidu.com/p/123?pid=456", article.body_html)
        self.assertIn("危险链接", article.body_html)
        self.assertNotIn("二楼回复不得出现", article.body_html)
        self.assertNotIn("点击展开", article.body_html)
        self.assertNotIn("javascript:", article.body_html)
        self.assertNotIn("onclick", article.body_html)
        self.assertNotIn("onerror", article.body_html)
        self.assertNotIn("<script", article.body_html)

    def test_falls_back_to_tail_marker_when_data_field_is_missing(self):
        page = """
        <h3 class="core_title_txt">旧版页面</h3>
        <div class="l_post">
          <div class="j_d_post_content">一楼正文</div>
          <div class="post-tail-wrap"><span class="tail-info">1楼</span></div>
        </div>
        """
        article = parse_tieba_article(page, "https://tieba.baidu.com/p/123")
        self.assertIn("一楼正文", article.body_html)

    def test_rejects_page_without_floor_one(self):
        page = """
        <div class="l_post" data-field='{"content":{"post_no":2}}'>
          <div class="j_d_post_content">二楼</div>
        </div>
        """
        with self.assertRaises(TiebaPageError):
            parse_tieba_article(page, "https://tieba.baidu.com/p/123")

    def test_extracts_logged_in_account_name(self):
        self.assertEqual(
            account_name_from_userinfo(
                {"error_code": "0", "user": {"name": "账号甲"}}
            ),
            "账号甲",
        )

    def test_extracts_account_name_from_nested_user(self):
        self.assertEqual(
            account_name_from_userinfo(
                {"error_code": 0, "data": {"user": {"name_show": "昵称乙"}}}
            ),
            "昵称乙",
        )

    def test_rejects_logged_out_account_response(self):
        with self.assertRaises(TiebaPageError):
            account_name_from_userinfo({"error_code": "2", "error_msg": "未登录"})

    def test_redirect_from_tieba_page_to_image_host_strips_cookie(self):
        session = _FakeSession(
            [
                _FakeResponse(
                    302,
                    headers={"Location": "https://imgsa.baidu.com/final.png"},
                ),
                _FakeResponse(
                    200,
                    _tiny_png(),
                    headers={"Content-Type": "image/png"},
                ),
            ]
        )

        with patch("tieba_page.aiohttp.ClientSession", return_value=session):
            result = asyncio.run(
                _inline_tieba_images(
                    _image_article("https://tieba.baidu.com/image.png"),
                    cookie="BAIDUID=unneeded; BDUSS=secret; STOKEN=token; OTHER=x",
                    timeout_seconds=5,
                )
            )

        self.assertIn("data:image/png;base64,", result.body_html)
        self.assertEqual(
            session.requests[0][1]["headers"]["Cookie"],
            "BDUSS=secret; STOKEN=token",
        )
        self.assertNotIn("Cookie", session.requests[1][1]["headers"])
        self.assertTrue(
            all(
                not request[1]["allow_redirects"]
                for request in session.requests
            )
        )

    def test_blocks_redirect_to_third_party_before_request(self):
        session = _FakeSession(
            [
                _FakeResponse(
                    302,
                    headers={"Location": "https://example.com/private.png"},
                )
            ]
        )

        with patch("tieba_page.aiohttp.ClientSession", return_value=session):
            result = asyncio.run(
                _inline_tieba_images(
                    _image_article("https://tieba.baidu.com/image.png"),
                    cookie="BDUSS=secret; STOKEN=token",
                    timeout_seconds=5,
                )
            )

        self.assertEqual(len(session.requests), 1)
        self.assertNotIn("data:image/", result.body_html)

    def test_stops_redirect_loop_at_maximum(self):
        session = _FakeSession(
            [
                _FakeResponse(
                    302,
                    headers={"Location": "https://imgsa.baidu.com/loop.png"},
                )
                for _ in range(_MAX_IMAGE_REDIRECTS + 1)
            ]
        )

        with patch("tieba_page.aiohttp.ClientSession", return_value=session):
            result = asyncio.run(
                _inline_tieba_images(
                    _image_article("https://imgsa.baidu.com/loop.png"),
                    cookie="BDUSS=secret; STOKEN=token",
                    timeout_seconds=5,
                )
            )

        self.assertEqual(len(session.requests), _MAX_IMAGE_REDIRECTS + 1)
        self.assertNotIn("data:image/", result.body_html)
        self.assertTrue(
            all(
                not request[1]["allow_redirects"]
                for request in session.requests
            )
        )

    def test_first_image_host_request_does_not_send_cookie(self):
        session = _FakeSession(
            [
                _FakeResponse(
                    200,
                    _tiny_png(),
                    headers={"Content-Type": "image/png"},
                )
            ]
        )

        with patch(
            "tieba_page.aiohttp.ClientSession", return_value=session
        ) as session_factory:
            result = asyncio.run(
                _inline_tieba_images(
                    _image_article("https://imgsa.baidu.com/start.png"),
                    cookie="BDUSS=secret; STOKEN=token",
                    timeout_seconds=5,
                )
            )

        self.assertIn("data:image/png;base64,", result.body_html)
        self.assertNotIn("Cookie", session.requests[0][1]["headers"])
        self.assertEqual(
            type(session_factory.call_args.kwargs["cookie_jar"]).__name__,
            "DummyCookieJar",
        )

    def test_avif_image_is_embedded_when_advertised_as_supported(self):
        session = _FakeSession(
            [
                _FakeResponse(
                    200,
                    b"synthetic-avif",
                    headers={"Content-Type": "image/avif"},
                )
            ]
        )

        with patch("tieba_page.aiohttp.ClientSession", return_value=session):
            result = asyncio.run(
                _inline_tieba_images(
                    _image_article("https://imgsa.baidu.com/start.avif"),
                    cookie="BDUSS=secret; STOKEN=token",
                    timeout_seconds=5,
                )
            )

        self.assertIn("data:image/avif;base64,", result.body_html)
        self.assertNotIn("Cookie", session.requests[0][1]["headers"])

    def test_blocks_https_to_http_image_redirect(self):
        session = _FakeSession(
            [
                _FakeResponse(
                    302,
                    headers={"Location": "http://imgsa.baidu.com/plain.png"},
                )
            ]
        )

        with patch("tieba_page.aiohttp.ClientSession", return_value=session):
            result = asyncio.run(
                _inline_tieba_images(
                    _image_article("https://tieba.baidu.com/image.png"),
                    cookie="BDUSS=secret; STOKEN=token",
                    timeout_seconds=5,
                )
            )

        self.assertEqual(len(session.requests), 1)
        self.assertNotIn("data:image/", result.body_html)


if __name__ == "__main__":
    unittest.main()
