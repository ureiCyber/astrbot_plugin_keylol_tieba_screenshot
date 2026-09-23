"""Isolated iPhone-style Playwright capture for a Baidu Tieba first post.

This renderer is intentionally separate from :mod:`tieba_page`.  It keeps the
real Tieba page's mobile/desktop CSS, but only permits the requested thread,
trusted Baidu static assets, and read-only requests.  In particular,
login cookies are installed as host-only cookies on the two Tieba page hosts;
they are never copied to an image or CDN host.
"""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from urllib.parse import urlparse

from PIL import Image

try:
    from .tieba_dom import FIRST_POST_RESOLVER, FIRST_POST_READY_SCRIPT, PAGE_SNAPSHOT_SCRIPT
    from .tieba_diagnostics import classify_tieba_failure, sanitize_diagnostics, save_failure_artifacts
except ImportError:
    from tieba_dom import FIRST_POST_RESOLVER, FIRST_POST_READY_SCRIPT, PAGE_SNAPSHOT_SCRIPT
    from tieba_diagnostics import classify_tieba_failure, sanitize_diagnostics, save_failure_artifacts

try:
    from .screenshot_capture import (
        DEVICE_SCALE_FACTOR,
        ScreenshotCaptureError,
        _capture_segmented_screenshot,
    )
except ImportError:  # Direct import from the plugin directory.
    from screenshot_capture import (  # type: ignore[no-redef]
        DEVICE_SCALE_FACTOR,
        ScreenshotCaptureError,
        _capture_segmented_screenshot,
    )

try:  # Optional: the API/HTML renderer remains usable without Playwright.
    from playwright.async_api import async_playwright
except ImportError:  # pragma: no cover
    async_playwright = None  # type: ignore[assignment]


DEFAULT_BROWSER_VIEWPORT_WIDTH = 390
DEFAULT_BROWSER_VIEWPORT_HEIGHT = 844
DEFAULT_BROWSER_TIMEOUT_MS = 45_000
FIRST_POST_TIMEOUT_MS = 10_000
DEBUG_DIRECTORY = Path(__file__).resolve().parent / "logs" / "debug" / "tieba"
ALLOWED_TIEBA_HOSTS = {"tieba.baidu.com", "www.tieba.baidu.com"}
# These are first-party/static Baidu hosts used by ordinary Tieba pages.  The
# route handler still requires HTTPS, GET, and an allowed resource type.
ALLOWED_TIEBA_STATIC_HOSTS = {
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
_LEGACY_TIEBA_EMOTICON_HOST = "static.tieba.baidu.com"
_LEGACY_TIEBA_EMOTICON_PATH_RE = re.compile(
    r"^/tb/editor/images/client/image_emoticon[0-9]{1,3}\.png$",
    re.IGNORECASE,
)
ALLOWED_TIEBA_IMAGE_HOSTS = (
    ALLOWED_TIEBA_HOSTS
    | ALLOWED_TIEBA_STATIC_HOSTS
    | {_LEGACY_TIEBA_EMOTICON_HOST}
)
MAX_BROWSER_PAGE_HEIGHT = 100_000
MAX_BROWSER_IMAGE_COUNT = 500
MAX_BROWSER_DOM_NODES = 20_000
# Deprecated compatibility symbol. Final image safety is enforced by
# screenshot_safety; page height remains bounded independently above.
MAX_BROWSER_TOTAL_PIXELS = 120_000_000
_POST_PATH_RE = re.compile(r"^/p/(\d+)/?$", re.I)
_IMAGE_EXT_RE = re.compile(r"\.(?:avif|gif|jpe?g|png|webp)(?:$|[?#])", re.I)
_MEDIA_VIDEO_RE = re.compile(r"\.(?:avi|flv|m4v|mkv|mov|mp4|ts|webm|wmv)(?:$|[?#])", re.I)
_MEDIA_AUDIO_RE = re.compile(r"\.(?:aac|amr|flac|m4a|mp3|ogg|opus|wav|wma)(?:$|[?#])", re.I)


class TiebaBrowserCaptureError(RuntimeError):
    """Base class for errors safe to show to a command caller."""

    default_stage = "unknown"
    default_reason = "capture_failed"

    def __init__(
        self,
        message: str,
        *,
        stage: str | None = None,
        reason: str | None = None,
        segment_index: int | None = None,
        cookie_present: bool | None = None,
        bduss_found: bool | None = None,
        stoken_found: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.stage = stage or self.default_stage
        self.reason = reason or self.default_reason
        self.segment_index = segment_index
        self.cookie_present = cookie_present
        self.bduss_found = bduss_found
        self.stoken_found = stoken_found
        self.diagnostics: dict[str, object] = {}


class TiebaBrowserUnavailable(TiebaBrowserCaptureError):
    """Playwright or a usable Chromium browser is unavailable."""

    default_stage = "browser_launch"
    default_reason = "unavailable"


class TiebaBrowserUrlError(TiebaBrowserCaptureError):
    """The URL is not a supported Tieba thread URL."""

    default_stage = "navigation"
    default_reason = "invalid_url"


class TiebaBrowserNavigationError(TiebaBrowserCaptureError):
    """The isolated page was not the requested thread or had no first post."""

    default_stage = "navigation"
    default_reason = "navigation_failed"


class TiebaBrowserTimeoutError(TiebaBrowserCaptureError):
    """Bounded browser navigation or screenshot work timed out."""

    default_stage = "capture"
    default_reason = "timeout"


class TiebaBrowserCookieError(TiebaBrowserCaptureError):
    """The supplied Cookie header cannot provide the browser credentials."""

    default_stage = "cookie_parse"
    default_reason = "invalid_cookie"


class TiebaBrowserCaptureStatus(str, Enum):
    OK = "ok"
    PARTIAL = "partial"


@dataclass(frozen=True, slots=True)
class TiebaBrowserCaptureResult:
    image_path: str
    source_url: str
    title: str
    image_count: int
    loaded_image_count: int
    failed_image_count: int
    status: TiebaBrowserCaptureStatus
    image_paths: tuple[str, ...] = ()
    cookie_present: bool = False
    bduss_found: bool = False
    stoken_found: bool = False
    viewport_css_width: int = 0
    device_pixel_ratio: int = DEVICE_SCALE_FACTOR
    raw_png_width: int = 0
    raw_png_height: int = 0


def normalize_tieba_browser_url(raw_url: str) -> str:
    """Validate and canonicalize only ``tieba.baidu.com/p/<digits>`` URLs."""

    value = str(raw_url or "").strip().strip("<>")
    if not value:
        raise TiebaBrowserUrlError("请提供百度贴吧帖子链接。")
    if "://" not in value:
        value = "https://" + value
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError as exc:
        raise TiebaBrowserUrlError("贴吧帖子链接格式无效。") from exc
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme.lower() not in {"http", "https"} or host not in ALLOWED_TIEBA_HOSTS:
        raise TiebaBrowserUrlError("仅支持 tieba.baidu.com 的 http/https 帖子链接。")
    if parsed.username or parsed.password or port or parsed.fragment:
        raise TiebaBrowserUrlError("贴吧链接不能包含账号信息、端口或片段。")
    match = _POST_PATH_RE.fullmatch(parsed.path or "")
    if not match:
        raise TiebaBrowserUrlError("仅支持百度贴吧的 /p/数字 帖子链接。")
    # Query parameters such as see_lz are deliberately discarded: navigation
    # and routing must stay bound to this one thread document.
    return f"https://tieba.baidu.com/p/{match.group(1)}"


def _tieba_thread_id(url: str) -> str | None:
    match = _POST_PATH_RE.fullmatch(urlparse(url).path or "")
    return match.group(1) if match else None


# Convenience alias for callers that already use tieba_page's naming.
normalize_tieba_url = normalize_tieba_browser_url


def _safe_https_url(value: str, hosts: set[str]) -> bool:
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    return (
        parsed.scheme.lower() == "https"
        and host in hosts
        and not parsed.username
        and not parsed.password
        and port is None
    )


def _is_allowed_thread_document(value: str, expected_thread_id: str) -> bool:
    if not _safe_https_url(value, ALLOWED_TIEBA_HOSTS):
        return False
    return _tieba_thread_id(value) == str(expected_thread_id)


def _is_allowed_style_request(value: str) -> bool:
    if not _safe_https_url(value, ALLOWED_TIEBA_HOSTS | ALLOWED_TIEBA_STATIC_HOSTS):
        return False
    return (urlparse(value).path or "").lower().endswith(".css")


def _is_allowed_script_request(value: str) -> bool:
    # A first-party bundle can generate the first floor. Do not permit arbitrary
    # hosts, JSONP endpoints, or scripts embedded by a post on an image host.
    hosts = ALLOWED_TIEBA_HOSTS | {f"tb{i}.bdstatic.com" for i in range(1, 7)}
    return _safe_https_url(value, hosts) and (urlparse(value).path or "").lower().endswith(".js")


def _is_allowed_image_request(value: str) -> bool:
    if not _safe_https_url(value, ALLOWED_TIEBA_IMAGE_HOSTS):
        return False
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower().rstrip(".")
    path = (parsed.path or "").lower()
    if host == _LEGACY_TIEBA_EMOTICON_HOST:
        return bool(
            _LEGACY_TIEBA_EMOTICON_PATH_RE.fullmatch(parsed.path or "")
            and not parsed.query
            and not parsed.fragment
        )
    # Tieba attachment URLs are occasionally extensionless; only permit them
    # on known image hosts and in the familiar image/attachment paths.
    if _IMAGE_EXT_RE.search(path):
        return True
    return host in ALLOWED_TIEBA_HOSTS and any(
        marker in path for marker in ("/photo/", "/album/", "/tbpic/", "/attachment/")
    )


def parse_tieba_cookie_header(cookie: str) -> list[tuple[str, str]]:
    """Extract only BDUSS and STOKEN from a Cookie header.

    Only the two credentials used by the browser context are interpreted.
    Everything else is deliberately ignored, including copied browser
    attributes and malformed unrelated segments.  Header-level injection
    characters and NUL are rejected before any segment is considered.
    """

    raw = str(cookie or "").strip()
    cookie_present = bool(raw)
    if "\r" in raw or "\n" in raw:
        raise TiebaBrowserCookieError(
            "Cookie 配置格式无效。",
            reason="header_injection",
            cookie_present=cookie_present,
        )
    if "\x00" in raw:
        nul_segment_index: int | None = None
        for segment_index, piece in enumerate(raw.split(";")):
            item = piece.strip()
            if "=" not in item:
                continue
            name, _value = (part.strip() for part in item.split("=", 1))
            if "\x00" in item and name.upper() in {"BDUSS", "STOKEN"}:
                nul_segment_index = segment_index
                break
        raise TiebaBrowserCookieError(
            "Cookie 配置格式无效。",
            reason="nul_value",
            segment_index=nul_segment_index,
            cookie_present=cookie_present,
        )
    if not raw:
        raise TiebaBrowserCookieError(
            "贴吧 Cookie 中未找到 BDUSS。",
            reason="missing_bduss",
            cookie_present=False,
            bduss_found=False,
            stoken_found=False,
        )
    if raw.lower().startswith("cookie:"):
        raw = raw[7:].strip()
    found: dict[str, str] = {}
    for _segment_index, piece in enumerate(raw.split(";")):
        item = piece.strip()
        if not item:
            continue
        if "=" not in item:
            # A copied browser Cookie header can contain flags or unrelated
            # bare text.  Neither can provide a browser credential.
            continue
        name, value = (part.strip() for part in item.split("=", 1))
        upper = name.upper()
        if upper not in {"BDUSS", "STOKEN"}:
            # Never validate or install unrelated browser cookies.  In
            # particular, odd names and values from a copied Cookie header
            # must not make the Playwright path fail.
            continue
        if "\x00" in name or "\x00" in value:
            raise TiebaBrowserCookieError(
                "Cookie 配置格式无效。",
                reason="nul_value",
                segment_index=_segment_index,
                cookie_present=cookie_present,
                bduss_found=bool(found.get("BDUSS")),
                stoken_found=bool(found.get("STOKEN")),
            )
        found[upper] = value

    bduss = found.get("BDUSS", "")
    stoken = found.get("STOKEN", "")
    if not bduss:
        raise TiebaBrowserCookieError(
            "贴吧 Cookie 中未找到 BDUSS。",
            reason="missing_bduss",
            cookie_present=cookie_present,
            bduss_found=False,
            stoken_found=bool(stoken),
        )
    return [
        (name, value)
        for name, value in (("BDUSS", bduss), ("STOKEN", stoken))
        if value
    ]


# Alias matching the naming used by the Keylol browser module.
parse_tieba_browser_cookie_header = parse_tieba_cookie_header


def _attach_cookie_diagnostics(
    error: TiebaBrowserCaptureError,
    *,
    cookie_present: bool,
    pairs: list[tuple[str, str]] | None,
) -> TiebaBrowserCaptureError:
    """Add non-secret Cookie presence flags to an already safe error."""

    error.cookie_present = cookie_present
    if pairs is not None:
        names = {name.upper() for name, _value in pairs}
        error.bduss_found = "BDUSS" in names
        error.stoken_found = "STOKEN" in names
    return error


def _bounded_timeout(value: object) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = DEFAULT_BROWSER_TIMEOUT_MS
    return min(120_000, max(5_000, number))


def _bounded_width(value: object) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = DEFAULT_BROWSER_VIEWPORT_WIDTH
    return min(440, max(320, number))


def _playwright_proxy(value: str) -> dict[str, str] | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if "\r" in raw or "\n" in raw:
        raise TiebaBrowserCaptureError(
            "代理配置格式无效。",
            stage="browser_launch",
            reason="invalid_proxy",
        )
    try:
        parsed = urlparse(raw)
        port = parsed.port
    except ValueError as exc:
        raise TiebaBrowserCaptureError(
            "代理配置格式无效。",
            stage="browser_launch",
            reason="invalid_proxy",
        ) from exc
    if (
        parsed.scheme.lower() not in {"http", "https", "socks5"}
        or not parsed.hostname
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise TiebaBrowserCaptureError(
            "代理配置格式无效。",
            stage="browser_launch",
            reason="invalid_proxy",
        )
    server = f"{parsed.scheme.lower()}://{parsed.hostname}"
    if port:
        server += f":{port}"
    result = {"server": server}
    if parsed.username:
        result["username"] = parsed.username
    if parsed.password:
        result["password"] = parsed.password
    return result


_TRANSFORM_SCRIPT = r"""
async ({sourceUrl, suppliedTitle, suppliedAuthor, suppliedPublishedAt}) => {
  const placeholder = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==";
  if (!document.querySelector('meta[name="viewport"]')) { const viewport = document.createElement("meta"); viewport.name = "viewport"; viewport.content = "width=device-width, initial-scale=1"; document.head.append(viewport); }
  const imageAttrs = ["data-original", "data-src", "data-lazy-src", "data-actualsrc", "data-imgurl", "data-image-url", "data-tieba-src", "data-origin-src", "data-zoomfile", "data-url", "src"];
  const badImage = /(?:none\.gif|loading(?:[._-]|\.gif)|blank|spacer)/i;
  const text = (node) => (node && node.textContent || "").replace(/\s+/g, " ").trim();
  const firstText = (root, selectors) => { for (const selector of selectors) { const value = text(root.querySelector(selector)); if (value) return value; } return ""; };
  const allowedHost = (host) => ["tieba.baidu.com", "www.tieba.baidu.com", "tb1.bdstatic.com", "tb2.bdstatic.com", "tb3.bdstatic.com", "tb4.bdstatic.com", "tb5.bdstatic.com", "tb6.bdstatic.com", "tbpic.bdimg.com", "imgsrc.baidu.com", "imgsa.baidu.com", "img0.baidu.com", "img1.baidu.com", "img2.baidu.com", "img3.baidu.com", "tiebapic.baidu.com", "himg.bdimg.com", "bdstatic.com", "static.tieba.baidu.com"].includes(host.toLowerCase().replace(/\.$/, ""));
  const legacyEmoticonUrl = /^http:\/\/static\.tieba\.baidu\.com\/tb\/editor\/images\/client\/image_emoticon[0-9]{1,3}\.png$/i;
  const legacyEmoticonPath = /^\/tb\/editor\/images\/client\/image_emoticon[0-9]{1,3}\.png$/i;
  const absolute = (value) => {
    if (!value || /^javascript:/i.test(value)) return "";
    const raw = String(value).trim();
    // Rewrite the one known legacy HTTP image form before the general HTTPS
    // gate. Arbitrary HTTP and other static.tieba.baidu.com paths stay blocked.
    const candidate = legacyEmoticonUrl.test(raw) ? raw.replace(/^http:/i, "https:") : raw;
    try {
      const url = new URL(candidate, location.href);
      const host = url.hostname.toLowerCase().replace(/\.$/, "");
      if (host === "static.tieba.baidu.com" && (!legacyEmoticonPath.test(url.pathname) || url.search || url.hash)) return "";
      return url.protocol === "https:" && allowedHost(url.hostname) && !url.username && !url.password && !url.port ? url.href : "";
    } catch (_) { return ""; }
  };
  const {selected, candidates} = (__RESOLVER__)();
  if (!selected) throw new Error("NO_FIRST_POST");
  const {article, content} = selected;
  article.setAttribute("data-tieba-capture-article", "1");
  for (const node of [document.documentElement, document.body]) { node.style.setProperty("width", "100%", "important"); node.style.setProperty("max-width", "100%", "important"); node.style.setProperty("min-width", "0", "important"); node.style.setProperty("box-sizing", "border-box", "important"); }
  for (const node of candidates) if (node !== article && !article.contains(node) && !node.contains(article)) node.style.display = "none";
  // Desktop Tieba wraps posts in a roughly 980px fixed-width container.  A
  // 390px mobile viewport must not merely crop that container horizontally.
  for (let node = article; node && node !== document.body; node = node.parentElement) {
    node.style.setProperty("width", "100%", "important"); node.style.setProperty("max-width", "100%", "important"); node.style.setProperty("min-width", "0", "important"); node.style.setProperty("margin-left", "0", "important"); node.style.setProperty("margin-right", "0", "important"); node.style.setProperty("float", "none", "important"); node.style.setProperty("box-sizing", "border-box", "important");
  }
  for (const node of article.querySelectorAll(".d_post_content_main, .d_post_content, .j_d_post_content, .d_author, .p_author")) {
    node.style.setProperty("max-width", "100%", "important"); node.style.setProperty("min-width", "0", "important"); node.style.setProperty("box-sizing", "border-box", "important");
  }
  // Keep only the ancestor path needed to lay out the first post.  This also
  // removes sidebars, recommendations, and any other post-list siblings while
  // retaining the article's real CSS context.
  for (const node of document.body.querySelectorAll("*")) if (node !== article && !article.contains(node) && !node.contains(article)) node.style.display = "none";
  for (const node of document.querySelectorAll("script, noscript, form, input, button, .j_thread_list, .threadlist, .l_posts_num")) if (node !== article && !article.contains(node)) node.remove();
  for (const node of document.querySelectorAll("body > *")) if (node !== article && !node.contains(article) && !/^(STYLE|LINK)$/.test(node.tagName)) node.style.display = "none";
  for (const node of article.querySelectorAll("script, noscript, form, input, button")) node.remove();
  for (const node of article.querySelectorAll("*")) for (const attr of [...node.attributes]) if (/^on/i.test(attr.name) || ["srcdoc", "formaction"].includes(attr.name.toLowerCase())) node.removeAttribute(attr.name);
  for (const media of [...article.querySelectorAll("video, audio, iframe, embed, object")]) { const card = document.createElement("div"); card.className = "tieba-browser-media-card"; card.textContent = `媒体内容${(media.getAttribute("title") || "").slice(0, 120)}（静态截图无法播放）`; media.replaceWith(card); }
  const title = suppliedTitle || firstText(document, ["#thread_subject", "h1", "title"]) || "百度贴吧帖子";
  const author = suppliedAuthor || firstText(article, [".p_author_name, .d_name, .j_user_card, a.username"]) || "";
  const publishedAt = suppliedPublishedAt || firstText(article, [".tail-info, .post-tail, .p_tail, time"]) || "";
  const oldFooter = article.querySelector(".tieba-capture-footer"); if (oldFooter) oldFooter.remove();
  const footer = document.createElement("footer"); footer.className = "tieba-capture-footer";
  const heading = document.createElement("strong"); heading.textContent = "截图信息"; footer.append(heading);
  const meta = document.createElement("div"); meta.textContent = [title, author, publishedAt].filter(Boolean).join(" · "); footer.append(meta);
  const source = document.createElement("a"); source.href = sourceUrl; source.textContent = sourceUrl; source.rel = "noopener noreferrer"; footer.append(source); content.append(footer);
  const style = document.createElement("style"); style.id = "tieba-browser-capture-style"; style.textContent = `.tieba-capture-footer{clear:both!important;display:block!important;position:static!important;margin:24px 0 8px;padding:14px 0 4px;border-top:1px solid #dfe3e8;color:#68717d;font-size:12px;line-height:1.55;overflow-wrap:anywhere}.tieba-capture-footer strong{display:block;color:#20242a;font-size:14px}.tieba-capture-footer a{display:block;color:#1769aa;text-decoration:none}.tieba-browser-media-card,.tieba-browser-image-failed{margin:12px 0;padding:12px;border:1px solid #dfe3e8;border-radius:6px;background:#f6f8fa;color:#59636e;font-size:13px;line-height:1.5;overflow-wrap:anywhere}.tieba-browser-image-failed{border-color:#efd4d4;background:#fff7f7;color:#9a3b3b}[data-tieba-capture-article="1"],[data-tieba-capture-article="1"] .d_post_content,[data-tieba-capture-article="1"] .j_d_post_content{box-sizing:border-box;max-width:100%;width:auto;overflow-wrap:anywhere;word-break:break-word}[data-tieba-capture-article="1"] img{max-width:100%;height:auto}[data-tieba-capture-article="1"] table{max-width:100%;display:block;overflow-x:auto}`; document.head.append(style);
  let imageCount = 0, missingImageCount = 0;
  const missing = (label) => { const card = document.createElement("div"); card.className = "tieba-browser-media-card tieba-browser-image-failed"; card.textContent = `图片地址不可用${label ? ` · ${label.slice(0, 120)}` : ""}`; return card; };
  for (const image of [...content.querySelectorAll("img")]) {
    const values = []; for (const attr of imageAttrs) { const value = image.getAttribute(attr); if (value) values.push(value); }
    const srcset = image.getAttribute("srcset") || image.getAttribute("data-srcset") || ""; for (const value of srcset.split(",").map((x) => x.trim().split(/\s+/)[0]).reverse()) if (value) values.push(value);
    const candidates = []; for (const value of values) { if (badImage.test(value)) continue; const candidate = absolute(value); if (candidate && !candidates.includes(candidate)) candidates.push(candidate); }
    const current = absolute(image.currentSrc || image.src || "");
    if (current && image.complete && image.naturalWidth > 1) { image.dataset.tiebaLoaded = "1"; image.loading = "eager"; imageCount++; continue; }
    if (!candidates.length) { if (values.some((value) => badImage.test(value))) image.remove(); else { image.replaceWith(missing(image.alt || "")); missingImageCount++; } continue; }
    image.dataset.tiebaCandidates = JSON.stringify(candidates); image.dataset.tiebaPending = "1"; image.src = placeholder; image.removeAttribute("srcset"); image.removeAttribute("data-srcset"); for (const attr of imageAttrs) if (attr !== "src") image.removeAttribute(attr); image.loading = "eager"; image.decoding = "async"; image.referrerPolicy = "strict-origin-when-cross-origin"; imageCount++;
  }
  return {title, imageCount, missingImageCount, selectedSelector: selected.selector, nodeCount: article.querySelectorAll("*").length};
}
""".replace("__RESOLVER__", FIRST_POST_RESOLVER)


_SCROLL_SCRIPT = r"""
async ({maxImages, maxHeight, perImageTimeoutMs}) => {
  const placeholder = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="; const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const warmPage = async () => { let stable = 0, steps = 0; while (steps++ < 320) { const height = document.documentElement.scrollHeight; if (height > maxHeight) return {tooTall:true}; const bottom = Math.max(0, height - innerHeight); const next = Math.min(bottom, scrollY + Math.max(360, Math.floor(innerHeight * .8))); scrollTo(0, next); await wait(60); const expanded = document.documentElement.scrollHeight; if (next >= bottom - 1 && expanded <= height + 1) stable++; else stable = 0; if (stable >= 2) break; } return {tooTall: document.documentElement.scrollHeight > maxHeight}; };
  const initial = await warmPage(); scrollTo(0, 0); if (initial.tooTall) return {pageHeight: document.documentElement.scrollHeight, tooMany:false, tooTall:true};
  const images = [...document.querySelectorAll("img[data-tieba-candidates]")]; if (images.length > maxImages) return {pageHeight: document.documentElement.scrollHeight, tooMany:true, tooTall:false};
  const loadOnce = (image, source) => new Promise((resolve) => { let settled = false; const finish = (ok) => { if (settled) return; settled = true; clearTimeout(timer); image.onload = null; image.onerror = null; resolve(Boolean(ok)); }; const timer = setTimeout(() => finish(false), perImageTimeoutMs); image.onload = () => finish(image.naturalWidth > 0); image.onerror = () => finish(false); image.src = source; if (image.complete) queueMicrotask(() => finish(image.naturalWidth > 0)); });
  let loaded = 0, failed = 0; for (const image of images) { image.scrollIntoView({block:"center", inline:"nearest"}); await wait(40); let candidates = []; try { const parsed = JSON.parse(image.dataset.tiebaCandidates || "[]"); if (Array.isArray(parsed)) candidates = parsed; } catch (_) {} let ok = false; for (const source of candidates) { for (let attempt = 0; attempt < 2 && !ok; attempt++) { ok = await loadOnce(image, source); if (!ok) { image.src = placeholder; await wait(220 * (attempt + 1)); } } if (ok) break; } delete image.dataset.tiebaCandidates; if (ok) { image.dataset.tiebaLoaded = "1"; loaded++; } else { image.dataset.tiebaFailed = "1"; failed++; } if (document.documentElement.scrollHeight > maxHeight) return {pageHeight:document.documentElement.scrollHeight, loaded, failed, tooMany:false, tooTall:true}; await wait(80); }
  const final = await warmPage(); scrollTo(0, 0); await wait(100); return {pageHeight:document.documentElement.scrollHeight, loaded, failed, tooMany:false, tooTall:Boolean(final.tooTall)};
}
"""


_FINALIZE_IMAGES_SCRIPT = r"""
() => { const article = document.querySelector('[data-tieba-capture-article="1"]'); const root = article || document; let failed = 0; for (const image of [...root.querySelectorAll("img")]) { if (image.complete && image.naturalWidth > 0) continue; const card = document.createElement("div"); card.className = "tieba-browser-media-card tieba-browser-image-failed"; card.textContent = `图片加载失败${image.alt ? ` · ${image.alt.slice(0,120)}` : ""}`; image.replaceWith(card); failed++; } const footer = article && article.querySelector(".tieba-capture-footer"); const footerBottom = footer ? Math.ceil(footer.getBoundingClientRect().bottom + scrollY + 8) : 0; const articleBottom = article ? Math.ceil(article.getBoundingClientRect().bottom + scrollY + 8) : document.documentElement.scrollHeight; const captureHeight = Math.max(articleBottom, footerBottom); document.body.style.minHeight = `${captureHeight}px`; return {loaded:[...root.querySelectorAll("img")].filter((image) => image.dataset.tiebaLoaded === "1" && image.complete && image.naturalWidth > 0).length, failed, pageHeight:document.documentElement.scrollHeight, captureHeight}; }
"""


_HIDE_REPEATED_CHROME_SCRIPT = r"""
() => { const attr = "data-tieba-capture-repeated-chrome"; for (const node of document.body.querySelectorAll("*")) { const css = getComputedStyle(node); if (css.position !== "fixed" && css.position !== "sticky") continue; const rect = node.getBoundingClientRect(); if (css.position === "fixed" || (rect.bottom > 0 && rect.top < Math.min(140, innerHeight))) node.setAttribute(attr, "1"); } let style = document.getElementById("tieba-hide-repeated-chrome"); if (!style) { style = document.createElement("style"); style.id = "tieba-hide-repeated-chrome"; style.textContent = `[${attr}="1"]{visibility:hidden!important}`; document.head.append(style); } }
"""
_RESTORE_REPEATED_CHROME_SCRIPT = r"""
() => { document.getElementById("tieba-hide-repeated-chrome")?.remove(); for (const node of document.querySelectorAll("[data-tieba-capture-repeated-chrome]")) node.removeAttribute("data-tieba-capture-repeated-chrome"); scrollTo(0, 0); }
"""


async def _page_snapshot(page: object) -> dict:
    """Collect page evidence, never cookie/storage contents or exception strings."""
    try:
        return await asyncio.wait_for(page.evaluate(PAGE_SNAPSHOT_SCRIPT), timeout=2)
    except Exception:
        return {"url": str(getattr(page, "url", "")), "snapshot_unavailable": True}


async def _attach_page_diagnostics(error, page, *, pairs, status, blocked_document_url, blocked_resources):
    """Diagnostics must never replace the original native-renderer failure."""
    if page is None:
        return
    try:
        snapshot = getattr(error, "page_snapshot", None) or await _page_snapshot(page)
        if error.stage == "navigation" or blocked_document_url:
            classified = classify_tieba_failure(snapshot, status=status, blocked_document_url=blocked_document_url)
            if classified and classified not in {"tieba_dom_changed", "tieba_blank_page", "tieba_page_not_loaded"}:
                error.reason = classified
        error.diagnostics = sanitize_diagnostics(snapshot, pairs)
        error.diagnostics["http_status"] = status
        error.diagnostics["blocked_resources"] = dict(blocked_resources)
        if blocked_document_url:
            error.diagnostics["blocked_document_url"] = sanitize_diagnostics({"url": blocked_document_url}, pairs).get("url", "")
        error.diagnostics["debug_artifacts"] = await save_failure_artifacts(page, pairs=pairs, directory=DEBUG_DIRECTORY)
    except Exception:
        error.diagnostics["diagnostics_failed"] = True


async def _wait_for_first_post(page: object, timeout_ms: int, *, status: int | None = None) -> dict:
    snapshot = await _page_snapshot(page)
    reason = classify_tieba_failure(snapshot, status=status)
    # Known interstitials/errors cannot become a post just by waiting. A bare
    # loading shell or an unfamiliar DOM, however, gets a bounded readiness wait.
    if reason and reason not in {"tieba_dom_changed", "tieba_page_not_loaded", "tieba_blank_page", "tieba_first_post_timeout"}:
        error = TiebaBrowserNavigationError("贴吧页面无法显示主楼。", stage="page_check", reason=reason)
        error.page_snapshot = snapshot
        raise error
    if snapshot.get("has_first_post"):
        return snapshot
    try:
        handle = await page.wait_for_function(FIRST_POST_READY_SCRIPT, timeout=min(FIRST_POST_TIMEOUT_MS, timeout_ms))
        await handle.dispose()
    except Exception as exc:
        snapshot = await _page_snapshot(page)
        if snapshot.get("has_first_post"):
            return snapshot
        timed_out = "timeout" in type(exc).__name__.lower()
        reason = classify_tieba_failure(snapshot, status=status, wait_timed_out=timed_out)
        error = TiebaBrowserNavigationError(
            "贴吧首帖未能在限定时间内准备完成。",
            stage="wait_first_post",
            reason=reason or "tieba_dom_changed",
        )
        error.page_snapshot = snapshot
        raise error from exc
    return await _page_snapshot(page)


async def _capture_mobile_page_tiles(page: object, output_path: str, *, width: int, viewport_height: int, page_height: int, timeout_ms: int) -> None:
    if page_height <= 0 or page_height > MAX_BROWSER_PAGE_HEIGHT:
        raise TiebaBrowserNavigationError(
            "帖子页面过长，已停止网页截图。",
            stage="capture",
            reason="page_height",
        )
    try:
        await _capture_segmented_screenshot(
            page,
            output_path,
            width=width,
            viewport_height=viewport_height,
            page_height=page_height,
            timeout_ms=timeout_ms,
            hide_repeated_chrome_script=_HIDE_REPEATED_CHROME_SCRIPT,
            restore_repeated_chrome_script=_RESTORE_REPEATED_CHROME_SCRIPT,
            max_total_height=MAX_BROWSER_PAGE_HEIGHT,
        )
    except ScreenshotCaptureError as exc:
        if exc.reason == "page_height":
            raise TiebaBrowserNavigationError(
                "帖子页面过长，已停止网页截图。",
                stage="capture",
                reason="page_height",
            ) from exc
        if exc.reason == "dimensions":
            raise TiebaBrowserNavigationError(
                "移动网页截图尺寸异常。",
                stage="capture",
                reason="dimensions",
            ) from exc
        raise TiebaBrowserNavigationError(
            "移动网页截图无法继续拼接。",
            stage="capture",
            reason=exc.reason or "capture_failed",
        ) from exc


async def capture_tieba_webpage_screenshot(
    raw_url: str,
    *,
    cookie: str = "",
    output_path: str | os.PathLike[str] | None = None,
    viewport_width: int = DEFAULT_BROWSER_VIEWPORT_WIDTH,
    viewport_height: int = DEFAULT_BROWSER_VIEWPORT_HEIGHT,
    timeout_ms: int = DEFAULT_BROWSER_TIMEOUT_MS,
    browser_channel: str = "",
    executable_path: str | os.PathLike[str] | None = None,
    proxy_url: str = "",
    title: str = "",
    author: str = "",
    published_at: str = "",
) -> TiebaBrowserCaptureResult:
    url = normalize_tieba_browser_url(raw_url)
    source_url = url
    cookie_present = bool(str(cookie or "").strip())
    pairs = parse_tieba_cookie_header(cookie)
    cookie_names = {name.upper() for name, _value in pairs}
    bduss_found = "BDUSS" in cookie_names
    stoken_found = "STOKEN" in cookie_names
    if async_playwright is None:
        raise TiebaBrowserUnavailable(
            "浏览器截图功能不可用，请安装 Playwright 后重试。",
            cookie_present=cookie_present,
            bduss_found=bduss_found,
            stoken_found=stoken_found,
        )
    timeout = _bounded_timeout(timeout_ms)
    width = _bounded_width(viewport_width)
    try:
        height = min(1200, max(480, int(viewport_height)))
    except (TypeError, ValueError):
        height = DEFAULT_BROWSER_VIEWPORT_HEIGHT
    screenshot_path = os.fspath(output_path) if output_path is not None else ""
    own_output = output_path is None or not screenshot_path
    allocated: list[str] = []
    screenshot_paths: list[str] = []
    completed = False
    playwright = browser = context = page = None
    response_status = None
    blocked_document_url = ""
    blocked_resources: dict[str, int] = {}
    try:
        try:
            playwright = await async_playwright().start()
        except Exception as exc:
            raise TiebaBrowserUnavailable(
                "浏览器运行时启动失败，请检查 Playwright 安装。",
                reason="playwright_start_failed",
                cookie_present=cookie_present,
                bduss_found=bduss_found,
                stoken_found=stoken_found,
            ) from exc
        launch_kwargs: dict[str, object] = {"headless": True}
        if executable_path:
            launch_kwargs["executable_path"] = os.fspath(executable_path)
        elif browser_channel.strip().lower() in {"chrome", "msedge"}:
            launch_kwargs["channel"] = browser_channel.strip().lower()
        proxy = _playwright_proxy(proxy_url)
        if proxy:
            launch_kwargs["proxy"] = proxy
        try:
            browser = await playwright.chromium.launch(**launch_kwargs)
        except Exception as first_error:
            if executable_path or browser_channel.strip().lower() in {"chrome", "msedge"}:
                raise TiebaBrowserUnavailable(
                    "浏览器不可用，请检查浏览器安装或路径。",
                    reason="launch_failed",
                    cookie_present=cookie_present,
                    bduss_found=bduss_found,
                    stoken_found=stoken_found,
                ) from first_error
            for channel in ("chrome", "msedge"):
                try:
                    fallback = dict(launch_kwargs); fallback["channel"] = channel; fallback.pop("executable_path", None)
                    browser = await playwright.chromium.launch(**fallback)
                    break
                except Exception:
                    browser = None
            if browser is None:
                raise TiebaBrowserUnavailable(
                    "未找到可用的 Chromium 浏览器。",
                    reason="launch_failed",
                    cookie_present=cookie_present,
                    bduss_found=bduss_found,
                    stoken_found=stoken_found,
                ) from first_error
        device = dict(getattr(playwright, "devices", {}).get("iPhone 15", {}))
        device.pop("default_browser_type", None)
        device.update({"viewport": {"width": width, "height": height}, "screen": {"width": width, "height": height}, "is_mobile": True, "has_touch": True, "locale": "zh-CN", "service_workers": "block", "accept_downloads": False})
        device["device_scale_factor"] = DEVICE_SCALE_FACTOR
        try:
            context = await browser.new_context(**device)
        except Exception as exc:
            raise TiebaBrowserUnavailable(
                "浏览器上下文创建失败。",
                reason="context_failed",
                cookie_present=cookie_present,
                bduss_found=bduss_found,
                stoken_found=stoken_found,
            ) from exc
        if pairs:
            try:
                await context.add_cookies([{"name": name, "value": value, "url": f"https://{host}/", "httpOnly": True, "secure": True, "sameSite": "Lax"} for host in sorted(ALLOWED_TIEBA_HOSTS) for name, value in pairs])
            except Exception as exc:
                raise TiebaBrowserUnavailable(
                    "贴吧 Cookie 安装失败。",
                    reason="cookie_install_failed",
                    cookie_present=cookie_present,
                    bduss_found=bduss_found,
                    stoken_found=stoken_found,
                ) from exc
        try:
            page = await context.new_page()
            page.set_default_timeout(timeout)
        except Exception as exc:
            raise TiebaBrowserUnavailable(
                "浏览器页面创建失败。",
                reason="page_create_failed",
                cookie_present=cookie_present,
                bduss_found=bduss_found,
                stoken_found=stoken_found,
            ) from exc
        expected_id = _tieba_thread_id(url)
        if expected_id is None:
            raise TiebaBrowserUrlError("仅支持百度贴吧的 /p/数字 帖子链接。")

        async def route_handler(route: object, request: object) -> None:
            nonlocal blocked_document_url
            request_url = str(getattr(request, "url", "")); method = str(getattr(request, "method", "GET")).upper(); resource_type = str(getattr(request, "resource_type", ""))
            allowed_document = resource_type == "document" and getattr(request, "frame", None) == page.main_frame and _is_allowed_thread_document(request_url, expected_id)
            allowed_style = resource_type == "stylesheet" and _is_allowed_style_request(request_url)
            allowed_image = resource_type == "image" and _is_allowed_image_request(request_url)
            allowed_script = resource_type == "script" and _is_allowed_script_request(request_url)
            # Only a read of this very thread is allowed asynchronously; never
            # enable all same-origin APIs (which include account mutations).
            allowed_read = resource_type in {"xhr", "fetch"} and _is_allowed_thread_document(request_url, expected_id)
            if method == "GET" and (allowed_document or allowed_style or allowed_image or allowed_script or allowed_read):
                try:
                    host = (urlparse(request_url).hostname or "").lower().rstrip(".")
                except ValueError:
                    host = ""
                # Credentials are needed only for this thread document/read.
                # Strip them from every static request, including JS bundles,
                # even when the resource is served by tieba.baidu.com itself.
                if not (allowed_document or allowed_read) or host not in ALLOWED_TIEBA_HOSTS:
                    headers = dict(await request.all_headers())  # type: ignore[attr-defined]
                    headers.pop("cookie", None); headers.pop("Cookie", None)
                    await route.continue_(headers=headers)  # type: ignore[attr-defined]
                else:
                    await route.continue_()  # type: ignore[attr-defined]
            else:
                blocked_resources[resource_type] = blocked_resources.get(resource_type, 0) + 1
                if resource_type == "document" and getattr(request, "frame", None) == page.main_frame:
                    blocked_document_url = request_url
                await route.abort(error_code="blockedbyclient")  # type: ignore[attr-defined]
        try:
            await page.route("**/*", route_handler)
        except Exception as exc:
            raise TiebaBrowserNavigationError(
                "贴吧页面路由初始化失败。",
                reason="route_setup_failed",
                cookie_present=cookie_present,
                bduss_found=bduss_found,
                stoken_found=stoken_found,
            ) from exc
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            response_status = response.status if response is not None else None
        except Exception as exc:
            reason = (
                "tieba_page_not_loaded"
                if "timeout" in type(exc).__name__.lower()
                else "navigation_failed"
            )
            error_type = (
                TiebaBrowserTimeoutError if reason == "tieba_page_not_loaded" else TiebaBrowserNavigationError
            )
            raise error_type(
                "打开贴吧页面超时或失败，请稍后重试。",
                stage="navigation",
                reason=reason,
                cookie_present=cookie_present,
                bduss_found=bduss_found,
                stoken_found=stoken_found,
            ) from exc
        if not _is_allowed_thread_document(page.url, expected_id):
            raise TiebaBrowserNavigationError(
                "贴吧页面跳转到了不受支持的地址。",
                reason="redirect",
                cookie_present=cookie_present,
                bduss_found=bduss_found,
                stoken_found=stoken_found,
            )
        await _wait_for_first_post(page, timeout, status=response_status)
        try:
            info = await page.evaluate(_TRANSFORM_SCRIPT, {"sourceUrl": source_url, "suppliedTitle": str(title or "").strip()[:300], "suppliedAuthor": str(author or "").strip()[:120], "suppliedPublishedAt": str(published_at or "").strip()[:120]})
        except Exception as exc:
            raise TiebaBrowserNavigationError(
                "贴吧主楼 DOM 整理失败。",
                stage="transform",
                reason="tieba_dom_changed" if "NO_FIRST_POST" in str(exc) else "tieba_transform_failed",
                cookie_present=cookie_present,
                bduss_found=bduss_found,
                stoken_found=stoken_found,
            ) from exc
        if int(info.get("imageCount", 0)) > MAX_BROWSER_IMAGE_COUNT or int(info.get("nodeCount", 0)) > MAX_BROWSER_DOM_NODES:
            raise TiebaBrowserNavigationError(
                "帖子内容过大，已停止网页截图。",
                stage="transform",
                reason="content_limits",
                cookie_present=cookie_present,
                bduss_found=bduss_found,
                stoken_found=stoken_found,
            )
        try:
            state = await page.evaluate(_SCROLL_SCRIPT, {"maxImages": MAX_BROWSER_IMAGE_COUNT, "maxHeight": MAX_BROWSER_PAGE_HEIGHT, "perImageTimeoutMs": min(8_000, max(2_000, timeout // 8))})
        except Exception as exc:
            raise TiebaBrowserNavigationError(
                "页面内容准备失败。",
                stage="transform",
                reason="scroll_failed",
                cookie_present=cookie_present,
                bduss_found=bduss_found,
                stoken_found=stoken_found,
            ) from exc
        if bool(state.get("tooMany")) or bool(state.get("tooTall")) or int(state.get("pageHeight", 0)) > MAX_BROWSER_PAGE_HEIGHT:
            raise TiebaBrowserNavigationError(
                "帖子页面过长，已停止网页截图。",
                stage="transform",
                reason="page_limits",
                cookie_present=cookie_present,
                bduss_found=bduss_found,
                stoken_found=stoken_found,
            )
        try:
            stats = await page.evaluate(_FINALIZE_IMAGES_SCRIPT)
        except Exception as exc:
            raise TiebaBrowserNavigationError(
                "页面截图前整理失败。",
                stage="transform",
                reason="finalize_failed",
                cookie_present=cookie_present,
                bduss_found=bduss_found,
                stoken_found=stoken_found,
            ) from exc
        if int(stats.get("pageHeight", 0)) > MAX_BROWSER_PAGE_HEIGHT:
            raise TiebaBrowserNavigationError(
                "帖子页面过长，已停止网页截图。",
                stage="transform",
                reason="page_limits",
                cookie_present=cookie_present,
                bduss_found=bduss_found,
                stoken_found=stoken_found,
            )
        capture_height = int(stats.get("captureHeight", stats.get("pageHeight", 0)))
        current = screenshot_path
        if own_output or not current:
            fd, current = tempfile.mkstemp(prefix="tieba-browser-", suffix=".png"); os.close(fd); allocated.append(current)
        try:
            await _capture_mobile_page_tiles(page, current, width=width, viewport_height=height, page_height=capture_height, timeout_ms=timeout)
        except TiebaBrowserCaptureError:
            raise
        except Exception as exc:
            reason = (
                "timeout"
                if "timeout" in type(exc).__name__.lower()
                else "capture_failed"
            )
            error_type = (
                TiebaBrowserTimeoutError if reason == "timeout" else TiebaBrowserCaptureError
            )
            raise error_type(
                "生成贴吧网页截图超时或失败，请稍后重试。",
                stage="capture",
                reason=reason,
                cookie_present=cookie_present,
                bduss_found=bduss_found,
                stoken_found=stoken_found,
            ) from exc
        screenshot_paths.append(current)
        try:
            with Image.open(current) as raw_png:
                raw_png_width, raw_png_height = raw_png.size
        except Exception as exc:
            raise TiebaBrowserCaptureError(
                "生成的贴吧截图文件无效。",
                stage="capture",
                reason="invalid_png",
                cookie_present=cookie_present,
                bduss_found=bduss_found,
                stoken_found=stoken_found,
            ) from exc
        image_count = int(info.get("imageCount", 0)); loaded = int(stats.get("loaded", 0)); failed = int(stats.get("failed", 0)) + int(info.get("missingImageCount", 0))
        status = TiebaBrowserCaptureStatus.PARTIAL if failed or loaded < image_count else TiebaBrowserCaptureStatus.OK
        completed = True
        return TiebaBrowserCaptureResult(
            current,
            source_url,
            str(info.get("title", "百度贴吧帖子")),
            image_count,
            loaded,
            failed,
            status,
            tuple(screenshot_paths),
            cookie_present,
            bduss_found,
            stoken_found,
            width,
            DEVICE_SCALE_FACTOR,
            raw_png_width,
            raw_png_height,
        )
    except TiebaBrowserCaptureError as exc:
        _attach_cookie_diagnostics(
            exc,
            cookie_present=cookie_present,
            pairs=pairs,
        )
        await _attach_page_diagnostics(exc, page, pairs=pairs, status=response_status,
            blocked_document_url=blocked_document_url, blocked_resources=blocked_resources)
        if own_output:
            for path in dict.fromkeys((*allocated, *screenshot_paths)):
                try: os.remove(path)
                except OSError: pass
        raise
    except Exception as exc:
        if own_output:
            for path in dict.fromkeys((*allocated, *screenshot_paths)):
                try: os.remove(path)
                except OSError: pass
        error = TiebaBrowserCaptureError(
            "浏览器截图失败，请检查浏览器环境后重试。",
            stage="capture",
            reason="unexpected_failure",
            cookie_present=cookie_present,
            bduss_found=bduss_found,
            stoken_found=stoken_found,
        )
        await _attach_page_diagnostics(error, page, pairs=pairs, status=response_status,
            blocked_document_url=blocked_document_url, blocked_resources=blocked_resources)
        raise error from exc
    finally:
        for resource in (page, context, browser):
            if resource is not None:
                try: await resource.close()
                except Exception: pass
        if playwright is not None:
            try: await playwright.stop()
            except Exception: pass
        if own_output and not completed:
            for path in dict.fromkeys((*allocated, *screenshot_paths)):
                try: os.remove(path)
                except OSError: pass


async def capture_tieba_screenshot(*args: object, **kwargs: object) -> str:
    result = await capture_tieba_webpage_screenshot(*args, **kwargs)  # type: ignore[arg-type]
    return result.image_path


__all__ = [
    "TiebaBrowserCaptureError", "TiebaBrowserCookieError", "TiebaBrowserUnavailable", "TiebaBrowserUrlError", "TiebaBrowserNavigationError", "TiebaBrowserTimeoutError", "TiebaBrowserCaptureStatus", "TiebaBrowserCaptureResult", "capture_tieba_webpage_screenshot", "capture_tieba_screenshot", "normalize_tieba_browser_url", "normalize_tieba_url", "parse_tieba_cookie_header", "parse_tieba_browser_cookie_header", "_is_allowed_thread_document", "_is_allowed_style_request", "_is_allowed_image_request", "_playwright_proxy", "_TRANSFORM_SCRIPT", "_SCROLL_SCRIPT", "_FINALIZE_IMAGES_SCRIPT", "MAX_BROWSER_PAGE_HEIGHT", "MAX_BROWSER_IMAGE_COUNT", "MAX_BROWSER_DOM_NODES", "MAX_BROWSER_TOTAL_PIXELS",
]
