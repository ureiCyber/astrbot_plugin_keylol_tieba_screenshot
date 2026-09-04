import asyncio
import unittest
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory

from bs4 import BeautifulSoup
from PIL import Image, ImageDraw

from keylol_page import (
    Article,
    DEFAULT_MOBILE_VIEWPORT_WIDTH,
    IPHONE_SAFARI_USER_AGENT,
    KeylolPageError,
    MOBILE_PAGE_PADDING,
    _download_keylol_page,
    _desktop_view_url,
    _inline_keylol_images,
    build_render_html,
    extract_keylol_thread_urls,
    mobile_viewport_height,
    normalize_keylol_url,
    normalize_mobile_viewport_width,
    parse_article,
    trim_rendered_screenshot,
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
        self.charset = "utf-8"

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False


class _FakeSession:
    def __init__(self, responses: list[_FakeResponse]):
        self.responses = list(responses)
        self.requests: list[tuple[str, dict[str, object]]] = []

    def get(self, url: str, **kwargs: object):
        self.requests.append((url, kwargs))
        return self.responses.pop(0)


def _tiny_png() -> bytes:
    buffer = BytesIO()
    image = Image.new("RGB", (2, 2), (12, 34, 56))
    image.save(buffer, format="PNG")
    image.close()
    return buffer.getvalue()


SAMPLE = """
<!doctype html><html><body>
  <a id="thread_subject">测试帖子</a>
  <a class="btn-user-action" href="member.php?mod=logging&amp;action=login">登录</a>
  <div id="post_123">
    <div class="pls"><div class="authi"><a class="xw1">作者甲</a></div></div>
    <em id="authorposton123"><span title="2026-08-09 18:44:01">刚刚</span></em>
    <a id="postnum123"><em>1</em>楼</a>
    <td class="t_f" id="postmessage_123">
      <div class="rnd_ai_pr"><img src="/ad.jpg"></div>
      <div class="original_text_style1">原创声明</div>
      <p onclick="steal()">正文 <a href="/thread-link">链接</a></p>
      <dl class="tattl">
        <dt><img src="static/image/filetype/image_s.gif"></dt>
        <dd>
          <p class="attnm"><a href="forum.php?mod=attachment&amp;aid=456">example.jpg</a></p>
          <p>1.2 MB，下载次数：3</p>
          <a href="forum.php?mod=attachment&amp;aid=456">下载附件</a>
          <img src="/static/image/common/none.gif"
               zoomfile="forum.php?mod=attachment&amp;aid=456"
               width="1920" height="1080" onerror="steal()">
        </dd>
      </dl>
      <script>alert(1)</script>
    </td>
    <div class="attach_nopermission">请登录</div>
  </div>
  <div id="post_124">
    <a id="postnum124"><em>2</em>楼</a>
    <td class="t_f" id="postmessage_124"><p>二楼回复不得出现</p></td>
  </div>
</body></html>
"""


class KeylolPageTests(unittest.TestCase):
    def test_parse_main_post_and_sanitize_dangerous_markup(self):
        article = parse_article(SAMPLE, "https://keylol.com/t123-1-1")

        self.assertEqual(article.title, "测试帖子")
        self.assertEqual(article.author, "作者甲")
        self.assertEqual(article.published_at, "2026-08-09 18:44:01")
        self.assertTrue(article.has_locked_resources)
        self.assertFalse(article.is_authenticated)
        self.assertIn("正文", article.body_html)
        self.assertIn("https://keylol.com/thread-link", article.body_html)
        self.assertIn("article-notice", article.body_html)
        self.assertIn(
            "https://keylol.com/forum.php?mod=attachment&amp;aid=456",
            article.body_html,
        )
        self.assertNotIn("static/image/common/none.gif", article.body_html)
        self.assertNotIn("static/image/filetype/image_s.gif", article.body_html)
        self.assertNotIn("下载附件", article.body_html)
        self.assertNotIn("example.jpg", article.body_html)
        self.assertNotIn("下载次数", article.body_html)
        self.assertNotIn('width="1920"', article.body_html)
        self.assertNotIn('height="1080"', article.body_html)
        self.assertNotIn("rnd_ai_pr", article.body_html)
        self.assertNotIn("onclick", article.body_html)
        self.assertNotIn("onerror", article.body_html)
        self.assertNotIn("<script", article.body_html)
        self.assertNotIn("二楼回复不得出现", article.body_html)

    def test_real_attachment_survives_attachimg_placeholder(self):
        attachimg_sample = SAMPLE.replace(
            "/static/image/common/none.gif",
            "/static/image/common/attachimg.gif",
        )

        article = parse_article(attachimg_sample, "https://keylol.com/t123-1-1")

        self.assertIn(
            "https://keylol.com/forum.php?mod=attachment&amp;aid=456",
            article.body_html,
        )
        self.assertNotIn("static/image/common/attachimg.gif", article.body_html)

    def test_image_only_attachment_is_not_mistaken_for_empty_body(self):
        page = """
        <a id="thread_subject">纯附件帖子</a>
        <div id="post_1">
          <a id="postnum1"><em>1</em>楼</a>
          <td id="postmessage_1">
            <ignore_js_op>
              <img src="/static/image/common/none.gif"
                   zoomfile="forum.php?mod=attachment&amp;aid=999"
                   file="forum.php?mod=attachment&amp;aid=999">
            </ignore_js_op>
          </td>
        </div>
        """

        article = parse_article(page, "https://keylol.com/t1-1-1")

        self.assertEqual(
            BeautifulSoup(article.body_html, "html.parser").get_text(" ", strip=True),
            "",
        )
        self.assertIn(
            "https://keylol.com/forum.php?mod=attachment&amp;aid=999",
            article.body_html,
        )

        session = _FakeSession([_FakeResponse(200, _tiny_png())])
        inlined = asyncio.run(
            _inline_keylol_images(
                article,
                session,
                cookie="sid=secret",
                proxy_url="",
                max_image_bytes=1024 * 1024,
                max_total_bytes=1024 * 1024,
            )
        )

        self.assertIn('src="data:image/png;base64,', inlined.body_html)
        self.assertEqual(inlined.unresolved_image_count, 0)

    def test_locked_only_post_is_reported_as_restricted_instead_of_empty(self):
        page = """
        <a id="thread_subject">仅受限附件</a>
        <div id="post_1">
          <a id="postnum1"><em>1</em>楼</a>
          <td id="postmessage_1"></td>
          <div class="attach_nopermission">登录后可见</div>
        </div>
        """

        article = parse_article(page, "https://keylol.com/t1-1-1")

        self.assertEqual(article.body_html, "")
        self.assertTrue(article.has_locked_resources)

    def test_logged_out_first_page_without_floor_link_uses_opening_post(self):
        page = """
        <a id="thread_subject">未登录第一页</a>
        <div id="post_10">
          <td id="postmessage_10"><p>公开正文</p></td>
        </div>
        <div id="post_new"><td id="postmessage_new">快速回复</td></div>
        """

        article = parse_article(page, "https://keylol.com/t10-1-1")

        self.assertIn("公开正文", article.body_html)
        self.assertNotIn("快速回复", article.body_html)

    def test_video_card_keeps_only_trusted_keylol_poster(self):
        page = """
        <a id="thread_subject">视频海报测试</a>
        <div id="post_1">
          <a id="postnum1"><em>1</em>楼</a>
          <td id="postmessage_1">
            <video poster="https://blob.keylol.com/poster/preview.jpg"
                   src="https://media.example.invalid/video.mp4"></video>
            <video poster="http://127.0.0.1/private.jpg"
                   src="http://127.0.0.1/private.mp4"></video>
          </td>
        </div>
        """

        article = parse_article(page, "https://keylol.com/t1-1-1")
        body = BeautifulSoup(article.body_html, "html.parser")

        self.assertEqual(len(body.select(".media-card")), 2)
        self.assertEqual(len(body.find_all("img")), 1)
        self.assertIn("https://blob.keylol.com/poster/preview.jpg", article.body_html)
        self.assertNotIn("127.0.0.1", article.body_html)
        self.assertNotIn("example.invalid", article.body_html)

    def test_removes_trailing_discuz_attachment_spacing(self):
        page = """
        <a id="thread_subject">空行测试</a>
        <div id="post_1">
          <a id="postnum1"><em>1</em>楼</a>
          <td id="postmessage_1">正文<br><br><br></td>
        </div>
        """

        article = parse_article(page, "https://keylol.com/t123-1-1")

        self.assertEqual(article.body_html, "正文")

    def test_external_lazy_attachment_prefers_real_source_and_drops_external_images(self):
        page = """
        <a id="thread_subject">附件测试</a>
        <div id="post_1">
          <a id="postnum1"><em>1</em>楼</a>
          <td id="postmessage_1">
            <p>正文</p><img src="http://127.0.0.1/private.png">
          </td>
          <div class="pattl"><dl class="tattl"><dd><p>
            <img zoomfile="/static/image/common/attachimg.gif"
                 data-actualsrc="/data/attachment/forum/real.png"
                 src="/static/image/common/none.gif">
          </p></dd></dl></div>
        </div>
        """

        article = parse_article(page, "https://keylol.com/t1-1-1")

        self.assertIn("附件与资源", article.body_html)
        self.assertIn(
            "https://keylol.com/data/attachment/forum/real.png",
            article.body_html,
        )
        self.assertNotIn("127.0.0.1", article.body_html)

    def test_blob_attachment_keeps_primary_download_and_falls_back_when_inlining(self):
        primary_url = "https://keylol.com/forum.php?mod=attachment&aid=987"
        blob_url = "https://blob.keylol.com/attachment/987/original.png"
        page = f"""
        <a id="thread_subject">Blob 附件测试</a>
        <div id="post_1">
          <a id="postnum1"><em>1</em>楼</a>
          <td id="postmessage_1">
            <p>正文</p>
            <ignore_js_op>
              <img src="{blob_url}" zoomfile="{blob_url}" file="{blob_url}">
              <a class="aimg_tip" href="{primary_url}">查看原图</a>
            </ignore_js_op>
          </td>
        </div>
        """

        article = parse_article(page, "https://keylol.com/t1048309-1-1")

        # The authenticated first-party attachment endpoint must survive the
        # sanitizing pass even though Discuz puts the visible image on blob.
        self.assertIn(primary_url.replace("&", "&amp;"), article.body_html)
        parsed_image = BeautifulSoup(article.body_html, "html.parser").find("img")
        self.assertIsNotNone(parsed_image)
        self.assertEqual(parsed_image.get("src"), primary_url)

        # A transient failure on the preferred endpoint should not make the
        # image disappear when a safe secondary candidate is available.
        session = _FakeSession(
            [_FakeResponse(404), _FakeResponse(200, _tiny_png())]
        )
        inlined = asyncio.run(
            _inline_keylol_images(
                article,
                session,
                cookie="sid=secret",
                proxy_url="",
                max_image_bytes=1024 * 1024,
                max_total_bytes=1024 * 1024,
            )
        )

        self.assertEqual(
            [request[0] for request in session.requests],
            [primary_url, blob_url],
        )
        self.assertEqual(session.requests[0][1]["headers"]["Cookie"], "sid=secret")
        self.assertNotIn("Cookie", session.requests[1][1]["headers"])
        self.assertIn('src="data:image/png;base64,', inlined.body_html)
        self.assertEqual(inlined.unresolved_image_count, 0)

    def test_html5_video_and_onexin_embed_become_visible_static_media_cards(self):
        page = """
        <a id="thread_subject">媒体嵌入测试</a>
        <div id="post_1">
          <a id="postnum1"><em>1</em>楼</a>
          <td id="postmessage_1">
            <p>正文前</p>
            <video controls src="https://cdn.example.invalid/watch.mp4">
              <source src="https://cdn.example.invalid/watch.mp4" type="video/mp4">
            </video>
            <iframe class="onexin-player"
                    src="https://www.onexin.com/embed/abc123"
                    allowfullscreen></iframe>
            <p>正文后</p>
          </td>
        </div>
        """

        article = parse_article(page, "https://keylol.com/t1048309-1-1")
        body = BeautifulSoup(article.body_html, "html.parser")

        self.assertEqual(
            body.get_text(" ", strip=True).count("打开原帖查看媒体"),
            2,
        )
        self.assertIsNone(body.find("video"))
        self.assertIsNone(body.find("source"))
        self.assertIsNone(body.find("iframe"))
        self.assertNotIn("cdn.example.invalid", article.body_html)
        self.assertNotIn("onexin.com", article.body_html)

    def test_video_file_attachment_becomes_visible_static_media_card(self):
        page = """
        <a id="thread_subject">视频附件测试</a>
        <div id="post_1">
          <a id="postnum1"><em>1</em>楼</a>
          <td id="postmessage_1"><p>正文</p></td>
          <div class="pattl">
            <dl class="tattl">
              <dt><img src="static/image/filetype/mp4.gif"></dt>
              <dd>
                <p class="attnm">
                  <a href="forum.php?mod=attachment&amp;aid=246">clip.mp4</a>
                </p>
                <p>12.4 MB，下载次数：8</p>
                <a href="forum.php?mod=attachment&amp;aid=246">下载附件</a>
              </dd>
            </dl>
          </div>
        </div>
        """

        article = parse_article(page, "https://keylol.com/t1048309-1-1")
        body = BeautifulSoup(article.body_html, "html.parser")

        self.assertIn("打开原帖查看媒体", body.get_text(" ", strip=True))
        self.assertTrue(
            any(
                "media" in " ".join(node.get("class", []))
                for node in body.find_all(True)
            )
        )
        self.assertNotIn("clip.mp4", article.body_html)
        self.assertNotIn("下载附件", article.body_html)
        self.assertNotIn("下载次数", article.body_html)

        inlined = asyncio.run(
            _inline_keylol_images(
                article,
                _FakeSession([]),
                cookie="sid=secret",
                proxy_url="",
                max_image_bytes=1024 * 1024,
                max_total_bytes=1024 * 1024,
            )
        )
        self.assertIn("打开原帖查看媒体", inlined.body_html)

    def test_inlines_binary_attachment_with_cookie_referer_and_proxy(self):
        article = Article(
            "标题",
            "作者",
            "时间",
            "https://keylol.com/t1-1-1",
            '<img src="https://img.keylol.com/forum.php?mod=attachment&amp;aid=1">',
            False,
            True,
        )
        session = _FakeSession(
            [_FakeResponse(200, _tiny_png(), {"Content-Type": "application/octet-stream"})]
        )

        result = asyncio.run(
            _inline_keylol_images(
                article,
                session,
                cookie="sid=secret",
                proxy_url="http://127.0.0.1:7890",
                max_image_bytes=1024 * 1024,
                max_total_bytes=1024 * 1024,
            )
        )

        self.assertIn('src="data:image/png;base64,', result.body_html)
        self.assertEqual(result.unresolved_image_count, 0)
        requested_url, options = session.requests[0]
        self.assertEqual(requested_url, "https://img.keylol.com/forum.php?mod=attachment&aid=1")
        self.assertNotIn("Cookie", options["headers"])
        self.assertEqual(options["headers"]["Referer"], article.source_url)
        self.assertIn("image/avif", options["headers"]["Accept"])
        self.assertEqual(options["proxy"], "http://127.0.0.1:7890")
        self.assertFalse(options["allow_redirects"])

    def test_sends_cookie_to_primary_keylol_attachment_host(self):
        article = Article(
            "标题",
            "作者",
            "时间",
            "https://keylol.com/t1-1-1",
            '<img src="https://keylol.com/forum.php?mod=attachment&amp;aid=1">',
            False,
            True,
        )
        session = _FakeSession([_FakeResponse(200, _tiny_png())])

        asyncio.run(
            _inline_keylol_images(
                article,
                session,
                cookie="sid=secret",
                proxy_url="",
                max_image_bytes=1024 * 1024,
                max_total_bytes=1024 * 1024,
            )
        )

        self.assertEqual(session.requests[0][1]["headers"]["Cookie"], "sid=secret")

    def test_stops_image_requests_when_total_download_budget_is_used(self):
        payload = _tiny_png()
        article = Article(
            "标题",
            "作者",
            "时间",
            "https://keylol.com/t1-1-1",
            "".join(
                f'<img src="https://keylol.com/data/{index}.png">'
                for index in range(3)
            ),
            False,
            True,
        )
        session = _FakeSession([_FakeResponse(200, payload) for _ in range(3)])

        result = asyncio.run(
            _inline_keylol_images(
                article,
                session,
                cookie="sid=secret",
                proxy_url="",
                max_image_bytes=1024 * 1024,
                max_total_bytes=len(payload),
            )
        )

        self.assertEqual(len(session.requests), 1)
        self.assertEqual(result.unresolved_image_count, 2)
        self.assertEqual(result.body_html.count("data:image/png;base64,"), 1)
        self.assertNotIn("https://keylol.com/data/1.png", result.body_html)

    def test_does_not_follow_attachment_redirect_outside_keylol(self):
        article = Article(
            "标题",
            "作者",
            "时间",
            "https://keylol.com/t1-1-1",
            '<img src="https://keylol.com/forum.php?mod=attachment&amp;aid=1">',
            False,
            True,
        )
        session = _FakeSession(
            [_FakeResponse(302, headers={"Location": "https://example.com/private.png"})]
        )

        result = asyncio.run(
            _inline_keylol_images(
                article,
                session,
                cookie="sid=secret",
                proxy_url="",
                max_image_bytes=1024 * 1024,
                max_total_bytes=1024 * 1024,
            )
        )

        self.assertEqual(len(session.requests), 1)
        self.assertNotIn("data:image/", result.body_html)
        self.assertNotIn("forum.php", result.body_html)
        self.assertEqual(result.unresolved_image_count, 1)

    def test_does_not_send_cookie_on_attachment_https_downgrade(self):
        article = Article(
            "标题",
            "作者",
            "时间",
            "https://keylol.com/t1-1-1",
            '<img src="https://keylol.com/forum.php?mod=attachment&amp;aid=1">',
            False,
            True,
        )
        session = _FakeSession(
            [_FakeResponse(302, headers={"Location": "http://img.keylol.com/private.png"})]
        )

        result = asyncio.run(
            _inline_keylol_images(
                article,
                session,
                cookie="sid=secret",
                proxy_url="",
                max_image_bytes=1024 * 1024,
                max_total_bytes=1024 * 1024,
            )
        )

        self.assertEqual(len(session.requests), 1)
        self.assertEqual(result.unresolved_image_count, 1)

    def test_build_render_document_contains_source_and_access_note(self):
        article = Article(
            "标题",
            "作者",
            "时间",
            "https://keylol.com/t1-1-1",
            "<p>正文</p>",
            True,
            True,
        )
        document = build_render_html(article, content_width=390)

        self.assertIn("content=\"width=390, height=844, initial-scale=1\"", document)
        self.assertIn("width: 390px", document)
        self.assertIn("background: #fff", document)
        self.assertIn("padding: 8px 16px 20px", document)
        self.assertIn("font: 17px/1.7", document)
        self.assertIn("登录后才能查看", document)
        self.assertIn("https://keylol.com/t1-1-1", document)
        self.assertIn(
            ".article img { max-width: 100%; width: auto; height: auto; }",
            document,
        )
        self.assertNotIn('class="card"', document)
        self.assertNotIn("clip-path:", document)
        self.assertNotIn("border-radius:", document)

    def test_build_render_document_reports_unresolved_images(self):
        article = Article(
            "标题",
            "作者",
            "时间",
            "https://keylol.com/t1-1-1",
            "<p>正文</p>",
            False,
            True,
            2,
        )

        document = build_render_html(article)

        self.assertIn("有 2 张站内图片下载失败", document)

    def test_mobile_viewport_width_is_clamped_to_iphone_range(self):
        self.assertEqual(normalize_mobile_viewport_width(200), 320)
        self.assertEqual(normalize_mobile_viewport_width(960), 440)
        self.assertEqual(
            normalize_mobile_viewport_width("not-a-number"),
            DEFAULT_MOBILE_VIEWPORT_WIDTH,
        )
        self.assertEqual(mobile_viewport_height(390), 844)

    def test_reject_non_keylol_hosts(self):
        with self.assertRaises(KeylolPageError):
            normalize_keylol_url("https://example.com/t1-1-1")

    def test_upgrades_keylol_links_to_https(self):
        self.assertEqual(
            normalize_keylol_url("http://keylol.com/t1-1-1"),
            "https://keylol.com/t1-1-1",
        )

    def test_iphone_ua_requests_complete_discuz_view(self):
        self.assertIn("iPhone", IPHONE_SAFARI_USER_AGENT)
        self.assertEqual(
            _desktop_view_url("https://keylol.com/t1-1-1?foo=1&mobile=2#post"),
            "https://keylol.com/t1-1-1?foo=1&mobile=no",
        )

    def test_page_fetch_does_not_follow_external_redirect_with_cookie(self):
        session = _FakeSession(
            [_FakeResponse(302, headers={"Location": "https://example.com/login"})]
        )

        with self.assertRaises(KeylolPageError):
            asyncio.run(
                _download_keylol_page(
                    session,
                    "https://keylol.com/t1-1-1",
                    cookie="sid=secret",
                    proxy_url="",
                    max_html_bytes=1024,
                )
            )

        self.assertEqual(len(session.requests), 1)
        self.assertEqual(
            session.requests[0][0],
            "https://keylol.com/t1-1-1?mobile=no",
        )
        self.assertEqual(session.requests[0][1]["headers"]["Cookie"], "sid=secret")

    def test_finds_first_floor_even_when_second_floor_appears_first(self):
        reversed_page = """
        <a id="thread_subject">倒序页面</a>
        <div id="post_2"><a id="postnum2"><em>2</em>楼</a><td id="postmessage_2">二楼</td></div>
        <div id="post_1"><a id="postnum1"><em>1</em>楼</a><td id="postmessage_1">一楼正文</td></div>
        """
        article = parse_article(reversed_page, "https://keylol.com/t1-1-1")
        self.assertIn("一楼正文", article.body_html)
        self.assertNotIn("二楼", article.body_html)

    def test_rejects_page_without_first_floor(self):
        page_two = """
        <div id="post_31"><a id="postnum31"><em>31</em>楼</a><td id="postmessage_31">回复</td></div>
        """
        with self.assertRaises(KeylolPageError):
            parse_article(page_two, "https://keylol.com/t1-2-1")

    def test_surfaces_forum_membership_denial_before_floor_parsing(self):
        denied_page = """
        <div id="main_message">
          <div id="messagetext"><p></p></div>
          <div><p>浏览本版块需要初阶会员或更高等级</p></div>
        </div>
        """

        with self.assertRaisesRegex(KeylolPageError, "需要初阶会员"):
            parse_article(denied_page, "https://keylol.com/t1-1-1")

    def test_extracts_group_message_thread_links(self):
        message = (
            "看看这个：https://keylol.com/t1046223-1-1，重复链接 "
            "https://keylol.com/t1046223-1-1 以及 keylol.com/t123-1-1。"
        )
        self.assertEqual(
            extract_keylol_thread_urls(message),
            [
                "https://keylol.com/t1046223-1-1",
                "https://keylol.com/t123-1-1",
            ],
        )

    def test_does_not_match_lookalike_domains(self):
        self.assertEqual(
            extract_keylol_thread_urls("https://evilkeylol.com/t1046223-1-1"),
            [],
        )

    def test_trims_only_bottom_viewport_background(self):
        with TemporaryDirectory() as directory:
            image_path = Path(directory) / "short-page.png"
            image = Image.new("RGB", (100, 400), (238, 241, 245))
            draw = ImageDraw.Draw(image)
            draw.rectangle((10, 10, 89, 149), fill=(255, 255, 255))
            image.save(image_path)
            image.close()

            self.assertTrue(trim_rendered_screenshot(image_path))
            with Image.open(image_path) as trimmed:
                self.assertEqual(trimmed.size, (100, 150 + MOBILE_PAGE_PADDING))
                self.assertEqual(trimmed.getpixel((10, 10)), (255, 255, 255))

    def test_does_not_trim_content_reaching_image_bottom(self):
        with TemporaryDirectory() as directory:
            image_path = Path(directory) / "long-page.png"
            image = Image.new("RGB", (100, 200), (238, 241, 245))
            draw = ImageDraw.Draw(image)
            draw.rectangle((10, 10, 89, 199), fill=(255, 255, 255))
            image.save(image_path)
            image.close()

            self.assertFalse(trim_rendered_screenshot(image_path))
            with Image.open(image_path) as unchanged:
                self.assertEqual(unchanged.size, (100, 200))

if __name__ == "__main__":
    unittest.main()
