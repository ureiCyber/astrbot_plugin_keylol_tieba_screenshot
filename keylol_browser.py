"""Controlled iPhone-style renderer for Keylol first-page screenshots.

The renderer keeps Keylol's native mobile DOM and CSS, opens it in an isolated
iPhone 15 context, warms up lazy images by scrolling, and captures either one
page or each safe AJAX table-of-contents section as its own PNG.

The module does not log cookies or include network exception text in public
errors.  Callers can catch :class:`KeylolBrowserCaptureError` and fall back to
the existing renderer when Playwright is not installed or the browser path
cannot complete safely.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from enum import Enum
from io import BytesIO
from urllib.parse import parse_qsl, urlencode, urlparse

from PIL import Image

try:  # Optional dependency.  The existing non-browser renderer must continue to work without it.
    from playwright.async_api import async_playwright
except ImportError:  # pragma: no cover - exercised only in installations without Playwright.
    async_playwright = None  # type: ignore[assignment]


DEFAULT_BROWSER_VIEWPORT_WIDTH = 390
DEFAULT_BROWSER_VIEWPORT_HEIGHT = 844
DEFAULT_BROWSER_TIMEOUT_MS = 45_000
ALLOWED_KEYLOL_HOSTS = {"keylol.com", "www.keylol.com"}
ALLOWED_KEYLOL_IMAGE_HOSTS = {*ALLOWED_KEYLOL_HOSTS, "blob.keylol.com"}
MAX_BROWSER_PAGE_HEIGHT = 100_000
MAX_BROWSER_IMAGE_COUNT = 500
MAX_BROWSER_DOM_NODES = 20_000
DEFAULT_BROWSER_TOC_SECTIONS = 12
MAX_BROWSER_TOC_SECTIONS = 20
MAX_BROWSER_TOTAL_PIXELS = 120_000_000
_SHORT_FIRST_PAGE_RE = re.compile(r"^/t\d+-1-\d+/?$", re.IGNORECASE)
_MEDIA_VIDEO_RE = re.compile(r"\.(?:avi|flv|m4v|mkv|mov|mp4|ts|webm|wmv)(?:$|[?#])", re.I)
_MEDIA_AUDIO_RE = re.compile(r"\.(?:aac|amr|flac|m4a|mp3|ogg|opus|wav|wma)(?:$|[?#])", re.I)


class KeylolBrowserCaptureError(RuntimeError):
    """Base class for safe, user-facing browser capture failures."""


class KeylolBrowserUnavailable(KeylolBrowserCaptureError):
    """Raised when Playwright is not installed or its browser is unavailable."""


class KeylolBrowserUrlError(KeylolBrowserCaptureError):
    """Raised when a URL is not an allowed first-page Keylol thread URL."""


class KeylolBrowserNavigationError(KeylolBrowserCaptureError):
    """Raised when the isolated browser cannot open a suitable Keylol page."""


class KeylolBrowserTimeoutError(KeylolBrowserCaptureError):
    """Raised when navigation or capture exceeds the bounded timeout."""


class KeylolBrowserCaptureStatus(str, Enum):
    """Outcome quality reported by :func:`capture_keylol_webpage_screenshot`."""

    OK = "ok"
    PARTIAL = "partial"


@dataclass(frozen=True, slots=True)
class KeylolBrowserCaptureResult:
    """Metadata for a successfully written browser screenshot."""

    image_path: str
    source_url: str
    title: str
    image_count: int
    loaded_image_count: int
    failed_image_count: int
    status: KeylolBrowserCaptureStatus
    image_paths: tuple[str, ...] = ()
    section_titles: tuple[str, ...] = ()


def normalize_keylol_browser_url(raw_url: str) -> str:
    """Validate and normalize a first-page short or Discuz thread URL.

    Only ``keylol.com`` and ``www.keylol.com`` are accepted.  Credentials,
    custom ports, fragments, and page numbers other than one are rejected.
    The short form used by Keylol (``/t123-1-1``) is preferred, while the
    ordinary Discuz ``forum.php?mod=viewthread&tid=...&page=1`` form is also
    accepted for callers that already have it.
    """

    value = str(raw_url or "").strip().strip("<>")
    if not value:
        raise KeylolBrowserUrlError("请提供其乐帖子链接。")
    if "://" not in value:
        value = f"https://{value}"
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError as exc:
        raise KeylolBrowserUrlError("其乐帖子链接格式无效。") from exc

    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme.lower() not in {"http", "https"} or host not in ALLOWED_KEYLOL_HOSTS:
        raise KeylolBrowserUrlError("仅支持 keylol.com 的 http/https 帖子链接。")
    if parsed.username or parsed.password or port or parsed.fragment:
        raise KeylolBrowserUrlError("链接必须是无账号信息的其乐第一页帖子链接。")

    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    query_values = {key.lower(): value for key, value in query_pairs}
    short_page = _SHORT_FIRST_PAGE_RE.fullmatch(parsed.path or "")
    if short_page:
        if query_values.get("page", "1") not in {"", "1"}:
            raise KeylolBrowserUrlError("浏览器截图只支持帖子的第一页。")
        query = [
            (key, value)
            for key, value in query_pairs
            if key.lower() not in {"mobile", "page"}
        ]
        return parsed._replace(scheme="https", query=urlencode(query), fragment="").geturl()

    if (parsed.path or "").rstrip("/").lower() != "/forum.php":
        raise KeylolBrowserUrlError("仅支持其乐帖子的第一页链接。")
    if (
        query_values.get("mod", "").lower() != "viewthread"
        or not query_values.get("tid", "").isdigit()
    ):
        raise KeylolBrowserUrlError("仅支持其乐帖子的第一页链接。")
    if query_values.get("page", "1") not in {"", "1"}:
        raise KeylolBrowserUrlError("浏览器截图只支持帖子的第一页。")
    query_items = [
        (key, value) for key, value in query_pairs if key.lower() != "mobile"
    ]
    return parsed._replace(scheme="https", query=urlencode(query_items), fragment="").geturl()


def _keylol_thread_id(url: str) -> str | None:
    """Return a numeric thread id from a normalized Keylol thread URL."""

    parsed = urlparse(url)
    match = re.fullmatch(r"/t(\d+)-\d+-\d+/?", parsed.path or "", re.I)
    if match:
        return match.group(1)
    query = {key.lower(): value for key, value in parse_qsl(parsed.query)}
    if (
        (parsed.path or "").rstrip("/").lower() == "/forum.php"
        and query.get("mod", "").lower() == "viewthread"
        and query.get("tid", "").isdigit()
    ):
        return query["tid"]
    return None


def _display_source_url(url: str) -> str:
    """Remove the internal Discuz desktop-view flag from the visible source."""

    parsed = urlparse(url)
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() != "mobile"
    ]
    return parsed._replace(query=urlencode(query), fragment="").geturl()


def _is_safe_https_url(value: str, allowed_hosts: set[str]) -> bool:
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    return bool(
        parsed.scheme.lower() == "https"
        and host in allowed_hosts
        and not parsed.username
        and not parsed.password
        and port is None
    )


def _is_allowed_thread_document(value: str, expected_thread_id: str) -> bool:
    """Allow only the requested thread's main-frame document redirects."""

    if not _is_safe_https_url(value, ALLOWED_KEYLOL_HOSTS):
        return False
    try:
        normalized = normalize_keylol_browser_url(value)
    except KeylolBrowserUrlError:
        return False
    return _keylol_thread_id(normalized) == expected_thread_id


def _is_allowed_image_request(value: str) -> bool:
    """Restrict browser image requests to known Keylol asset endpoints."""

    if not _is_safe_https_url(value, ALLOWED_KEYLOL_IMAGE_HOSTS):
        return False
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower().rstrip(".")
    path = (parsed.path or "").lower()
    if host == "blob.keylol.com":
        return path.startswith("/forum/") and bool(
            re.search(r"\.(?:avif|gif|jpe?g|png|webp)$", path, re.I)
        )
    query = {key.lower(): value for key, value in parse_qsl(parsed.query)}
    if path.rstrip("/") == "/forum.php" and query.get("mod", "").lower() == "attachment":
        return True
    # Native mobile templates also use images below ``/template`` and plugin
    # asset paths.  The exact Keylol hosts are already enforced above; allow
    # ordinary image files there so a direct webpage capture keeps its chrome.
    return bool(re.search(r"\.(?:avif|gif|jpe?g|png|svg|webp)$", path, re.I))


def _bounded_toc_sections(value: object) -> int:
    try:
        count = int(value)
    except (TypeError, ValueError):
        count = DEFAULT_BROWSER_TOC_SECTIONS
    return min(MAX_BROWSER_TOC_SECTIONS, max(1, count))


def _is_allowed_toc_request(
    value: str,
    expected_thread_id: str,
    expected_viewpid: str,
    expected_cp: str,
) -> bool:
    """Allow only the exact same-thread AJAX request used by a TOC item."""

    if not _is_safe_https_url(value, ALLOWED_KEYLOL_HOSTS):
        return False
    parsed = urlparse(value)
    if (parsed.path or "").rstrip("/").lower() != "/forum.php":
        return False
    query = {key.lower(): value for key, value in parse_qsl(parsed.query)}
    return (
        query.get("mod", "").lower() == "viewthread"
        and query.get("threadindex", "").lower() == "yes"
        and query.get("tid") == str(expected_thread_id)
        and query.get("viewpid") == str(expected_viewpid)
        and query.get("cp") == str(expected_cp)
        and query.get("inajax") == "1"
        and query.get("ajaxtarget") == f"pid{expected_viewpid}"
        and set(query).issubset(
            {"mod", "threadindex", "tid", "viewpid", "cp", "inajax", "ajaxtarget"}
        )
    )


_COOKIE_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_COOKIE_ATTRIBUTES = {
    "domain",
    "expires",
    "httponly",
    "max-age",
    "path",
    "samesite",
    "secure",
}


def parse_keylol_cookie_header(cookie: str) -> list[tuple[str, str]]:
    """Parse a Cookie header without retaining domain attributes.

    The returned name/value pairs are intended to be installed as host-only
    Playwright cookies on ``keylol.com`` and ``www.keylol.com`` separately.
    This prevents a Keylol login cookie from being sent to ``blob.keylol.com``
    or another asset/CDN subdomain.
    """

    raw = str(cookie or "").strip()
    if not raw:
        return []
    if "\r" in raw or "\n" in raw:
        raise KeylolBrowserCaptureError("Cookie 配置格式无效。")
    if raw.lower().startswith("cookie:"):
        raw = raw[7:].strip()

    result: list[tuple[str, str]] = []
    for piece in raw.split(";"):
        item = piece.strip()
        if not item:
            continue
        if "=" not in item:
            if item.lower() in _COOKIE_ATTRIBUTES:
                continue
            # Netscape-style flags and stray attributes are not safe to guess.
            raise KeylolBrowserCaptureError("Cookie 配置格式无效。")
        name, value = item.split("=", 1)
        name = name.strip()
        value = value.strip()
        if name.lower() in _COOKIE_ATTRIBUTES or name.startswith("$"):
            continue
        if not _COOKIE_NAME_RE.fullmatch(name) or "\x00" in value:
            raise KeylolBrowserCaptureError("Cookie 配置格式无效。")
        result.append((name, value))
    return result


def _bounded_timeout(value: object) -> int:
    try:
        timeout = int(value)
    except (TypeError, ValueError):
        timeout = DEFAULT_BROWSER_TIMEOUT_MS
    return min(120_000, max(5_000, timeout))


def _bounded_width(value: object) -> int:
    try:
        width = int(value)
    except (TypeError, ValueError):
        width = DEFAULT_BROWSER_VIEWPORT_WIDTH
    return min(440, max(320, width))


def _playwright_proxy(value: str) -> dict[str, str] | None:
    """Convert a configured proxy URL into Playwright fields without logging it."""

    raw = str(value or "").strip()
    if not raw:
        return None
    if "\r" in raw or "\n" in raw:
        raise KeylolBrowserCaptureError("代理配置格式无效。")
    try:
        parsed = urlparse(raw)
    except ValueError as exc:
        raise KeylolBrowserCaptureError("代理配置格式无效。") from exc
    if (
        parsed.scheme.lower() not in {"http", "https", "socks5"}
        or not parsed.hostname
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise KeylolBrowserCaptureError("代理配置格式无效。")
    try:
        port = parsed.port
    except ValueError as exc:
        raise KeylolBrowserCaptureError("代理配置格式无效。") from exc
    server = f"{parsed.scheme.lower()}://{parsed.hostname}"
    if port:
        server += f":{port}"
    proxy: dict[str, str] = {"server": server}
    if parsed.username:
        proxy["username"] = parsed.username
    if parsed.password:
        proxy["password"] = parsed.password
    return proxy


_TOC_DISCOVERY_SCRIPT = r"""
() => {
  const text = (node) => (node && node.textContent || "").replace(/\s+/g, " ").trim();
  const result = [];
  const nativeItems = [...document.querySelectorAll("#threadindex .tindex li")];
  const items = nativeItems.length ? nativeItems : [...document.querySelectorAll("#threadindex a[page]")];
  const pathMatch = location.pathname.match(/^\/t(\d+)-/i);
  const fallbackTid = pathMatch ? pathMatch[1] : new URLSearchParams(location.search).get("tid") || "";
  const pidNode = document.querySelector("article.plc[id^='pid'], article.plc");
  const fallbackViewpid = pidNode && (pidNode.id.match(/(\d+)/) || [])[1] || "";
  for (const [index, item] of items.entries()) {
    const onclick = String(item.getAttribute("onclick") || "");
    const match = onclick.match(/forum\.php\?([^'"\s)]+)/i);
    const params = match ? new URLSearchParams(match[1].replace(/&amp;/g, "&")) : null;
    const cp = params ? params.get("cp") || "" : item.getAttribute("page") || "";
    const tid = params ? params.get("tid") || "" : fallbackTid;
    const viewpid = params ? params.get("viewpid") || "" : fallbackViewpid;
    if (params && (params.get("mod") !== "viewthread" || params.get("threadindex") !== "yes")) continue;
    if (!/^\d+$/.test(cp) || !/^\d+$/.test(tid) || !/^\d+$/.test(viewpid)) continue;
    result.push({index, title: text(item), cp, tid, viewpid});
  }
  return result;
}
"""


_TOC_CLICK_SCRIPT = r"""
async ({index, tid, viewpid, cp}) => {
  const item = document.querySelectorAll("#threadindex .tindex li")[index];
  const fallback = document.querySelectorAll("#threadindex a[page]")[index];
  const targetItem = item || fallback;
  if (!targetItem) throw new Error("NO_TOC_ITEM");
  // Use the exact endpoint parsed from the directory instead of depending on
  // Keylol's external JavaScript.  This keeps section loading deterministic
  // while the isolated capture context blocks unrelated scripts.
  const endpoint = new URL("/forum.php", location.origin);
  endpoint.search = new URLSearchParams({mod: "viewthread", threadindex: "yes", tid, viewpid, cp, inajax: "1", ajaxtarget: `pid${viewpid}`}).toString();
  const response = await fetch(endpoint.href, {credentials: "same-origin"});
  if (!response.ok) throw new Error("TOC_REQUEST_FAILED");
  let html = await response.text();
  const cdata = html.match(/<!\[CDATA\[([\s\S]*?)\]\]>/i);
  if (cdata) html = cdata[1];
  const incoming = new DOMParser().parseFromString(html, "text/html");
  const target = document.getElementById(`pid${viewpid}`);
  const replacement = incoming.querySelector(`article.plc#pid${viewpid}`) || incoming.querySelector("article.plc") || incoming.querySelector("section.postlist");
  if (!target || !replacement) throw new Error("TOC_CONTENT_MISSING");
  if (replacement.matches("article.plc")) target.replaceWith(replacement);
  else target.innerHTML = replacement.innerHTML;
  return "fetched";
}
"""


_TRANSFORM_SCRIPT = r"""
async ({sourceUrl, viewportWidth, suppliedTitle, suppliedAuthor, suppliedPublishedAt, sectionTitle, hideToc}) => {
  const placeholder = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==";
  const imageAttrs = ["zoomfile", "file", "data-original", "data-src", "data-lazy-src", "data-actualsrc", "data-lazyload", "data-zoomfile", "data-file", "data-url", "data-image-url", "data-ori-src", "origin-src", "picurl", "src"];
  const badImage = /(?:none\.gif|attachimg|folder_|filetype\/|loading(?:[._-]|\.gif))/i;
  const text = (node) => (node && node.textContent || "").replace(/\s+/g, " ").trim();
  const firstText = (root, selectors) => {
    for (const selector of selectors) { const value = text(root.querySelector(selector)); if (value) return value; }
    return "";
  };
  const allowedHost = (hostname) => ["keylol.com", "www.keylol.com", "blob.keylol.com"].includes(hostname.toLowerCase().replace(/\.$/, ""));
  const absolute = (value) => {
    if (!value || /^javascript:/i.test(value)) return "";
    try {
      const url = new URL(String(value).trim(), location.href);
      return url.protocol === "https:" && allowedHost(url.hostname) && !url.username && !url.password && !url.port ? url.href : "";
    } catch (_) { return ""; }
  };
  const article = document.querySelector("article.plc");
  const content = article && article.querySelector(".message");
  if (!article || !content) throw new Error("NO_FIRST_POST");
  article.dataset.keylolCaptureArticle = "1";
  for (const node of document.querySelectorAll("article.plc")) if (node !== article) node.style.display = "none";
  if (hideToc) for (const node of document.querySelectorAll("#threadindex, .tindex")) node.style.display = "none";
  for (const node of document.querySelectorAll(".pgs, .rnd_ai_pr, .a_mu, .pstatus, .jammer, form, script, noscript")) node.remove();
  const firstFloor = content.closest(".first_follor");
  if (firstFloor) for (const node of [...firstFloor.children]) if (node !== content) node.style.display = "none";
  const keepArticleNode = (node) => node === firstFloor || node === content || node.matches(".avatar, .display.pi");
  for (const node of [...article.children]) if (!keepArticleNode(node)) node.style.display = "none";
  for (let node = article.nextElementSibling; node; node = node.nextElementSibling) node.style.display = "none";
  const postList = article.closest("section.postlist");
  if (postList) for (let node = postList.nextElementSibling; node; node = node.nextElementSibling) node.style.display = "none";
  for (const node of article.querySelectorAll("*")) for (const attribute of [...node.attributes]) if (/^on/i.test(attribute.name) || ["srcdoc", "formaction"].includes(attribute.name.toLowerCase())) node.removeAttribute(attribute.name);
  for (const media of article.querySelectorAll("video, audio")) { media.pause(); media.removeAttribute("autoplay"); }

  const title = suppliedTitle || firstText(document, ["#thread_subject", "h1.ts", "h1", "title"]) || "其乐帖子";
  const post = article;
  const author = suppliedAuthor || firstText(post, [".authi a", ".authi .xi2", ".author", ".xg1"]) || firstText(document, [".authi a", ".author"]);
  const publishedAt = suppliedPublishedAt || firstText(post, [".authi em", ".pti .authi", "time"]) || firstText(document, [".authi em", "time"]);
  const oldFooter = article.querySelector(".keylol-capture-footer"); if (oldFooter) oldFooter.remove();
  const footer = document.createElement("footer"); footer.className = "keylol-capture-footer";
  const footerTitle = document.createElement("strong"); footerTitle.textContent = sectionTitle ? `目录：${sectionTitle}` : "截图信息"; footer.append(footerTitle);
  const meta = document.createElement("div"); meta.textContent = [title, author, publishedAt].filter(Boolean).join(" · "); footer.append(meta);
  const source = document.createElement("a"); source.href = sourceUrl; source.textContent = sourceUrl; source.rel = "noopener noreferrer"; footer.append(source);
  content.append(footer);

  const style = document.createElement("style"); style.id = "keylol-browser-capture-style";
  style.textContent = `
    .keylol-capture-footer { clear: both !important; display: block !important; position: static !important; margin: 24px 0 8px; padding: 14px 0 4px; border-top: 1px solid #dfe3e8; color: #68717d; font-size: 12px; line-height: 1.55; overflow-wrap: anywhere; }
    .keylol-capture-footer strong { display: block; color: #20242a; font-size: 14px; }
    .keylol-capture-footer a { display: block; color: #1769aa; text-decoration: none; }
    .keylol-browser-media-card { margin: 12px 0; padding: 12px; border: 1px solid #dfe3e8; border-radius: 6px; background: #f6f8fa; color: #59636e; font-size: 13px; line-height: 1.5; overflow-wrap: anywhere; }
    .keylol-browser-image-failed { border-color: #efd4d4; background: #fff7f7; color: #9a3b3b; }
  `;
  const previousStyle = document.getElementById("keylol-browser-capture-style"); if (previousStyle) previousStyle.remove(); document.head.append(style);
  const mediaCard = (kind, label) => { const card = document.createElement("div"); card.className = `keylol-browser-media-card keylol-browser-media-${kind}`; card.textContent = `${kind === "video" ? "视频内容" : "音频内容"}${label ? ` · ${label.slice(0, 120)}` : ""}（静态截图无法播放）`; return card; };
  for (const media of [...article.querySelectorAll("iframe, embed, object")]) media.replaceWith(mediaCard("video", media.getAttribute("title") || ""));
  let imageCount = 0, missingImageCount = 0;
  const missingImageCard = (label) => { const card = document.createElement("div"); card.className = "keylol-browser-media-card keylol-browser-image-failed"; card.textContent = `图片地址不可用${label ? ` · ${label.slice(0, 120)}` : ""}`; return card; };
  for (const image of [...content.querySelectorAll("img")]) {
    if (image.dataset.keylolLoaded === "1" || image.dataset.keylolCandidates) { image.loading = "eager"; continue; }
    const values = [];
    const nearby = image.closest(".pattl, .attachlist, dl.tattl, .ignore_js_op");
    for (const attr of imageAttrs) { const value = image.getAttribute(attr); if (value) values.push(value); }
    if (nearby) for (const a of nearby.querySelectorAll('a[href*="mod=attachment"]')) values.push(a.getAttribute("href"));
    const srcset = image.getAttribute("srcset") || image.getAttribute("data-srcset") || "";
    for (const value of srcset.split(",").map((item) => item.trim().split(/\s+/)[0]).reverse()) if (value) values.push(value);
    const candidates = []; for (const value of values) { if (badImage.test(value)) continue; const candidate = absolute(value); if (candidate && !candidates.includes(candidate)) candidates.push(candidate); }
    const current = absolute(image.currentSrc || image.src || "");
    if (current && !badImage.test(current) && image.complete && image.naturalWidth > 1) {
      image.dataset.keylolLoaded = "1";
      image.loading = "eager";
      imageCount++;
      continue;
    }
    if (!candidates.length) {
      if (values.some((value) => badImage.test(value))) image.remove();
      else { image.replaceWith(missingImageCard(image.alt || "")); missingImageCount++; }
      continue;
    }
    image.dataset.keylolCandidates = JSON.stringify(candidates); image.dataset.keylolPending = "1"; image.src = placeholder; image.removeAttribute("srcset"); image.removeAttribute("data-srcset"); for (const attr of imageAttrs) if (attr !== "src") image.removeAttribute(attr); image.loading = "eager"; image.decoding = "async"; image.referrerPolicy = "strict-origin-when-cross-origin"; imageCount++;
  }
  return {title, imageCount, missingImageCount, nodeCount: article.querySelectorAll("*").length};
}
"""


_SCROLL_SCRIPT = r"""
async ({maxImages, maxHeight, perImageTimeoutMs}) => {
  const placeholder = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==";
  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const warmPage = async () => {
    let stableBottoms = 0;
    let steps = 0;
    while (steps++ < 320) {
      const height = document.documentElement.scrollHeight;
      if (height > maxHeight) return {tooTall: true};
      const bottom = Math.max(0, height - window.innerHeight);
      const next = Math.min(bottom, window.scrollY + Math.max(360, Math.floor(window.innerHeight * 0.8)));
      window.scrollTo(0, next);
      await wait(60);
      const expandedHeight = document.documentElement.scrollHeight;
      if (next >= bottom - 1 && expandedHeight <= height + 1) stableBottoms++;
      else stableBottoms = 0;
      if (stableBottoms >= 2) break;
    }
    return {tooTall: document.documentElement.scrollHeight > maxHeight};
  };

  const firstWarmup = await warmPage();
  if (firstWarmup.tooTall) {
    window.scrollTo(0, 0);
    return {pageHeight: document.documentElement.scrollHeight, tooMany: false, tooTall: true};
  }
  const images = [...document.querySelectorAll('img[data-keylol-candidates]')];
  if (images.length > maxImages) {
    return {pageHeight: document.documentElement.scrollHeight, tooMany: true, tooTall: false};
  }

  const loadOnce = (image, source) => new Promise((resolve) => {
    let settled = false;
    const finish = (ok) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      image.onload = null;
      image.onerror = null;
      resolve(Boolean(ok));
    };
    const timer = setTimeout(() => finish(false), perImageTimeoutMs);
    image.onload = () => finish(image.naturalWidth > 0);
    image.onerror = () => finish(false);
    image.src = source;
    if (image.complete) queueMicrotask(() => finish(image.naturalWidth > 0));
  });

  let loaded = 0;
  let failed = 0;
  for (const image of images) {
    image.scrollIntoView({block: "center", inline: "nearest"});
    await wait(40);
    let candidates = [];
    try {
      const parsed = JSON.parse(image.dataset.keylolCandidates || "[]");
      if (Array.isArray(parsed)) candidates = parsed;
    } catch (_) {}
    let ok = false;
    for (const source of candidates) {
      for (let attempt = 0; attempt < 2 && !ok; attempt++) {
        ok = await loadOnce(image, source);
        if (!ok) {
          image.src = placeholder;
          await wait(220 * (attempt + 1));
        }
      }
      if (ok) break;
    }
    delete image.dataset.keylolCandidates;
    delete image.dataset.keylolPending;
    if (ok) {
      image.dataset.keylolLoaded = "1";
      loaded++;
    } else {
      image.dataset.keylolFailed = "1";
      failed++;
    }
    if (document.documentElement.scrollHeight > maxHeight) {
      return {
        pageHeight: document.documentElement.scrollHeight,
        loaded,
        failed,
        tooMany: false,
        tooTall: true
      };
    }
    await wait(80);
  }
  const finalWarmup = await warmPage();
  window.scrollTo(0, 0);
  await wait(100);
  return {
    pageHeight: document.documentElement.scrollHeight,
    loaded,
    failed,
    tooMany: false,
    tooTall: Boolean(finalWarmup.tooTall)
  };
}
"""


_FINALIZE_IMAGES_SCRIPT = r"""
() => {
  const article = document.querySelector('article[data-keylol-capture-article="1"]');
  const root = article && article.querySelector('.message') || article || document;
  let failed = 0;
  for (const image of [...root.querySelectorAll("img")]) {
    if (image.complete && image.naturalWidth > 0) continue;
    failed++;
    const card = document.createElement("div");
    card.className = "keylol-browser-media-card keylol-browser-image-failed";
    const strong = document.createElement("strong");
    strong.textContent = "图片加载失败";
    card.append(strong);
    const note = document.createElement("span");
    note.textContent = image.alt ? ` · ${image.alt.slice(0, 120)}` : "";
    card.append(note);
    image.replaceWith(card);
  }
  return {
    loaded: [...root.querySelectorAll("img")].filter((image) =>
      image.dataset.keylolLoaded === "1" && image.complete && image.naturalWidth > 0
    ).length,
    failed,
    ...(() => {
      const footer = article && article.querySelector('.keylol-capture-footer');
      const footerBottom = footer ? Math.ceil(footer.getBoundingClientRect().bottom + window.scrollY + 8) : 0;
      const articleBottom = article ? Math.ceil(article.getBoundingClientRect().bottom + window.scrollY + 8) : 0;
      const requiredBottom = Math.max(footerBottom, articleBottom);
      if (requiredBottom > 0) document.body.style.minHeight = `${requiredBottom}px`;
      return {
        pageHeight: document.documentElement.scrollHeight,
        captureHeight: requiredBottom || document.documentElement.scrollHeight
      };
    })()
  };
}
"""


_HIDE_REPEATED_CHROME_SCRIPT = r"""
() => {
  const attribute = "data-keylol-capture-repeated-chrome";
  for (const node of document.body.querySelectorAll("*")) {
    const css = getComputedStyle(node);
    if (css.position !== "fixed" && css.position !== "sticky") continue;
    const rect = node.getBoundingClientRect();
    if (css.position === "fixed" || (rect.bottom > 0 && rect.top < Math.min(140, innerHeight))) {
      node.setAttribute(attribute, "1");
    }
  }
  let style = document.getElementById("keylol-hide-repeated-chrome");
  if (!style) {
    style = document.createElement("style");
    style.id = "keylol-hide-repeated-chrome";
    style.textContent = `[${attribute}="1"] { visibility: hidden !important; }`;
    document.head.append(style);
  }
}
"""


_RESTORE_REPEATED_CHROME_SCRIPT = r"""
() => {
  document.getElementById("keylol-hide-repeated-chrome")?.remove();
  for (const node of document.querySelectorAll("[data-keylol-capture-repeated-chrome]")) {
    node.removeAttribute("data-keylol-capture-repeated-chrome");
  }
  window.scrollTo(0, 0);
}
"""


async def _capture_mobile_page_tiles(
    page: object,
    output_path: str,
    *,
    width: int,
    viewport_height: int,
    page_height: int,
    timeout_ms: int,
) -> None:
    """Capture a long page without changing its iPhone viewport height.

    Playwright's Chromium full-page mode temporarily expands a mobile viewport,
    which can reflow Keylol's ``vh``-based layout and clip the real tail.  Fixed
    viewport tiles preserve the native rendering and also exercise scrolling.
    """

    if page_height <= 0 or page_height > MAX_BROWSER_PAGE_HEIGHT:
        raise KeylolBrowserNavigationError("帖子页面过长，已停止网页截图并准备回退。")
    canvas = Image.new("RGB", (width, page_height), "white")
    covered = 0
    first_tile = True
    max_scroll = max(0, page_height - viewport_height)
    try:
        while covered < page_height:
            target_y = min(covered, max_scroll)
            actual_y = int(
                await page.evaluate(  # type: ignore[attr-defined]
                    "(value) => { window.scrollTo(0, value); return window.scrollY; }",
                    target_y,
                )
            )
            await page.wait_for_timeout(80)  # type: ignore[attr-defined]
            png = await page.screenshot(  # type: ignore[attr-defined]
                type="png",
                animations="disabled",
                caret="hide",
                scale="css",
                timeout=timeout_ms,
            )
            with Image.open(BytesIO(png)) as opened:
                tile = opened.convert("RGB")
            if tile.width != width or tile.height < 1:
                raise KeylolBrowserNavigationError("移动网页截图尺寸异常，已停止处理。")
            crop_top = max(0, covered - actual_y)
            crop_bottom = min(tile.height, page_height - actual_y)
            if crop_bottom <= crop_top:
                raise KeylolBrowserNavigationError("移动网页截图无法继续拼接。")
            canvas.paste(tile.crop((0, crop_top, width, crop_bottom)), (0, actual_y + crop_top))
            next_covered = actual_y + crop_bottom
            if next_covered <= covered:
                raise KeylolBrowserNavigationError("移动网页截图无法继续拼接。")
            covered = next_covered
            if first_tile:
                await page.evaluate(_HIDE_REPEATED_CHROME_SCRIPT)  # type: ignore[attr-defined]
                first_tile = False
        canvas.save(output_path, format="PNG", compress_level=6)
    finally:
        canvas.close()
        try:
            await page.evaluate(_RESTORE_REPEATED_CHROME_SCRIPT)  # type: ignore[attr-defined]
        except Exception:
            pass


async def capture_keylol_webpage_screenshot(
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
    split_toc_sections: bool = True,
    max_toc_sections: int = DEFAULT_BROWSER_TOC_SECTIONS,
) -> KeylolBrowserCaptureResult:
    """Capture the first post of a logged Keylol page as a mobile PNG.

    A new Playwright instance, browser, context, and page are created for each
    call.  Cookies are installed as host-only cookies for the two Keylol web
    hosts, so they cannot be sent to CDN subdomains.  The returned path belongs
    to the caller and is not removed by this function.
    """

    url = normalize_keylol_browser_url(raw_url)
    source_url = _display_source_url(url)
    toc_limit = _bounded_toc_sections(max_toc_sections)
    if async_playwright is None:
        raise KeylolBrowserUnavailable("浏览器截图功能不可用，请安装 Playwright 后重试。")

    timeout = _bounded_timeout(timeout_ms)
    width = _bounded_width(viewport_width)
    try:
        height = int(viewport_height)
    except (TypeError, ValueError):
        height = DEFAULT_BROWSER_VIEWPORT_HEIGHT
    height = min(1200, max(480, height))

    own_output = output_path is None
    screenshot_path = os.fspath(output_path) if output_path is not None else ""
    screenshot_paths: list[str] = []
    allocated_paths: list[str] = []

    playwright = browser = context = page = None
    try:
        playwright = await async_playwright().start()
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
        except Exception as first_launch_error:
            if executable_path or browser_channel.strip().lower() in {"chrome", "msedge"}:
                raise KeylolBrowserUnavailable("浏览器不可用，请检查浏览器安装或路径。") from first_launch_error
            browser = None
            for channel in ("chrome", "msedge"):
                try:
                    fallback_kwargs = dict(launch_kwargs)
                    fallback_kwargs["channel"] = channel
                    fallback_kwargs.pop("executable_path", None)
                    browser = await playwright.chromium.launch(**fallback_kwargs)
                    break
                except Exception:
                    continue
            if browser is None:
                raise KeylolBrowserUnavailable("未找到可用的 Chromium 浏览器。") from first_launch_error
        device = dict(getattr(playwright, "devices", {}).get("iPhone 15", {}))
        device.pop("default_browser_type", None)
        device["viewport"] = {"width": width, "height": height}
        device["screen"] = {"width": width, "height": height}
        device["is_mobile"] = True
        device["has_touch"] = True
        device["device_scale_factor"] = max(1, int(device.get("device_scale_factor", 3)))
        device["locale"] = "zh-CN"
        device["service_workers"] = "block"
        device["accept_downloads"] = False
        context = await browser.new_context(**device)
        pairs = parse_keylol_cookie_header(cookie)
        if pairs:
            await context.add_cookies(
                [
                    {
                        "name": name,
                        "value": value,
                        "url": f"https://{host}/",
                        "httpOnly": True,
                        "secure": True,
                        "sameSite": "Lax",
                    }
                    for host in sorted(ALLOWED_KEYLOL_HOSTS)
                    for name, value in pairs
                ]
            )
        page = await context.new_page()
        page.set_default_timeout(timeout)
        expected_thread_id = _keylol_thread_id(url)
        if expected_thread_id is None:
            raise KeylolBrowserUrlError("仅支持其乐帖子的第一页链接。")
        allowed_toc_sections: set[tuple[str, str]] = set()

        async def _route(route: object, request: object) -> None:
            request_url = str(getattr(request, "url", ""))
            resource_type = str(getattr(request, "resource_type", ""))
            method = str(getattr(request, "method", "GET")).upper()
            parsed_request = None
            try:
                parsed_request = urlparse(request_url)
                request_host = (parsed_request.hostname or "").lower().rstrip(".")
            except ValueError:
                request_host = ""
            request_frame = getattr(request, "frame", None)
            allowed_document = (
                resource_type == "document"
                and request_frame == page.main_frame
                and _is_allowed_thread_document(request_url, expected_thread_id)
            )
            allowed_style = (
                resource_type == "stylesheet"
                and parsed_request is not None
                and _is_safe_https_url(request_url, ALLOWED_KEYLOL_HOSTS)
                and (parsed_request.path or "").lower().endswith(".css")
            )
            allowed_image = resource_type == "image" and _is_allowed_image_request(
                request_url
            )
            allowed_toc = False
            if method == "GET" and resource_type in {"xhr", "fetch"}:
                try:
                    request_query = {
                        key.lower(): value
                        for key, value in parse_qsl(parsed_request.query)
                    } if parsed_request is not None else {}
                    allowed_toc = any(
                        _is_allowed_toc_request(request_url, expected_thread_id, viewpid, cp)
                        for cp, viewpid in allowed_toc_sections
                    )
                except Exception:
                    allowed_toc = False
            if method == "GET" and (allowed_document or allowed_style or allowed_image or allowed_toc):
                if request_host not in ALLOWED_KEYLOL_HOSTS:
                    headers = dict(await request.all_headers())  # type: ignore[attr-defined]
                    headers.pop("cookie", None)
                    headers.pop("Cookie", None)
                    await route.continue_(headers=headers)  # type: ignore[attr-defined]
                else:
                    await route.continue_()  # type: ignore[attr-defined]
            else:
                await route.abort(error_code="blockedbyclient")  # type: ignore[attr-defined]
        await page.route("**/*", _route)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        except Exception as exc:
            raise KeylolBrowserTimeoutError("打开其乐页面超时或失败，请稍后重试。") from exc

        if not _is_allowed_thread_document(page.url, expected_thread_id):
            raise KeylolBrowserNavigationError("其乐页面跳转到了不受支持的地址。")
        try:
            toc_items = await page.evaluate(_TOC_DISCOVERY_SCRIPT)
        except Exception:
            toc_items = []
        sections: list[dict[str, str | int]] = []
        seen_sections: set[tuple[str, str]] = set()
        if split_toc_sections:
            for item in toc_items if isinstance(toc_items, list) else []:
                if not isinstance(item, dict):
                    continue
                cp, viewpid = str(item.get("cp", "")), str(item.get("viewpid", ""))
                if not cp.isdigit() or not viewpid.isdigit() or (cp, viewpid) in seen_sections:
                    continue
                seen_sections.add((cp, viewpid))
                item["title"] = str(item.get("title", "")).strip()[:200] or f"第 {cp} 部分"
                sections.append(item)
                if len(sections) >= toc_limit:
                    break
        if not sections:
            sections = [{"index": -1, "title": "", "cp": "", "viewpid": ""}]
        allowed_toc_sections.update(
            (str(item["cp"]), str(item["viewpid"]))
            for item in sections
            if str(item["cp"]).isdigit() and str(item["viewpid"]).isdigit()
        )

        image_total = loaded_total = failed_total = 0
        total_pixels = 0
        capture_title = "其乐帖子"
        final_status = KeylolBrowserCaptureStatus.OK
        for section_index, section in enumerate(sections):
            if int(section.get("index", -1)) >= 0:
                cp, viewpid = str(section["cp"]), str(section["viewpid"])
                try:
                    async with page.expect_response(
                        lambda response: _is_allowed_toc_request(
                            response.url, expected_thread_id, viewpid, cp
                        ),
                        timeout=timeout,
                    ):
                        await page.evaluate(
                            _TOC_CLICK_SCRIPT,
                            {
                                "index": int(section["index"]),
                                "tid": str(section["tid"]),
                                "viewpid": viewpid,
                                "cp": cp,
                            },
                        )
                except Exception as exc:
                    raise KeylolBrowserNavigationError("目录内容加载失败，请稍后重试。") from exc
                await page.wait_for_timeout(250)
            try:
                info = await page.evaluate(
                    _TRANSFORM_SCRIPT,
                    {
                        "sourceUrl": source_url,
                        "viewportWidth": width,
                        "suppliedTitle": str(title or "").strip()[:300],
                        "suppliedAuthor": str(author or "").strip()[:120],
                        "suppliedPublishedAt": str(published_at or "").strip()[:120],
                        "sectionTitle": str(section.get("title", "")),
                        "hideToc": True,
                    },
                )
            except Exception as exc:
                raise KeylolBrowserNavigationError("页面未找到可截图的 1 楼正文；请确认 Cookie 有效。") from exc
            if int(info.get("imageCount", 0)) > MAX_BROWSER_IMAGE_COUNT or int(info.get("nodeCount", 0)) > MAX_BROWSER_DOM_NODES:
                raise KeylolBrowserNavigationError("帖子内容过大，已停止网页截图。")
            try:
                scroll_state = await page.evaluate(
                    _SCROLL_SCRIPT,
                    {"maxImages": MAX_BROWSER_IMAGE_COUNT, "maxHeight": MAX_BROWSER_PAGE_HEIGHT, "perImageTimeoutMs": min(8_000, max(2_000, timeout // 8))},
                )
                if bool(scroll_state.get("tooMany")) or bool(scroll_state.get("tooTall")) or int(scroll_state.get("pageHeight", 0)) > MAX_BROWSER_PAGE_HEIGHT:
                    raise KeylolBrowserNavigationError("帖子页面过长，已停止网页截图并准备回退。")
            except KeylolBrowserCaptureError:
                raise
            except Exception:
                pass
            stats = await page.evaluate(_FINALIZE_IMAGES_SCRIPT)
            if int(stats.get("pageHeight", 0)) > MAX_BROWSER_PAGE_HEIGHT:
                raise KeylolBrowserNavigationError("帖子页面过长，已停止网页截图并准备回退。")
            capture_height = int(stats.get("captureHeight", stats.get("pageHeight", 0)))
            total_pixels += width * capture_height
            if total_pixels > MAX_BROWSER_TOTAL_PIXELS:
                raise KeylolBrowserNavigationError("目录截图总长度过大，已停止处理并准备回退。")
            current_path = screenshot_path
            if own_output or not current_path or section_index:
                if own_output:
                    fd, current_path = tempfile.mkstemp(prefix="keylol-browser-", suffix=".png")
                    os.close(fd)
                    allocated_paths.append(current_path)
                elif section_index:
                    stem, extension = os.path.splitext(screenshot_path)
                    current_path = f"{stem}-{section_index + 1}{extension or '.png'}"
            try:
                await _capture_mobile_page_tiles(
                    page,
                    current_path,
                    width=width,
                    viewport_height=height,
                    page_height=capture_height,
                    timeout_ms=timeout,
                )
            except Exception as exc:
                raise KeylolBrowserTimeoutError("生成网页截图超时或失败，请稍后重试。") from exc
            screenshot_paths.append(current_path)
            image_count = int(info.get("imageCount", 0))
            loaded = int(stats.get("loaded", 0))
            failed = int(stats.get("failed", 0)) + int(info.get("missingImageCount", 0))
            image_total += image_count
            loaded_total += loaded
            failed_total += failed
            if failed or loaded < image_count:
                final_status = KeylolBrowserCaptureStatus.PARTIAL
            capture_title = str(info.get("title", capture_title))
        return KeylolBrowserCaptureResult(
            image_path=screenshot_paths[0],
            source_url=source_url,
            title=capture_title,
            image_count=image_total,
            loaded_image_count=loaded_total,
            failed_image_count=failed_total,
            status=final_status,
            image_paths=tuple(screenshot_paths),
            section_titles=tuple(str(section.get("title", "")) for section in sections),
        )
    except KeylolBrowserCaptureError:
        if own_output:
            for path in dict.fromkeys((*allocated_paths, *screenshot_paths)):
                try:
                    os.remove(path)
                except OSError:
                    pass
        raise
    except Exception as exc:
        if own_output:
            for path in dict.fromkeys((*allocated_paths, *screenshot_paths)):
                try:
                    os.remove(path)
                except OSError:
                    pass
        raise KeylolBrowserCaptureError("浏览器截图失败，请检查浏览器环境后重试。") from exc
    finally:
        if page is not None:
            try:
                await page.close()
            except Exception:
                pass
        if context is not None:
            try:
                await context.close()
            except Exception:
                pass
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                pass
        if playwright is not None:
            try:
                await playwright.stop()
            except Exception:
                pass


async def capture_keylol_screenshot(*args: object, **kwargs: object) -> str:
    """Compatibility helper returning only the generated PNG path."""

    result = await capture_keylol_webpage_screenshot(*args, **kwargs)  # type: ignore[arg-type]
    return result.image_path


__all__ = [
    "KeylolBrowserCaptureError",
    "KeylolBrowserCaptureResult",
    "KeylolBrowserCaptureStatus",
    "KeylolBrowserNavigationError",
    "KeylolBrowserTimeoutError",
    "KeylolBrowserUnavailable",
    "KeylolBrowserUrlError",
    "capture_keylol_screenshot",
    "capture_keylol_webpage_screenshot",
    "MAX_BROWSER_TOC_SECTIONS",
    "DEFAULT_BROWSER_TOC_SECTIONS",
    "normalize_keylol_browser_url",
    "parse_keylol_cookie_header",
]
