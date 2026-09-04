from __future__ import annotations

import asyncio
import base64
import html
import json
import re
from dataclasses import dataclass, replace
from os import PathLike
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse

import aiohttp
from bs4 import BeautifulSoup, Tag
from PIL import Image, ImageChops


DEFAULT_URL = "https://keylol.com/t1046223-1-1"
IPHONE_SAFARI_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.6 "
    "Mobile/15E148 Safari/604.1"
)
DEFAULT_MOBILE_VIEWPORT_WIDTH = 390
MOBILE_VIEWPORT_MIN_WIDTH = 320
MOBILE_VIEWPORT_MAX_WIDTH = 440
MOBILE_VIEWPORT_REFERENCE_HEIGHT = 844
MOBILE_PAGE_PADDING = 16
ALLOWED_PAGE_HOSTS = {"keylol.com", "www.keylol.com"}
_POST_ID_RE = re.compile(r"^postmessage_\d+$")
_POST_CONTAINER_RE = re.compile(r"^post_\d+$")
_THREAD_LINK_RE = re.compile(
    r"(?<![\w.-])(?:https?://)?(?:www\.)?keylol\.com/t\d+-\d+-\d+"
    r"(?:[?#][^\s<>\"']*)?",
    re.IGNORECASE,
)
_URL_TRAILING_PUNCTUATION = ".,!?;:，。！？；：、)]}>"
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
    "embed",
    "form",
    "iframe",
    "input",
    "noscript",
    "object",
    "script",
    "source",
    "style",
    "track",
    "video",
}
_IMAGE_CANDIDATES_ATTR = "data-keylol-image-candidates"
_REMOTE_FALLBACK_ATTR = "data-keylol-remote-fallback"
_SAFE_ATTRS = {
    "a": {"href", "title"},
    "figure": {"class"},
    "img": {"alt", "src", "title", _IMAGE_CANDIDATES_ATTR},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan"},
}
_IMAGE_SOURCE_ATTRS = (
    "zoomfile",
    "file",
    "data-original",
    "data-src",
    "data-lazy-src",
    "data-actualsrc",
    "src",
)
_VIDEO_FILE_EXTENSIONS = (".avi", ".m4v", ".mkv", ".mov", ".mp4", ".webm")
_AUDIO_FILE_EXTENSIONS = (".aac", ".flac", ".m4a", ".mp3", ".ogg", ".wav")
_IMAGE_PLACEHOLDER_MARKERS = (
    "static/image/filetype/",
    "static/image/common/attachimg",
    "static/image/common/folder_",
    "static/image/common/none.gif",
)
_IMAGE_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_MAX_IMAGE_REDIRECTS = 5


class KeylolPageError(RuntimeError):
    """An error that is safe to show to the command caller."""


class _DownloadLimitExceeded(KeylolPageError):
    def __init__(self, bytes_read: int):
        super().__init__("下载内容超过插件限制，已停止处理。")
        self.bytes_read = bytes_read


@dataclass(frozen=True, slots=True)
class Article:
    title: str
    author: str
    published_at: str
    source_url: str
    body_html: str
    has_locked_resources: bool
    is_authenticated: bool
    unresolved_image_count: int = 0
    image_candidates: tuple[tuple[str, ...], ...] = ()


def trim_rendered_screenshot(
    image_path: str | PathLike[str],
    *,
    bottom_padding: int = MOBILE_PAGE_PADDING,
) -> bool:
    """Trim viewport-only background below the rendered page content.

    Playwright full-page screenshots are never shorter than the browser viewport.
    The template keeps its outer background uniform, so the non-background pixel
    bounds identify the last visible content without estimating DOM height.
    """
    padding = max(0, int(bottom_padding))
    with Image.open(image_path) as source:
        rgb = source.convert("RGB")
        background_color = rgb.getpixel((0, 0))
        background = Image.new("RGB", rgb.size, background_color)
        content_bounds = ImageChops.difference(rgb, background).getbbox()
        background.close()
        rgb.close()

        if content_bounds is None:
            return False

        target_height = min(source.height, content_bounds[3] + padding)
        if target_height >= source.height:
            return False

        cropped = source.crop((0, 0, source.width, target_height))

    try:
        cropped.save(image_path, format="PNG", optimize=True)
    finally:
        cropped.close()
    return True


def normalize_mobile_viewport_width(value: object) -> int:
    """Keep rendered pages within common iPhone CSS viewport widths."""
    try:
        width = int(value)
    except (TypeError, ValueError):
        width = DEFAULT_MOBILE_VIEWPORT_WIDTH
    return min(MOBILE_VIEWPORT_MAX_WIDTH, max(MOBILE_VIEWPORT_MIN_WIDTH, width))


def mobile_viewport_height(width: object) -> int:
    """Return a matching iPhone-like viewport height for the given width."""
    normalized_width = normalize_mobile_viewport_width(width)
    return round(
        normalized_width
        * MOBILE_VIEWPORT_REFERENCE_HEIGHT
        / DEFAULT_MOBILE_VIEWPORT_WIDTH
    )


def extract_keylol_thread_urls(message: str) -> list[str]:
    """Extract unique Keylol short-form thread URLs from a chat message."""
    urls: list[str] = []
    seen: set[str] = set()
    for match in _THREAD_LINK_RE.finditer(message):
        candidate = match.group(0).rstrip(_URL_TRAILING_PUNCTUATION)
        try:
            normalized = normalize_keylol_url(candidate)
        except KeylolPageError:
            continue
        dedupe_key = normalized.lower()
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        urls.append(normalized)
    return urls


def normalize_keylol_url(raw_url: str) -> str:
    value = raw_url.strip().strip("<>")
    if not value:
        raise KeylolPageError("请提供其乐帖子链接。")
    if "://" not in value:
        value = f"https://{value}"

    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError as exc:
        raise KeylolPageError("其乐帖子链接格式无效。") from exc
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme not in {"http", "https"} or host not in ALLOWED_PAGE_HOSTS:
        raise KeylolPageError("仅支持 keylol.com 的 http/https 帖子链接。")
    if parsed.username or parsed.password or port:
        raise KeylolPageError("链接中不能包含账号、密码或自定义端口。")

    return parsed._replace(scheme="https").geturl()


def _desktop_view_url(url: str) -> str:
    """Keep the iPhone request UA while asking Discuz for its complete post DOM."""
    parsed = urlparse(url)
    query = [
        (name, value)
        for name, value in parse_qsl(parsed.query, keep_blank_values=True)
        if name.lower() != "mobile"
    ]
    query.append(("mobile", "no"))
    return parsed._replace(query=urlencode(query), fragment="").geturl()


def _absolute_http_url(value: str, base_url: str) -> str | None:
    try:
        absolute = urljoin(base_url, value.strip())
        parsed = urlparse(absolute)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    return absolute


def _is_image_placeholder(source: str) -> bool:
    normalized = source.lower()
    return any(marker in normalized for marker in _IMAGE_PLACEHOLDER_MARKERS)


def _preferred_image_source(image: Tag) -> str:
    candidates: list[str] = []
    for source_attr in _IMAGE_SOURCE_ATTRS:
        value = str(image.get(source_attr, "")).strip()
        if value:
            candidates.append(value)
            if not _is_image_placeholder(value):
                return value
    return candidates[0] if candidates else ""


def _attachment_download_source(image: Tag, root: Tag) -> str:
    """Return the authenticated Discuz download URL associated with an image.

    Keylol commonly renders the public CDN URL on the ``img`` itself while
    placing the authenticated ``forum.php?mod=attachment`` URL in a sibling
    tooltip.  Prefer the latter so the configured page Cookie is used without
    forwarding it to a CDN subdomain.
    """
    image_id = str(image.get("id", "")).strip()
    if image_id:
        menu = root.find(id=f"{image_id}_menu")
        if isinstance(menu, Tag):
            link = menu.select_one('a[href*="mod=attachment"]')
            if isinstance(link, Tag):
                return str(link.get("href", "")).strip()

    for parent in image.parents:
        if not isinstance(parent, Tag):
            continue
        if parent is root:
            break
        classes = set(parent.get("class") or [])
        if parent.name not in {"ignore_js_op", "dl"} and not classes.intersection(
            {"pattl", "attachlist", "tattl"}
        ):
            continue
        link = parent.select_one('a[href*="mod=attachment"]')
        if isinstance(link, Tag):
            return str(link.get("href", "")).strip()
    return ""


def _image_source_candidates(image: Tag, root: Tag, base_url: str) -> list[str]:
    raw_candidates = [_attachment_download_source(image, root)]
    raw_candidates.extend(str(image.get(attr, "")).strip() for attr in _IMAGE_SOURCE_ATTRS)

    candidates: list[str] = []
    seen: set[str] = set()
    for raw_source in raw_candidates:
        if not raw_source or _is_image_placeholder(raw_source):
            continue
        absolute = _absolute_http_url(raw_source, base_url)
        secure = _normalize_secure_keylol_asset(absolute) if absolute else None
        if not secure or secure in seen:
            continue
        seen.add(secure)
        candidates.append(secure)
    return candidates


def _stored_image_candidates(image: Tag) -> list[str]:
    raw_value = image.get(_IMAGE_CANDIDATES_ATTR)
    values: list[object] = []
    if isinstance(raw_value, str):
        try:
            decoded = json.loads(raw_value)
        except (TypeError, ValueError, json.JSONDecodeError):
            decoded = []
        if isinstance(decoded, list):
            values.extend(decoded)
    values.append(image.get("src", ""))

    candidates: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        secure = _normalize_secure_keylol_asset(value.strip())
        if not secure or secure in seen:
            continue
        seen.add(secure)
        candidates.append(secure)
    return candidates


def _extract_article_image_candidates(
    body_html: str,
) -> tuple[str, tuple[tuple[str, ...], ...]]:
    soup = BeautifulSoup(body_html, "html.parser")
    candidates: list[tuple[str, ...]] = []
    for image in soup.find_all("img"):
        candidates.append(tuple(_stored_image_candidates(image)))
        image.attrs.pop(_IMAGE_CANDIDATES_ATTR, None)
    return str(soup), tuple(candidates)


def _media_file_kind(value: str) -> str | None:
    try:
        path = urlparse(value).path.lower()
    except ValueError:
        path = value.lower()
    if path.endswith(_VIDEO_FILE_EXTENSIONS):
        return "video"
    if path.endswith(_AUDIO_FILE_EXTENSIONS):
        return "audio"
    return None


def _media_card(
    document: BeautifulSoup,
    *,
    kind: str,
    source_url: str,
    label: str = "",
    poster_url: str = "",
) -> Tag:
    kind_label = "视频" if kind == "video" else "音频"
    figure = document.new_tag("figure")
    figure["class"] = ["media-card", f"media-card-{kind}"]

    poster = _absolute_http_url(poster_url, source_url) if poster_url else None
    poster = _normalize_secure_keylol_asset(poster) if poster else None
    if poster:
        image = document.new_tag("img")
        image["src"] = poster
        image["alt"] = f"{kind_label}预览图"
        figure.append(image)

    caption = document.new_tag("figcaption")
    heading = document.new_tag("strong")
    heading.string = f"{kind_label}内容"
    caption.append(heading)
    if label:
        detail = document.new_tag("span")
        detail.string = f" · {label[:120]}"
        caption.append(detail)
    note = document.new_tag("span")
    note.string = "（静态截图无法播放）"
    caption.append(note)
    caption.append(document.new_tag("br"))
    link = document.new_tag("a", href=source_url)
    link.string = "打开原帖查看媒体"
    caption.append(link)
    figure.append(caption)
    return figure


def _replace_file_media_attachments(
    document: BeautifulSoup, root: Tag, base_url: str
) -> None:
    for attachment in list(root.select("dl.tattl")):
        name_node = attachment.select_one(".attnm")
        name = name_node.get_text(" ", strip=True) if name_node else ""
        link = attachment.select_one('a[href*="mod=attachment"]')
        href = str(link.get("href", "")).strip() if isinstance(link, Tag) else ""
        kind = _media_file_kind(name) or _media_file_kind(href)
        if not kind:
            continue
        attachment.replace_with(
            _media_card(
                document,
                kind=kind,
                source_url=base_url,
            )
        )


def _replace_embedded_media(document: BeautifulSoup, root: Tag, base_url: str) -> None:
    for media in list(root.find_all(["video", "audio", "iframe", "embed", "object"])):
        if media.parent is None:
            continue
        kind = "audio" if media.name == "audio" else "video"
        label = str(media.get("title", "")).strip()
        poster = str(media.get("poster", "")).strip() if media.name == "video" else ""
        media.replace_with(
            _media_card(
                document,
                kind=kind,
                source_url=base_url,
                label=label,
                poster_url=poster,
            )
        )


def _clean_fragment(content: Tag, base_url: str) -> str:
    fragment = BeautifulSoup(str(content), "html.parser")
    root = fragment.find()
    if root is None:
        raise KeylolPageError("帖子主楼正文为空。")

    for selector in (".rnd_ai_pr", ".a_mu", ".pstatus", ".jammer"):
        for node in root.select(selector):
            node.decompose()

    _replace_file_media_attachments(fragment, root, base_url)
    _replace_embedded_media(fragment, root, base_url)

    for image in root.find_all("img"):
        candidates = _image_source_candidates(image, root, base_url)
        if candidates:
            image[_IMAGE_CANDIDATES_ATTR] = json.dumps(
                candidates, ensure_ascii=True, separators=(",", ":")
            )

    for selector in (".attnm", ".aimg_tip", ".savephotop", ".attach_popup"):
        for node in root.select(selector):
            node.decompose()

    for attachment in root.select("dl.tattl"):
        for icon_cell in attachment.find_all("dt"):
            icon_cell.decompose()
        for paragraph in attachment.find_all("p"):
            if paragraph.find("img") is None:
                paragraph.decompose()

    for link in list(root.select('a[href*="mod=attachment"]')):
        if link.find("img") is not None:
            link.unwrap()
        else:
            link.decompose()

    for image in list(root.find_all("img")):
        if not _stored_image_candidates(image):
            image.decompose()

    for tag in list(root.find_all(True)):
        if tag.name in _DROP_TAGS:
            tag.decompose()
            continue
        if tag.name not in _SAFE_TAGS:
            tag.unwrap()
            continue

        is_notice = any(
            item in {"original_text_style1", "original_text_style2"}
            for item in (tag.get("class") or [])
        )
        image_source = ""
        if tag.name == "img":
            candidates = _stored_image_candidates(tag)
            image_source = candidates[0] if candidates else _preferred_image_source(tag)
        allowed = _SAFE_ATTRS.get(tag.name, set())
        for attr in list(tag.attrs):
            if attr not in allowed:
                del tag.attrs[attr]

        if is_notice:
            tag["class"] = ["article-notice"]

        if tag.name == "a":
            href = _absolute_http_url(str(tag.get("href", "")), base_url)
            if href:
                tag["href"] = href
                tag["rel"] = "noopener noreferrer"
            else:
                tag.attrs.pop("href", None)
        elif tag.name == "img":
            src = _absolute_http_url(image_source, base_url)
            src = _normalize_secure_keylol_asset(src) if src else None
            if not src:
                tag.decompose()
                continue
            tag["src"] = src
            tag["loading"] = "eager"
            tag["referrerpolicy"] = "no-referrer"

    rendered = root.decode_contents().strip()
    return re.sub(r"(?:<br\s*/?>\s*)+$", "", rendered, flags=re.IGNORECASE)


def _is_first_thread_page(source_url: str) -> bool:
    try:
        parsed = urlparse(source_url)
    except ValueError:
        return False
    short_match = re.search(r"(?:^|/)t\d+-(\d+)-\d+(?:$|[/?#])", parsed.path)
    if short_match:
        return short_match.group(1) == "1"
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    return query.get("mod") == "viewthread" and query.get("page", "1") in {"", "1"}


def _find_first_floor(soup: BeautifulSoup, source_url: str) -> tuple[Tag, Tag]:
    for post_container in soup.find_all("div", id=_POST_CONTAINER_RE):
        floor_node = post_container.select_one('a[id^="postnum"] em')
        if not floor_node or floor_node.get_text(" ", strip=True) != "1":
            continue
        content = post_container.find(id=_POST_ID_RE)
        if isinstance(content, Tag):
            return content, post_container

    # Logged-out Keylol pages currently omit the visible floor-number link even
    # on page 1.  On the first thread page the first real post container is the
    # opening post; later pages remain rejected to avoid mislabelling a reply.
    if _is_first_thread_page(source_url):
        for post_container in soup.find_all("div", id=_POST_CONTAINER_RE):
            content = post_container.find(id=_POST_ID_RE)
            if isinstance(content, Tag):
                return content, post_container

    raise KeylolPageError("当前页面不包含 1 楼；请使用帖子的第一页链接。")


def _first_floor_attachments(post_container: Tag, content: Tag, base_url: str) -> str:
    candidates: list[Tag] = []
    for selector in (".pattl", ".attachlist"):
        for node in post_container.select(selector):
            if node is content or content in node.parents:
                continue
            if any(parent in candidates for parent in node.parents):
                continue
            candidates.append(node)

    fragments = [_clean_fragment(node, base_url) for node in candidates]
    fragments = [
        fragment
        for fragment in fragments
        if fragment
        and (
            BeautifulSoup(fragment, "html.parser").find("img") is not None
            or BeautifulSoup(fragment, "html.parser").select_one(".media-card")
            is not None
        )
    ]
    if not fragments:
        return ""
    return '<section class="attachments"><h2>附件与资源</h2>' + "".join(fragments) + "</section>"


def parse_article(page_html: str, source_url: str) -> Article:
    soup = BeautifulSoup(page_html, "html.parser")
    access_message = soup.select_one("#main_message")
    if access_message is not None:
        message = access_message.get_text(" ", strip=True)
        if message:
            raise KeylolPageError(f"其乐页面提示：{message[:200]}")
    content, post_container = _find_first_floor(soup, source_url)

    title_node = soup.select_one("#thread_subject")
    title = title_node.get_text(" ", strip=True) if title_node else "其乐帖子"

    author = ""
    published_at = ""
    author_node = post_container.select_one(".pls .authi a.xw1")
    if author_node:
        author = author_node.get_text(" ", strip=True)
    time_node = post_container.select_one('em[id^="authorposton"] span[title]')
    if time_node:
        published_at = str(time_node.get("title", "")).strip()

    body_html = _clean_fragment(content, source_url)
    body_html += _first_floor_attachments(post_container, content, source_url)
    body_html, image_candidates = _extract_article_image_candidates(body_html)
    has_locked_resources = post_container.select_one(".attach_nopermission") is not None
    body_probe = BeautifulSoup(body_html, "html.parser")
    if (
        not body_probe.get_text(" ", strip=True)
        and body_probe.find("img") is None
        and not has_locked_resources
    ):
        raise KeylolPageError("主楼正文为空。")

    return Article(
        title=title,
        author=author,
        published_at=published_at,
        source_url=source_url,
        body_html=body_html,
        has_locked_resources=has_locked_resources,
        is_authenticated=soup.select_one(
            'a.btn-user-action[href*="mod=logging"][href*="action=login"]'
        )
        is None,
        image_candidates=image_candidates,
    )


def _is_keylol_asset(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return host == "keylol.com" or host.endswith(".keylol.com")


def _normalize_secure_keylol_asset(url: str) -> str | None:
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError:
        return None
    if parsed.username or parsed.password or not _is_keylol_asset(url):
        return None
    if parsed.scheme == "http" and port in {None, 80}:
        return parsed._replace(scheme="https", netloc=parsed.hostname).geturl()
    if parsed.scheme == "https" and port in {None, 443}:
        return parsed._replace(netloc=parsed.hostname).geturl()
    return None


def _sniff_image_mime(payload: bytes) -> str | None:
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if payload.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if payload.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(payload) >= 12 and payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return "image/webp"
    if len(payload) >= 12 and payload[4:8] == b"ftyp" and payload[8:12] in {
        b"avif",
        b"avis",
    }:
        return "image/avif"
    return None


async def _download_keylol_image(
    session: aiohttp.ClientSession,
    source_url: str,
    *,
    cookie: str,
    referer: str,
    proxy_url: str,
    max_image_bytes: int,
) -> tuple[str | None, bytes] | None:
    current_url = source_url
    for _ in range(_MAX_IMAGE_REDIRECTS + 1):
        current_url = _normalize_secure_keylol_asset(current_url)
        if not current_url:
            return None
        request_headers = {
            "Accept": "image/avif,image/webp,image/png,image/jpeg,image/gif,*/*;q=0.8",
            "Referer": referer,
        }
        current_host = (urlparse(current_url).hostname or "").lower().rstrip(".")
        if cookie and current_host in ALLOWED_PAGE_HOSTS:
            request_headers["Cookie"] = cookie

        async with session.get(
            current_url,
            headers=request_headers,
            proxy=proxy_url.strip() or None,
            allow_redirects=False,
        ) as response:
            if response.status in _IMAGE_REDIRECT_STATUSES:
                location = response.headers.get("Location", "")
                redirected_url = _absolute_http_url(location, current_url)
                if redirected_url and urlparse(redirected_url).scheme != "https":
                    return None
                redirected_url = (
                    _normalize_secure_keylol_asset(redirected_url)
                    if redirected_url
                    else None
                )
                if not redirected_url:
                    return None
                current_url = redirected_url
                continue
            if response.status != 200:
                return None
            payload = await _read_limited(response, max_image_bytes)
            content_type = _sniff_image_mime(payload)
            return content_type, payload
    return None


async def _read_limited(response: aiohttp.ClientResponse, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.content.iter_chunked(64 * 1024):
        total += len(chunk)
        if total > limit:
            raise _DownloadLimitExceeded(total)
        chunks.append(chunk)
    return b"".join(chunks)


async def _download_keylol_page(
    session: aiohttp.ClientSession,
    source_url: str,
    *,
    cookie: str,
    proxy_url: str,
    max_html_bytes: int,
) -> tuple[str, bytes, str]:
    current_url = normalize_keylol_url(source_url)
    for _ in range(_MAX_IMAGE_REDIRECTS + 1):
        request_url = _desktop_view_url(current_url)
        request_headers = {"Cookie": cookie} if cookie else None
        async with session.get(
            request_url,
            headers=request_headers,
            proxy=proxy_url.strip() or None,
            allow_redirects=False,
        ) as response:
            if response.status in _IMAGE_REDIRECT_STATUSES:
                location = response.headers.get("Location", "")
                redirected_url = _absolute_http_url(location, request_url)
                if not redirected_url or urlparse(redirected_url).scheme != "https":
                    raise KeylolPageError("其乐页面重定向到了不受信任的地址。")
                try:
                    current_url = normalize_keylol_url(redirected_url)
                except KeylolPageError as exc:
                    raise KeylolPageError("其乐页面重定向到了不受信任的地址。") from exc
                continue
            if response.status != 200:
                raise KeylolPageError(f"其乐页面请求失败（HTTP {response.status}）。")
            payload = await _read_limited(response, max_html_bytes)
            return current_url, payload, response.charset or "utf-8"
    raise KeylolPageError("其乐页面重定向次数过多，已停止处理。")


async def _inline_keylol_images(
    article: Article,
    session: aiohttp.ClientSession,
    *,
    cookie: str,
    proxy_url: str,
    max_image_bytes: int,
    max_total_bytes: int,
) -> Article:
    soup = BeautifulSoup(article.body_html, "html.parser")
    network_total = 0
    unresolved = 0

    images = [
        image
        for image in soup.find_all("img", src=True)
        if _is_keylol_asset(str(image.get("src", "")))
    ]
    for index, image in enumerate(images):
        article_candidates = (
            article.image_candidates[index]
            if index < len(article.image_candidates)
            else ()
        )
        stored_candidates = bool(article_candidates)
        candidates = list(article_candidates) or _stored_image_candidates(image)
        remaining_total = max_total_bytes - network_total
        if remaining_total <= 0:
            unresolved += 1
            if stored_candidates and candidates:
                fallback = next(
                    (
                        candidate
                        for candidate in candidates
                        if (urlparse(candidate).hostname or "").lower().rstrip(".")
                        not in ALLOWED_PAGE_HOSTS
                    ),
                    candidates[0],
                )
                image["src"] = fallback
                image[_REMOTE_FALLBACK_ATTR] = "1"
            continue
        resolved = False
        for src in candidates:
            remaining_total = max_total_bytes - network_total
            if remaining_total <= 0:
                break
            try:
                downloaded = await _download_keylol_image(
                    session,
                    src,
                    cookie=cookie,
                    referer=article.source_url,
                    proxy_url=proxy_url,
                    max_image_bytes=min(max_image_bytes, remaining_total),
                )
            except _DownloadLimitExceeded as exc:
                network_total += exc.bytes_read
                break
            except (aiohttp.ClientError, asyncio.TimeoutError):
                downloaded = None
            if downloaded is None:
                continue
            content_type, data = downloaded
            network_total += len(data)
            if content_type is None:
                continue
            encoded = base64.b64encode(data).decode("ascii")
            image["src"] = f"data:{content_type};base64,{encoded}"
            resolved = True
            break

        if resolved:
            continue

        unresolved += 1
        if stored_candidates and candidates:
            # Parsed pages carry a verified list of Keylol-only alternatives.
            # Keep the CDN/direct URL as a renderer fallback when inlining has
            # a transient failure, but never do this for arbitrary caller HTML.
            fallback = next(
                (
                    candidate
                    for candidate in candidates
                    if (urlparse(candidate).hostname or "").lower().rstrip(".")
                    not in ALLOWED_PAGE_HOSTS
                ),
                candidates[0],
            )
            image["src"] = fallback
            image[_REMOTE_FALLBACK_ATTR] = "1"

    for image in list(soup.find_all("img", src=True)):
        image.attrs.pop(_IMAGE_CANDIDATES_ATTR, None)
        keep_remote = image.attrs.pop(_REMOTE_FALLBACK_ATTR, None) == "1"
        if _is_keylol_asset(str(image.get("src", ""))) and not keep_remote:
            image.decompose()
    for attachment in list(soup.select(".attachments")):
        if attachment.find("img") is None and attachment.select_one(".media-card") is None:
            attachment.decompose()

    return replace(
        article,
        body_html=str(soup),
        unresolved_image_count=unresolved,
    )


async def fetch_article(
    raw_url: str,
    *,
    cookie: str = "",
    proxy_url: str = "",
    request_timeout_seconds: int = 25,
    max_html_bytes: int = 3 * 1024 * 1024,
    inline_keylol_images: bool = True,
    max_image_bytes: int = 5 * 1024 * 1024,
    max_total_image_bytes: int = 10 * 1024 * 1024,
    require_authentication: bool = False,
) -> Article:
    url = normalize_keylol_url(raw_url)
    if "\r" in cookie or "\n" in cookie:
        raise KeylolPageError("Cookie 配置格式无效。")

    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
        "Referer": "https://keylol.com/",
        "User-Agent": IPHONE_SAFARI_USER_AGENT,
    }
    clean_cookie = cookie.strip()
    timeout = aiohttp.ClientTimeout(total=max(5, request_timeout_seconds))
    try:
        async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
            final_url, payload, encoding = await _download_keylol_page(
                session,
                url,
                cookie=clean_cookie,
                proxy_url=proxy_url,
                max_html_bytes=max_html_bytes,
            )
            page_html = payload.decode(encoding, errors="replace")

            article = parse_article(page_html, final_url)
            if require_authentication and not article.is_authenticated:
                raise KeylolPageError("Cookie 未生效或已经过期，请重新获取后再试。")
            if inline_keylol_images:
                article = await _inline_keylol_images(
                    article,
                    session,
                    cookie=clean_cookie,
                    proxy_url=proxy_url,
                    max_image_bytes=max_image_bytes,
                    max_total_bytes=max_total_image_bytes,
                )
            return article
    except KeylolPageError:
        raise
    except asyncio.TimeoutError as exc:
        raise KeylolPageError("请求其乐页面超时，请稍后重试。") from exc
    except aiohttp.ClientError as exc:
        raise KeylolPageError(f"无法访问其乐页面：{exc}") from exc


def build_render_html(
    article: Article,
    *,
    content_width: int = DEFAULT_MOBILE_VIEWPORT_WIDTH,
    show_access_notice: bool = True,
) -> str:
    width = normalize_mobile_viewport_width(content_width)
    height = mobile_viewport_height(width)
    meta_parts = [part for part in (article.author, article.published_at) if part]
    meta = " · ".join(html.escape(part) for part in meta_parts)
    locked_notice = ""
    if article.has_locked_resources and show_access_notice:
        locked_notice = (
            '<div class="access-note">该帖子还有登录后才能查看的附件或资源；当前图片只包含已获取到的主楼正文。</div>'
        )
    image_notice = ""
    if article.unresolved_image_count:
        image_notice = (
            '<div class="access-note">'
            f"有 {article.unresolved_image_count} 张站内图片下载失败；请检查 Cookie、代理或图片大小限制。"
            "</div>"
        )

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width={width}, height={height}, initial-scale=1">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src https: http: data:; style-src 'unsafe-inline'">
  <style>
    * {{ box-sizing: border-box; }}
    html, body {{ margin: 0; padding: 0; background: #fff; color: #20242a; }}
    body {{ font: 17px/1.7 -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif; }}
    .page {{ width: {width}px; max-width: 100%; margin: 0 auto; background: #fff; }}
    .header {{ padding: 18px 16px 10px; }}
    h1 {{ margin: 0; color: #15191e; font-size: 24px; line-height: 1.35; overflow-wrap: anywhere; }}
    .meta {{ min-height: 1.75em; margin-top: 8px; color: #68717d; font-size: 14px; }}
    .article {{ padding: 8px 16px 20px; overflow-wrap: anywhere; }}
    .article > :first-child {{ margin-top: 0; }}
    .article > :last-child {{ margin-bottom: 0; }}
    .article p {{ margin: .7em 0; }}
    .article ul, .article ol {{ margin: .7em 0; padding-left: 1.6em; }}
    .article img {{ max-width: 100%; width: auto; height: auto; }}
    .article table {{ width: 100%; border-collapse: collapse; margin: 12px 0; }}
    .article td, .article th {{ padding: 7px 8px; border: 1px solid #dfe3e8; }}
    .article blockquote {{ margin: 12px 0; padding-left: 12px; color: #525b66; border-left: 3px solid #c7ccd2; }}
    .article pre {{ overflow-wrap: anywhere; white-space: pre-wrap; padding: 12px; background: #f6f8fa; }}
    .article a {{ color: #1769aa; text-decoration: none; }}
    .attachments {{ margin-top: 20px; padding-top: 16px; border-top: 1px solid #e7eaee; }}
    .attachments h2 {{ margin: 0 0 12px; font-size: 19px; }}
    .media-card {{ margin: 14px 0; padding: 12px; background: #f6f8fa; border: 1px solid #dfe3e8; }}
    .media-card img {{ display: block; margin: 0 auto 10px; }}
    .media-card figcaption {{ color: #525b66; font-size: 15px; line-height: 1.55; }}
    .media-card figcaption strong {{ color: #20242a; }}
    .article-notice {{ margin: 0 0 16px; padding-left: 10px; color: #525b66; border-left: 3px solid #aeb5bd; }}
    .access-note {{ margin: 0 16px 16px; padding: 10px 0; color: #666; font-size: 15px; border-top: 1px solid #e7eaee; border-bottom: 1px solid #e7eaee; }}
    .source {{ padding: 12px 16px 16px; color: #7a838e; font-size: 13px; border-top: 1px solid #e7eaee; overflow-wrap: anywhere; }}
  </style>
</head>
<body>
  <main class="page">
    <header class="header">
      <h1>{html.escape(article.title)}</h1>
      <div class="meta">{meta}</div>
    </header>
    <article class="article">{article.body_html}</article>
    {locked_notice}
    {image_notice}
    <footer class="source">来源：{html.escape(article.source_url)}</footer>
  </main>
</body>
</html>"""
