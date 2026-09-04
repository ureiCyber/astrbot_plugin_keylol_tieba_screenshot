from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import json
import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse

import aiohttp
from bs4 import BeautifulSoup, Tag

try:
    from .keylol_page import Article, IPHONE_SAFARI_USER_AGENT
except ImportError:  # Allows direct test execution from the plugin directory.
    from keylol_page import Article, IPHONE_SAFARI_USER_AGENT


ALLOWED_PAGE_HOSTS = {"tieba.baidu.com", "www.tieba.baidu.com"}
_THREAD_LINK_RE = re.compile(
    r"(?<![\w.-])(?:https?://)?(?:www\.)?tieba\.baidu\.com/p/(\d+)"
    r"(?:[/?#][^\s<>\"']*)?",
    re.IGNORECASE,
)
_URL_TRAILING_PUNCTUATION = ".,!?;:，。！？；：、)]}>"
# Keep this list in sync with ``tieba_browser.ALLOWED_TIEBA_STATIC_HOSTS``.
# Post content is user-controlled, so image URLs must use an explicit
# first-party allowlist before they are left in HTML or downloaded.
_ALLOWED_IMAGE_HOSTS = {
    "tieba.baidu.com",
    "www.tieba.baidu.com",
    "tb1.bdstatic.com",
    "tb2.bdstatic.com",
    "tb3.bdstatic.com",
    "tb4.bdstatic.com",
    "tb5.bdstatic.com",
    "tb6.bdstatic.com",
    "tbpic.bdimg.com",
    "imgsrc.baidu.com",
    "imgsa.baidu.com",
    "img0.baidu.com",
    "img1.baidu.com",
    "img2.baidu.com",
    "img3.baidu.com",
    "tiebapic.baidu.com",
    "himg.bdimg.com",
    "bdstatic.com",
}
_MAX_PAGE_BYTES = 8 * 1024 * 1024
_IMAGE_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_MAX_IMAGE_REDIRECTS = 5
_THREAD_API_URL = "https://c.tieba.baidu.com/c/f/pb/page"
_LOGIN_API_URL = "https://c.tieba.baidu.com/c/s/login"
_THREAD_CLIENT_VERSION = "12.64.1.1"
_LOGIN_CLIENT_VERSION = "22.5.1.0"
_SIGN_SECRET = "tiebaclient!!!"
_BEIJING_TIME = timezone(timedelta(hours=8))
_SAFE_TAGS = {
    "a",
    "b",
    "blockquote",
    "br",
    "code",
    "div",
    "em",
    "figcaption",
    "figure",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "i",
    "img",
    "li",
    "ol",
    "p",
    "pre",
    "s",
    "span",
    "strong",
    "table",
    "tbody",
    "td",
    "th",
    "thead",
    "tr",
    "u",
    "ul",
}
_DROP_TAGS = {
    "audio",
    "button",
    "canvas",
    "embed",
    "form",
    "iframe",
    "input",
    "noscript",
    "object",
    "script",
    "style",
    "svg",
    "video",
}
_SAFE_ATTRS = {
    "a": {"href", "title"},
    "img": {"alt", "src", "title"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan"},
}
_TIME_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?\b")
_APP_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/x-www-form-urlencoded",
    "User-Agent": "bdtb for Android 12.64.1.1",
}


class TiebaPageError(RuntimeError):
    """An error that is safe to show to the command caller."""


@dataclass(frozen=True, slots=True)
class TiebaCredentials:
    bduss: str
    stoken: str


def parse_tieba_cookie(cookie: str) -> TiebaCredentials:
    """Validate a browser Cookie header and expose its login identifiers."""
    value = cookie.strip()
    if "\r" in value or "\n" in value:
        raise TiebaPageError("Cookie 配置格式无效。")

    fields: dict[str, str] = {}
    for part in value.split(";"):
        name, separator, field_value = part.strip().partition("=")
        if separator and name:
            fields[name.upper()] = field_value.strip()

    return TiebaCredentials(
        bduss=fields.get("BDUSS", ""),
        stoken=fields.get("STOKEN", ""),
    )


def normalize_tieba_url(raw_url: str) -> str:
    value = html.unescape(raw_url).strip().strip("<>")
    if not value:
        raise TiebaPageError("请提供百度贴吧帖子链接。")
    if "://" not in value:
        value = f"https://{value}"

    parsed = urlparse(value)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme not in {"http", "https"} or host not in ALLOWED_PAGE_HOSTS:
        raise TiebaPageError("仅支持 tieba.baidu.com 的 http/https 帖子链接。")
    if parsed.username or parsed.password or parsed.port:
        raise TiebaPageError("链接中不能包含账号、密码或自定义端口。")

    match = re.fullmatch(r"/p/(\d+)/?", parsed.path)
    if not match:
        raise TiebaPageError("贴吧帖子链接格式无效。")
    return f"https://tieba.baidu.com/p/{match.group(1)}"


def _walk_string_values(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield html.unescape(value)
    elif isinstance(value, dict):
        for child in value.values():
            yield from _walk_string_values(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _walk_string_values(child)


def extract_tieba_thread_urls(*sources: Any) -> list[str]:
    """Extract unique Tieba thread URLs from text or a QQ JSON card payload."""
    urls: list[str] = []
    seen: set[str] = set()
    for source in sources:
        for text in _walk_string_values(source):
            for match in _THREAD_LINK_RE.finditer(text):
                candidate = match.group(0).rstrip(_URL_TRAILING_PUNCTUATION)
                try:
                    normalized = normalize_tieba_url(candidate)
                except TiebaPageError:
                    continue
                if normalized in seen:
                    continue
                seen.add(normalized)
                urls.append(normalized)
    return urls


def _absolute_http_url(value: str, base_url: str) -> str | None:
    try:
        absolute = urljoin(base_url, value.strip())
        parsed = urlparse(absolute)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    return absolute


def _safe_tieba_image_url(value: str, base_url: str) -> str | None:
    """Resolve an image URL only when it is a first-party HTTPS asset.

    This check is deliberately stricter than the generic link resolver.  A
    post can contain arbitrary user-provided image URLs, and leaving one in
    the rendered HTML would make the local renderer contact an external or
    private host.
    """

    absolute = _absolute_http_url(value, base_url)
    if not absolute:
        return None
    try:
        parsed = urlparse(absolute)
        port = parsed.port
    except ValueError:
        return None
    host = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme.lower() != "https"
        or host not in _ALLOWED_IMAGE_HOSTS
        or parsed.username
        or parsed.password
        or port is not None
    ):
        return None
    return absolute


def _decode_post_field(post: Tag) -> dict[str, Any]:
    raw = str(post.get("data-field", "")).strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _find_main_post(soup: BeautifulSoup) -> tuple[Tag, dict[str, Any]]:
    posts = soup.select("div.l_post, div.j_l_post")
    for post in posts:
        fields = _decode_post_field(post)
        content = fields.get("content")
        if isinstance(content, dict):
            try:
                if int(content.get("post_no") or 0) == 1:
                    return post, fields
            except (TypeError, ValueError):
                pass

    # A few older pages omit data-field but still mark the first floor in the tail.
    for post in posts:
        has_floor_one = any(
            node.get_text(" ", strip=True) == "1楼"
            for node in post.select(".tail-info")
        )
        if has_floor_one:
            return post, _decode_post_field(post)
    raise TiebaPageError("未能从贴吧页面读取到帖子主楼。")


def _clean_main_post(content: Tag, source_url: str) -> str:
    fragment = BeautifulSoup(str(content), "html.parser")
    root = fragment.find()
    if root is None:
        raise TiebaPageError("贴吧帖子主楼正文为空。")

    for selector in (".replace_tip", ".p_forbidden_tip", ".user-hide-post-down"):
        for node in root.select(selector):
            node.decompose()

    for tag in list(root.find_all(True)):
        if tag.name in _DROP_TAGS:
            marker = ""
            if tag.name in {"audio", "video"}:
                marker = (
                    "[音视频内容，请打开原帖查看]"
                    if tag.name == "video"
                    else "[语音内容，请打开原帖收听]"
                )
            if marker:
                tag.replace_with(marker)
            else:
                tag.decompose()
            continue
        if tag.name not in _SAFE_TAGS:
            tag.unwrap()
            continue

        image_source = ""
        if tag.name == "img":
            for attr in ("data-original", "data-src", "data-lazy-src", "src"):
                candidate = str(tag.get(attr, "")).strip()
                if candidate:
                    image_source = candidate
                    break

        allowed = _SAFE_ATTRS.get(tag.name, set())
        for attr in list(tag.attrs):
            if attr not in allowed:
                del tag.attrs[attr]

        if tag.name == "a":
            href = _absolute_http_url(str(tag.get("href", "")), source_url)
            if href:
                tag["href"] = href
                tag["rel"] = "noopener noreferrer"
            else:
                tag.attrs.pop("href", None)
        elif tag.name == "img":
            src = _safe_tieba_image_url(image_source, source_url)
            if not src:
                tag.decompose()
                continue
            tag["src"] = src
            tag["loading"] = "eager"
            tag["referrerpolicy"] = "no-referrer"

    rendered = root.decode_contents().strip()
    probe = BeautifulSoup(rendered, "html.parser")
    if not probe.get_text(" ", strip=True) and not probe.find("img"):
        raise TiebaPageError("贴吧帖子主楼正文为空。")
    return rendered


def _page_title(soup: BeautifulSoup) -> str:
    title_node = soup.select_one(".core_title_txt")
    if title_node is not None:
        title = str(
            title_node.get("title") or title_node.get_text(" ", strip=True)
        ).strip()
        if title:
            return title
    if soup.title:
        title = soup.title.get_text(" ", strip=True)
        return re.sub(r"【[^】]*吧】_百度贴吧\s*$", "", title).strip()
    return "百度贴吧帖子"


def parse_tieba_article(page_html: str, source_url: str) -> Article:
    """Parse and sanitize only floor one from a Tieba desktop page."""
    canonical_url = normalize_tieba_url(source_url)
    soup = BeautifulSoup(page_html, "html.parser")
    main_post, fields = _find_main_post(soup)
    content = main_post.select_one(".j_d_post_content, .d_post_content")
    if content is None:
        raise TiebaPageError("未能从贴吧页面读取到帖子主楼正文。")

    body_html = _clean_main_post(content, canonical_url)
    author_fields = fields.get("author")
    author_fields = author_fields if isinstance(author_fields, dict) else {}
    author_node = main_post.select_one(".p_author_name, .d_name a")
    author = (
        str(author_node.get_text(" ", strip=True))
        if author_node is not None
        else str(
            author_fields.get("name_show")
            or author_fields.get("user_nickname")
            or author_fields.get("user_name")
            or ""
        ).strip()
    )

    forum_meta = soup.find("meta", attrs={"fname": True})
    forum_name = str(forum_meta.get("fname", "")).strip() if forum_meta else ""
    if forum_name:
        forum_label = forum_name if forum_name.endswith("吧") else f"{forum_name}吧"
        author = f"{author} · {forum_label}" if author else forum_label

    published_at = ""
    tail = main_post.select_one(".post-tail-wrap")
    if tail is not None:
        match = _TIME_RE.search(tail.get_text(" ", strip=True))
        if match:
            published_at = match.group(0)

    return Article(
        title=_page_title(soup),
        author=author,
        published_at=published_at,
        source_url=canonical_url,
        body_html=body_html,
        has_locked_resources=False,
        is_authenticated=True,
    )


def _signed_form(fields: dict[str, Any]) -> dict[str, str]:
    form = {key: str(value) for key, value in fields.items() if value is not None}
    unsigned = "".join(f"{key}={form[key]}" for key in sorted(form))
    form["sign"] = hashlib.md5(
        f"{unsigned}{_SIGN_SECRET}".encode("utf-8")
    ).hexdigest()
    return form


def _api_error(payload: Any) -> str:
    if not isinstance(payload, dict):
        return "贴吧接口返回了无效数据。"
    code = payload.get("error_code", payload.get("no", 0))
    if str(code) in {"0", "None"}:
        return ""
    message = (
        payload.get("error_msg")
        or payload.get("error")
        or payload.get("errmsg")
        or payload.get("msg")
        or "未知错误"
    )
    return f"贴吧接口返回错误 {code}：{message}"


def _api_user_map(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    users = payload.get("user_list")
    if not isinstance(users, list):
        return {}
    return {
        str(user.get("id")): user
        for user in users
        if isinstance(user, dict) and user.get("id") is not None
    }


def _api_text(value: Any) -> str:
    return html.escape(str(value or "")).replace("\n", "<br>")


def _api_fragment_html(fragment: Any) -> str:
    if not isinstance(fragment, dict):
        return ""
    try:
        kind = int(fragment.get("type") or 0)
    except (TypeError, ValueError):
        kind = 0

    if kind in {3, 20}:
        source = _safe_tieba_image_url(
            str(
                fragment.get("origin_src")
                or fragment.get("big_cdn_src")
                or fragment.get("cdn_src")
                or fragment.get("src")
                or ""
            ),
            "https://tieba.baidu.com/",
        )
        if not source:
            return ""
        return (
            f'<img src="{html.escape(source, quote=True)}" '
            'loading="eager" referrerpolicy="no-referrer">'
        )

    if kind == 5:
        cover = _safe_tieba_image_url(
            str(
                fragment.get("origin_src")
                or fragment.get("big_cdn_src")
                or fragment.get("cdn_src")
                or fragment.get("src")
                or ""
            ),
            "https://tieba.baidu.com/",
        )
        cover_html = (
            f'<img src="{html.escape(cover, quote=True)}" '
            'loading="eager" referrerpolicy="no-referrer">'
            if cover
            else ""
        )
        return (
            f'<figure class="tieba-video">{cover_html}'
            "<figcaption>视频内容，请打开原帖查看</figcaption></figure>"
        )

    if kind == 10:
        return '<span class="tieba-media-note">[语音内容，请打开原帖收听]</span>'

    text = fragment.get("text") or fragment.get("c") or ""
    if kind == 1:
        href = _absolute_http_url(
            str(
                fragment.get("link")
                or fragment.get("url")
                or fragment.get("raw_url")
                or ""
            ),
            "https://tieba.baidu.com/",
        )
        if href:
            label = _api_text(text or href)
            return (
                f'<a href="{html.escape(href, quote=True)}" '
                f'rel="noopener noreferrer">{label}</a>'
            )
    return _api_text(text)


def article_from_api(payload: Any, source_url: str) -> Article:
    """Convert the Tieba client JSON response into the shared render model."""
    error = _api_error(payload)
    if error:
        raise TiebaPageError(error)
    if not isinstance(payload, dict):
        raise TiebaPageError("贴吧接口返回了无效数据。")

    thread = payload.get("thread")
    thread = thread if isinstance(thread, dict) else {}
    forum = payload.get("forum") or payload.get("display_forum")
    forum = forum if isinstance(forum, dict) else {}
    posts = payload.get("post_list")
    posts = posts if isinstance(posts, list) else []
    main_post: dict[str, Any] | None = None
    for post in posts:
        if not isinstance(post, dict):
            continue
        try:
            if int(post.get("floor") or post.get("post_no") or 0) == 1:
                main_post = post
                break
        except (TypeError, ValueError):
            continue
    if main_post is None:
        candidate = payload.get("first_floor")
        if isinstance(candidate, dict):
            main_post = candidate
    if main_post is None:
        raise TiebaPageError("贴吧接口未返回帖子主楼。")

    fragments = main_post.get("content")
    if not isinstance(fragments, list):
        raise TiebaPageError("贴吧接口返回的主楼正文格式无效。")
    rendered = "".join(_api_fragment_html(fragment) for fragment in fragments)
    probe = BeautifulSoup(rendered, "html.parser")
    if not probe.get_text(" ", strip=True) and not probe.find("img"):
        raise TiebaPageError("贴吧帖子主楼正文为空。")

    thread_author = thread.get("author")
    thread_author = thread_author if isinstance(thread_author, dict) else {}
    users = _api_user_map(payload)
    post_author = users.get(str(main_post.get("author_id"))) or {}
    author = str(
        thread_author.get("name_show")
        or thread_author.get("name")
        or post_author.get("name_show")
        or post_author.get("name")
        or ""
    ).strip()
    forum_name = str(forum.get("name") or forum.get("fname") or "").strip()
    if forum_name:
        forum_label = forum_name if forum_name.endswith("吧") else f"{forum_name}吧"
        author = f"{author} · {forum_label}" if author else forum_label

    published_at = ""
    try:
        timestamp = int(main_post.get("time") or thread.get("create_time") or 0)
    except (TypeError, ValueError):
        timestamp = 0
    if timestamp > 0:
        published_at = datetime.fromtimestamp(timestamp, _BEIJING_TIME).strftime(
            "%Y-%m-%d %H:%M:%S"
        )

    title = str(
        thread.get("title") or main_post.get("title") or "百度贴吧帖子"
    ).strip()
    return Article(
        title=title,
        author=author,
        published_at=published_at,
        source_url=normalize_tieba_url(source_url),
        body_html=f'<div class="tieba-content">{rendered}</div>',
        has_locked_resources=False,
        is_authenticated=True,
    )


def _is_tieba_image(url: str) -> bool:
    return _safe_tieba_image_url(url, "https://tieba.baidu.com/") is not None


def _is_tieba_page_url(url: str) -> bool:
    """Return whether a URL is an HTTPS Tieba page-host URL.

    This is intentionally narrower than ``_is_tieba_image``.  Image hosts are
    trusted as download destinations, but only these two exact hosts may ever
    receive the selected Tieba login identifiers.
    """
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    return (
        parsed.scheme == "https"
        and host in ALLOWED_PAGE_HOSTS
        and not parsed.username
        and not parsed.password
        and port in {None, 443}
    )


def _auth_cookie_header(credentials: TiebaCredentials) -> str:
    """Build the minimal Cookie header needed by Tieba image requests."""
    fields = []
    if credentials.bduss:
        fields.append(f"BDUSS={credentials.bduss}")
    if credentials.stoken:
        fields.append(f"STOKEN={credentials.stoken}")
    return "; ".join(fields)


async def _read_limited(
    response: aiohttp.ClientResponse,
    limit: int,
    *,
    limit_message: str = "贴吧图片超过插件大小限制。",
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.content.iter_chunked(64 * 1024):
        total += len(chunk)
        if total > limit:
            raise TiebaPageError(limit_message)
        chunks.append(chunk)
    return b"".join(chunks)


async def _inline_tieba_images(
    article: Article,
    *,
    cookie: str,
    timeout_seconds: int,
    max_image_bytes: int = 8 * 1024 * 1024,
    max_total_bytes: int = 20 * 1024 * 1024,
) -> Article:
    soup = BeautifulSoup(article.body_html, "html.parser")
    headers = {
        "Accept": "image/avif,image/webp,image/png,image/jpeg,image/gif,*/*;q=0.8",
        "Referer": article.source_url,
        "User-Agent": IPHONE_SAFARI_USER_AGENT,
    }
    timeout = aiohttp.ClientTimeout(total=max(5, timeout_seconds))
    credentials = (
        parse_tieba_cookie(cookie) if cookie.strip() else TiebaCredentials("", "")
    )
    auth_cookie = _auth_cookie_header(credentials)
    total = 0
    async with aiohttp.ClientSession(
        headers=headers,
        timeout=timeout,
        cookie_jar=aiohttp.DummyCookieJar(),
    ) as session:
        for image in soup.find_all("img", src=True):
            def mark_image_failed() -> None:
                if image.parent is not None:
                    image.replace_with("[图片未加载]")

            source = str(image.get("src", ""))
            current_url = _safe_tieba_image_url(source, article.source_url)
            if not current_url:
                # Never leave arbitrary post-controlled URLs for the HTML
                # renderer to fetch later (including localhost/private hosts).
                image.decompose()
                continue
            try:
                # A credential-bearing request is only allowed to remain on
                # the exact HTTPS Tieba page hosts.  Once a redirect leaves
                # them (including for a trusted image host), credentials stay
                # stripped for the rest of that redirect chain.
                credentials_allowed = bool(auth_cookie) and _is_tieba_page_url(
                    current_url
                )
                for _ in range(_MAX_IMAGE_REDIRECTS + 1):
                    if not _safe_tieba_image_url(current_url, article.source_url):
                        image.decompose()
                        break
                    request_headers = dict(headers)
                    if credentials_allowed:
                        request_headers["Cookie"] = auth_cookie

                    async with session.get(
                        current_url,
                        headers=request_headers,
                        allow_redirects=False,
                    ) as response:
                        if response.status in _IMAGE_REDIRECT_STATUSES:
                            location = response.headers.get("Location", "")
                            redirected_url = _safe_tieba_image_url(
                                location, current_url
                            )
                            if not redirected_url:
                                # Do not leave the original URL as a fallback:
                                # its browser-side redirect could contact the
                                # untrusted destination after this function.
                                image.decompose()
                                break
                            credentials_allowed = (
                                credentials_allowed
                                and _is_tieba_page_url(redirected_url)
                            )
                            current_url = redirected_url
                            continue

                        if response.status != 200:
                            mark_image_failed()
                            break
                        content_type = response.headers.get("Content-Type", "").split(
                            ";", 1
                        )[0].lower()
                        if content_type not in {
                            "image/avif",
                            "image/gif",
                            "image/jpeg",
                            "image/png",
                            "image/webp",
                        }:
                            mark_image_failed()
                            break
                        try:
                            payload = await _read_limited(response, max_image_bytes)
                        except TiebaPageError:
                            mark_image_failed()
                            break
                        if total + len(payload) > max_total_bytes:
                            mark_image_failed()
                            break
                        total += len(payload)
                        encoded = base64.b64encode(payload).decode("ascii")
                        image["src"] = f"data:{content_type};base64,{encoded}"
                        break
                else:
                    # Exhausting the redirect budget must not leave a URL for
                    # a later renderer to follow.
                    mark_image_failed()
            except (aiohttp.ClientError, asyncio.TimeoutError):
                if image.parent is not None:
                    mark_image_failed()

    return replace(article, body_html=str(soup))


async def _read_page_limited(response: aiohttp.ClientResponse) -> str:
    payload = await _read_limited(
        response,
        _MAX_PAGE_BYTES,
        limit_message="贴吧页面超过插件大小限制。",
    )
    encoding = response.charset or "utf-8"
    try:
        return payload.decode(encoding, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


async def _post_tieba_json(
    session: aiohttp.ClientSession,
    url: str,
    fields: dict[str, Any],
) -> Any:
    async with session.post(url, data=_signed_form(fields)) as response:
        if response.status in {401, 403}:
            raise TiebaPageError("百度贴吧拒绝访问，请稍后更换网络重试。")
        if response.status != 200:
            raise TiebaPageError(f"百度贴吧接口请求失败（HTTP {response.status}）。")
        raw = await _read_page_limited(response)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TiebaPageError("贴吧接口返回了无效数据。") from exc


def account_name_from_userinfo(payload: Any) -> str:
    error = _api_error(payload)
    if error:
        raise TiebaPageError(error)
    if not isinstance(payload, dict):
        raise TiebaPageError("贴吧账号验证返回了无效数据。")
    data = payload.get("data")
    data = data if isinstance(data, dict) else {}
    user = payload.get("user") or data.get("user")
    user = user if isinstance(user, dict) else {}
    name = str(
        data.get("show_nickname")
        or data.get("user_nickname")
        or data.get("user_name")
        or data.get("name_show")
        or data.get("name")
        or user.get("show_nickname")
        or user.get("user_nickname")
        or user.get("user_name")
        or user.get("name_show")
        or user.get("name")
        or ""
    ).strip()
    if not name:
        raise TiebaPageError("贴吧 Cookie 无效，或未能读取账号昵称。")
    return name


async def fetch_tieba_article(
    raw_url: str,
    *,
    cookie: str,
    request_timeout_seconds: int = 25,
    inline_images: bool = True,
) -> Article:
    url = normalize_tieba_url(raw_url)
    credentials = parse_tieba_cookie(cookie)
    if not credentials.bduss:
        raise TiebaPageError("贴吧 Cookie 中未找到 BDUSS。")
    thread_id = url.rsplit("/", 1)[1]

    try:
        timeout = aiohttp.ClientTimeout(total=max(5, int(request_timeout_seconds)))
        async with aiohttp.ClientSession(
            headers=_APP_HEADERS,
            timeout=timeout,
        ) as session:
            payload = await _post_tieba_json(
                session,
                _THREAD_API_URL,
                {
                    "_client_type": "2",
                    "_client_version": _THREAD_CLIENT_VERSION,
                    "kz": thread_id,
                    "pn": "1",
                    "rn": "2",
                    "with_floor": "1",
                    **(
                        {"BDUSS": credentials.bduss}
                        if credentials.bduss
                        else {}
                    ),
                },
            )
        article = article_from_api(payload, url)
    except TiebaPageError:
        raise
    except asyncio.TimeoutError as exc:
        raise TiebaPageError("请求百度贴吧接口超时，请稍后重试。") from exc
    except aiohttp.ClientError as exc:
        raise TiebaPageError(f"百度贴吧接口请求失败：{exc}") from exc

    if inline_images:
        article = await _inline_tieba_images(
            article,
            cookie=cookie,
            timeout_seconds=request_timeout_seconds,
        )
    return article


async def check_tieba_cookie(
    cookie: str,
    *,
    request_timeout_seconds: int = 25,
) -> str:
    credentials = parse_tieba_cookie(cookie)
    if not credentials.bduss:
        raise TiebaPageError("贴吧 Cookie 中未找到 BDUSS。")

    try:
        timeout = aiohttp.ClientTimeout(total=max(5, int(request_timeout_seconds)))
        async with aiohttp.ClientSession(
            headers=_APP_HEADERS,
            timeout=timeout,
        ) as session:
            payload = await _post_tieba_json(
                session,
                _LOGIN_API_URL,
                {
                    "_client_type": "2",
                    "_client_version": _LOGIN_CLIENT_VERSION,
                    "bdusstoken": f"{credentials.bduss}|null",
                },
            )
        return account_name_from_userinfo(payload)
    except TiebaPageError:
        raise
    except asyncio.TimeoutError as exc:
        raise TiebaPageError("验证贴吧 Cookie 超时，请稍后重试。") from exc
    except aiohttp.ClientError as exc:
        raise TiebaPageError(f"贴吧 Cookie 验证失败：{exc}") from exc
