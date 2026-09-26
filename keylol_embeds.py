"""Safe, server-rendered static cards for approved Keylol embeds.

The only live provider handled here is the Steam Store widget endpoint. The
browser receives generated markup and a validated, inlined image; it never
loads the provider page or executes provider JavaScript.
"""

from __future__ import annotations

import asyncio
import base64
import html
import inspect
import logging
import re
from dataclasses import dataclass, field
from typing import Callable, Literal, Mapping
from urllib.parse import SplitResult, parse_qsl, urlsplit

from bs4 import BeautifulSoup, Tag


STEAM_STORE_HOST = "store.steampowered.com"
STEAM_WIDGET_TIMEOUT_SECONDS = 10
STEAM_WIDGET_MAX_HTML_BYTES = 512 * 1024
STEAM_WIDGET_MAX_IMAGE_BYTES = 2 * 1024 * 1024
logger = logging.getLogger(__name__)

# These hosts serve Steam artwork. Paths are separately restricted to the
# exact app artwork path, so the allowlist does not grant general CDN access.
STEAM_CDN_HOSTS = frozenset(
    {
        "cdn.akamai.steamstatic.com",
        "cdn.cloudflare.steamstatic.com",
        "shared.akamai.steamstatic.com",
        "shared.cloudflare.steamstatic.com",
        "shared.fastly.steamstatic.com",
        "steamcdn-a.akamaihd.net",
    }
)
# Observed in the live 3575980 widget response (Steam's China CDN variants).
# Kept separate so existing video-source/poster permissions do not expand.
STEAM_WIDGET_CDN_HOSTS = STEAM_CDN_HOSTS | frozenset({
    "shared.cdn.queniuqe.com", "shared.st.dl.eccdnx.com",
})
_WIDGET_PATH_RE = re.compile(r"^/widget/([0-9]{1,15})/$")
_STEAM_IMAGE_PATH_RE = re.compile(
    r"^/(?:store_item_assets/)?steam/apps/([0-9]{1,15})/"
    r"(?:[0-9a-f]{40}/)?(?:header|capsule_184x69|capsule_231x87)\.jpg$",
)
_CACHE_QUERY_RE = re.compile(r"^t=[0-9]{1,20}$")
_DISCOUNT_RE = re.compile(r"(?:-|−)\s*\d{1,3}\s*%")

# Fixed local styling for the rebuilt embed cards. Callers should append
# this to their render stylesheet; it contains no provider CSS or resources.
EMBED_CARD_CSS = """
.keylol-browser-media-card.keylol-browser-steam-embed {
  display: block; box-sizing: border-box; overflow: hidden;
  margin: 12px 0; padding: 16px; border: 1px solid #344654;
  border-radius: 4px; background: linear-gradient(120deg, #22394b, #162331);
  color: #c7d5e0; font: 14px/1.55 Arial, Helvetica, sans-serif;
  text-align: left;
}
.keylol-browser-steam-embed .keylol-steam-embed-heading {
  display: flex; align-items: flex-start; justify-content: space-between;
  gap: 14px; margin-bottom: 14px;
}
.keylol-browser-steam-embed .keylol-steam-embed-title {
  color: #fff; font-size: 18px; line-height: 1.3; overflow-wrap: anywhere;
}
.keylol-browser-steam-embed .keylol-steam-embed-brand {
  flex-shrink: 0; color: #fff; font-size: 12px; letter-spacing: 1px;
  border: 1px solid #8299aa; border-radius: 3px; padding: 2px 6px;
}
.keylol-browser-steam-embed .keylol-steam-embed-image img {
  display: block; width: 100%; max-width: 100%; height: auto;
  margin: 0 0 14px; border: 0;
}
.keylol-browser-steam-embed .keylol-steam-embed-description {
  margin: 0 0 16px; color: #c7d5e0; font-size: 14px; line-height: 1.55;
}
.keylol-browser-steam-embed .keylol-steam-embed-pricing {
  display: flex; flex-wrap: wrap; align-items: center; gap: 10px;
  padding: 8px; background: #101c26;
}
.keylol-browser-steam-embed .keylol-steam-embed-discount {
  background: #4c6b22; color: #beee11; font-size: 24px; padding: 0 7px;
}
.keylol-browser-steam-embed .keylol-steam-embed-prices { display: grid; }
.keylol-browser-steam-embed .keylol-steam-embed-original-price {
  color: #8f98a0; font-size: 12px; text-decoration: line-through;
}
.keylol-browser-steam-embed .keylol-steam-embed-price {
  color: #beee11; font-size: 18px;
}
.keylol-browser-steam-embed a.keylol-steam-embed-buy {
  display: inline-block; margin-left: auto; border-radius: 2px;
  background: #4c7518; color: #fff !important; text-decoration: none;
  padding: 8px 10px; font-size: 13px; white-space: nowrap;
}
.keylol-browser-steam-embed .keylol-steam-embed-partial {
  display: block; margin-top: 10px; font-size: 12px; color: #8f98a0;
}
.keylol-steam-widget-error {
  box-sizing: border-box;
  display: flex;
  align-items: center;
  min-height: 78px;
  margin: 12px 0;
  padding: 14px 18px;
  border: 1px solid #2c4358;
  border-radius: 3px;
  background: linear-gradient(110deg, #1b2838 0%, #213449 100%);
  color: #c7d5e0;
  font: 14px/1.5 Arial, Helvetica, sans-serif;
}
.keylol-steam-widget-error-content { min-width: 0; }
.keylol-steam-widget-error-title {
  display: block;
  margin: 0 0 4px;
  color: #fff;
  font-size: 16px;
  font-weight: 700;
}
.keylol-steam-widget-error-message { margin: 0; }
.keylol-steam-widget-error-code {
  display: block;
  margin-top: 5px;
  color: #8f98a0;
  font-size: 12px;
}
.keylol-video-embed img {
  display: block;
  max-width: 100%;
  height: auto;
  margin: 0 0 10px;
}
"""


@dataclass(frozen=True)
class EmbedRenderResult:
    """Sanitized HTML and provider/fetch state for one embedded resource."""

    html: str
    status: Literal["success", "provider_error", "fetch_fallback"]
    provider: str | None
    partial: bool = False
    # Internal only: never interpolated into the user-facing card.
    reason: str = "success"
    diagnostics: dict[str, object] = field(default_factory=dict)

    @property
    def loaded(self) -> bool:
        """Whether a provider-specific static card was successfully built."""

        return self.status == "provider_error" or (
            self.status == "success" and not self.partial
        )

    @property
    def fallback(self) -> bool:
        """Compatibility flag for callers that track fallback embed cards."""

        return self.status == "fetch_fallback" or self.partial


def normalize_steam_widget_url(value: object) -> str | None:
    """Return a canonical Steam widget URL, or None for every other URL.

    The optional utm_source=keylol marker is accepted because older forum
    posts can append it. It is discarded from the canonical URL and never
    sent to Steam.
    """

    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw or len(raw) > 2048:
        return None
    try:
        parsed = urlsplit(raw)
        host = (parsed.hostname or "").lower()
        # Comparing the complete authority rejects explicit :443, credentials,
        # trailing-dot aliases, and authority tricks.
        if (
            parsed.scheme.lower() != "https"
            or host != STEAM_STORE_HOST
            or parsed.netloc.lower() != STEAM_STORE_HOST
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            or parsed.fragment
        ):
            return None
        match = _WIDGET_PATH_RE.fullmatch(parsed.path)
        if match is None:
            return None
        if parsed.query and parsed.query != "utm_source=keylol":
            return None
        # Guard against alternate encodings or duplicate parameters.
        if parsed.query and parse_qsl(parsed.query, keep_blank_values=True) != [
            ("utm_source", "keylol")
        ]:
            return None
    except (ValueError, UnicodeError):
        return None
    return f"https://{STEAM_STORE_HOST}/widget/{match.group(1)}/"


def _steam_app_id(canonical_url: str) -> str:
    match = _WIDGET_PATH_RE.fullmatch(urlsplit(canonical_url).path)
    return match.group(1) if match else ""


def _steam_widget_redirect_allowed(url: str, canonical_url: str) -> bool:
    """Enforce the exact widget origin and app path before every redirect."""

    return normalize_steam_widget_url(url) == canonical_url


def _steam_image_redirect_allowed(url: str, app_id: str) -> bool:
    """Allow only one app's named artwork on explicit Steam CDN hosts."""

    if (not isinstance(url, str) or len(url) > 2048
            or any(ord(char) <= 0x20 or ord(char) == 0x7f for char in url)):
        return False
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        if (
            parsed.scheme.lower() != "https"
            or host not in STEAM_WIDGET_CDN_HOSTS
            or parsed.netloc.lower() != host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            or parsed.fragment
        ):
            return False
        path_match = _STEAM_IMAGE_PATH_RE.fullmatch(parsed.path)
        if path_match is None or path_match.group(1) != app_id:
            return False
        if parsed.query and not _CACHE_QUERY_RE.fullmatch(parsed.query):
            return False
    except (ValueError, UnicodeError):
        return False
    return True


def _mapping_or_attr(value: object, *names: str) -> object | None:
    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
        return None
    for name in names:
        if hasattr(value, name):
            return getattr(value, name)
    return None


def _media_result_data(result: object) -> tuple[bytes, str, str | None] | None:
    """Read the small common result surface exposed by safe_media."""

    if result is None:
        return None
    raw_data = _mapping_or_attr(result, "data", "body", "content")
    if not isinstance(raw_data, (bytes, bytearray, memoryview)):
        return None
    raw_type = _mapping_or_attr(result, "content_type", "mime_type")
    if not isinstance(raw_type, str):
        headers = _mapping_or_attr(result, "headers")
        raw_type = _mapping_or_attr(headers, "Content-Type", "content-type")
    if not isinstance(raw_type, str):
        return None
    final_url = _mapping_or_attr(result, "final_url", "url")
    return bytes(raw_data), raw_type.split(";", 1)[0].strip().lower(), (
        final_url if isinstance(final_url, str) else None
    )


async def _call_fetch(
    downloader: object,
    method_name: str,
    url: str,
    *,
    allowed_url: Callable[[str], bool],
    max_bytes: int,
    diagnostics: dict[str, object] | None = None,
    provider: str | None = None,
) -> object | None:
    """Call safe_media while keeping provider redirects constrained."""

    method = getattr(downloader, method_name, None)
    if not callable(method):
        return None
    try:
        parameters = inspect.signature(method).parameters
    except (TypeError, ValueError):
        return None

    # Fail closed if the downloader cannot enforce the provider rule at every
    # redirect. A final-URL check would happen after an unsafe request.
    if "allowed_url" not in parameters:
        return None
    kwargs: dict[str, object] = {"allowed_url": allowed_url}
    accepts_kwargs = any(
        item.kind == inspect.Parameter.VAR_KEYWORD for item in parameters.values()
    )
    if "max_bytes" in parameters or accepts_kwargs:
        kwargs["max_bytes"] = max_bytes
    if diagnostics is not None and ("diagnostics" in parameters or accepts_kwargs):
        kwargs["diagnostics"] = diagnostics
    if provider is not None and ("provider" in parameters or accepts_kwargs):
        kwargs["provider"] = provider

    try:
        call_result = method(url, **kwargs)
        if not inspect.isawaitable(call_result):
            return None
        return await asyncio.wait_for(
            call_result, timeout=STEAM_WIDGET_TIMEOUT_SECONDS
        )
    except Exception:
        if diagnostics is not None:
            diagnostics["reason"] = "request_failed"
        return None


def _clean_text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:limit]


def _first_text(
    root: BeautifulSoup | Tag, selectors: tuple[str, ...], limit: int
) -> str:
    for selector in selectors:
        node = root.select_one(selector)
        if node is None:
            continue
        value = (
            node.get("content")
            if node.name == "meta"
            else node.get_text(" ", strip=True)
        )
        cleaned = _clean_text(value, limit)
        if cleaned:
            return cleaned
    return ""


def _steam_widget_fields(document: BeautifulSoup, app_id: str) -> dict[str, str]:
    name = ""
    # Current widgets use a purchase heading, not the store page's game_name.
    # Validate its product link against this widget's AppID before trusting it.
    for node in document.select(
        "#widget .header_container .main_text a[href], "
        "#widget #header h1:not(.tail) a[href], #widget h1.main_text a[href]"
    ):
        try:
            target = urlsplit(str(node.get("href", "")))
            same_app = (
                target.scheme == "https"
                and target.netloc == STEAM_STORE_HOST
                and re.fullmatch(rf"/app/{re.escape(app_id)}/(?:[^/]+/)?", target.path)
            )
        except ValueError:
            same_app = False
        if same_app:
            name = re.sub(
                r"^(?:Buy\s+|购买\s*|購買\s*)", "", _plain_node_text(node, 260),
                flags=re.IGNORECASE,
            )[:240]
            if name:
                break
    if not name:
        name = _first_text(
            document.select_one("#widget") or document,
            (".game_name", ".game_title", "[itemprop='name']"),
            240,
        )
    description = _first_text(
        document,
        (
            "#widget .desc",
            ".game_description_snippet",
            ".game_description",
            "#game_area_description .game_description_snippet",
            "meta[name='description']",
        ),
        1200,
    )
    price = _first_text(
        document,
        (
            ".discount_final_price",
            ".game_purchase_price",
            ".game_purchase_action .price",
            ".game_area_purchase_game .price",
        ),
        120,
    )
    discount = _first_text(document, (".discount_pct",), 60)
    original_price = _first_text(document, (".discount_original_price",), 120)
    if not discount:
        discount_block = document.select_one(".discount_block")
        block_text = _clean_text(
            discount_block.get_text(" ", strip=True) if discount_block else "", 300
        )
        found = _DISCOUNT_RE.search(block_text)
        if found:
            discount = _clean_text(found.group(0), 60)

    image_url = ""
    for selector in (
        "#widget img.capsule",
        "link[rel='image_src']",
        ".game_header_image_ctn img",
        "img.game_header_image",
        ".game_header_image",
    ):
        for node in document.select(selector):
            if node.name not in {"img", "link"}:
                continue
            candidate = str(node.get("href" if node.name == "link" else "src", "")).strip()
            if not candidate:
                candidate = str(node.get("data-src", "")).strip()
            if _steam_image_redirect_allowed(candidate, app_id):
                image_url = candidate
                break
        if image_url:
            break
    # Older metadata-based widgets must also identify this app's artwork;
    # a generic document og:title alone is not evidence of a product.
    if not name and image_url:
        name = _first_text(document, ("meta[property='og:title']",), 240)
    return {
        "name": name,
        "description": description,
        "price": price,
        "original_price": original_price,
        "discount": discount,
        "image_url": image_url,
        "app_id": app_id,
    }


def _plain_node_text(node: Tag, limit: int) -> str:
    """Extract bounded visible text without carrying source markup forward."""

    fragment = BeautifulSoup(str(node), "html.parser")
    for hidden in fragment.select("script, style, template, noscript, iframe, object, embed"):
        hidden.decompose()
    return _clean_text(fragment.get_text(" ", strip=True), limit)


def _steam_provider_error_fields(document: BeautifulSoup) -> dict[str, str] | None:
    """Recognize the confirmed Steam widget error structure using text only.

    Steam's live 4813850 widget response was HTTP 200 HTML with the localized
    error heading in ``#widget .header_container > h1.main_text`` and its
    description in ``#widget .desc``. The app ID is not present in the body;
    optional ``#<digits>`` error codes are handled generically if Steam adds
    one to the description in another response.
    """

    heading = document.select_one("#widget .header_container > h1.main_text")
    description_node = document.select_one("#widget .desc")
    if heading is None or description_node is None:
        return None
    title = _plain_node_text(heading, 80)
    description = _plain_node_text(description_node, 400)
    if title.casefold() not in {"错误", "error"} or not description:
        return None

    code_match = re.search(r"(?<!\w)#([0-9]{1,20})(?![0-9])", description)
    code = f"#{code_match.group(1)}" if code_match else ""
    if code_match:
        description = _clean_text(
            description[: code_match.start()] + description[code_match.end() :], 400
        )
    return {"title": title, "description": description, "code": code}


def _steam_provider_error_card_html(fields: Mapping[str, str]) -> str:
    title = html.escape(fields.get("title", "错误"), quote=True)
    description = html.escape(fields.get("description", ""), quote=True)
    code_text = fields.get("code", "")
    code = (
        f'<span class="keylol-steam-widget-error-code">'
        f"{html.escape(code_text, quote=True)}</span>"
        if code_text
        else ""
    )
    return (
        '<article class="keylol-browser-media-card keylol-steam-widget-error" '
        'data-keylol-embed-provider="steam" '
        'data-keylol-embed-status="provider_error">'
        '<div class="keylol-steam-widget-error-content">'
        f'<strong class="keylol-steam-widget-error-title">{title}</strong>'
        f'<p class="keylol-steam-widget-error-message">{description}</p>'
        f"{code}</div></article>"
    )


def _sniff_image(data: bytes, content_type: str) -> str | None:
    """Accept only static raster formats whose bytes match their declared MIME."""

    actual: str | None = None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        actual = "image/png"
    elif data.startswith(b"\xff\xd8\xff"):
        actual = "image/jpeg"
    elif data[:6] in (b"GIF87a", b"GIF89a"):
        actual = "image/gif"
    elif len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        actual = "image/webp"
    elif (
        len(data) >= 12
        and data[4:8] == b"ftyp"
        and data[8:12] in (b"avif", b"avis")
    ):
        actual = "image/avif"
    if actual != content_type:
        return None
    return actual


def _fallback_html(provider: str | None) -> str:
    title = "Steam 商店内容" if provider == "steam" else "外部嵌入内容"
    return (
        '<div class="keylol-browser-media-card keylol-browser-embed-fallback" '
        f'data-keylol-embed-provider="{provider or "unknown"}" '
        'data-keylol-embed-status="fetch_fallback">'
        f"<strong>{title}</strong>"
        "<span>（静态截图无法完整加载）</span>"
        "</div>"
    )


def _steam_card_html(
    fields: Mapping[str, str], image_data_uri: str = "", *, partial: bool
) -> str:
    name = html.escape(fields.get("name", "") or "Steam 商店内容", quote=True)
    image = ""
    if image_data_uri:
        alt = html.escape(fields.get("name", "Steam 游戏封面"), quote=True)
        image = (
            '<div class="keylol-steam-embed-image">'
            f'<img data-keylol-embed-image="1" src="{image_data_uri}" alt="{alt}" '
            'loading="eager" decoding="async">'
            "</div>"
        )
    details: list[str] = []
    description = html.escape(fields.get("description", ""), quote=True)
    if description:
        details.append(
            f'<p class="keylol-steam-embed-description">{description}</p>'
        )
    price = html.escape(fields.get("price", ""), quote=True)
    original_price = html.escape(fields.get("original_price", ""), quote=True)
    discount = html.escape(fields.get("discount", ""), quote=True)
    price_parts: list[str] = []
    if discount:
        price_parts.append(
            f'<span class="keylol-steam-embed-discount">{discount}</span>'
        )
    prices = ""
    if original_price:
        prices += f'<del class="keylol-steam-embed-original-price">{original_price}</del>'
    if price:
        prices += f'<strong class="keylol-steam-embed-price">{price}</strong>'
    if prices:
        price_parts.append(f'<span class="keylol-steam-embed-prices">{prices}</span>')
    # Build the destination locally; never copy provider markup or arbitrary links.
    app_id = fields.get("app_id", "")
    if re.fullmatch(r"[0-9]{1,15}", app_id):
        price_parts.append(
            f'<a class="keylol-steam-embed-buy" href="https://{STEAM_STORE_HOST}/app/{app_id}/" '
            'rel="noopener noreferrer">在 Steam 上购买</a>'
        )
    if price_parts:
        details.append(
            '<div class="keylol-steam-embed-pricing">'
            + " ".join(price_parts)
            + "</div>"
        )
    if partial:
        details.append(
            '<span class="keylol-steam-embed-partial">部分商店内容无法加载</span>'
        )
    return (
        '<article class="keylol-browser-media-card keylol-browser-steam-embed" '
        'data-keylol-embed-provider="steam" '
        'data-keylol-embed-status="success">'
        '<div class="keylol-steam-embed-heading">'
        f'<strong class="keylol-steam-embed-title">{name}</strong>'
        '<span class="keylol-steam-embed-brand">STEAM</span></div>'
        f'{image}<div class="keylol-steam-embed-content">'
        + "".join(details)
        + "</div></article>"
    )


async def render_embed(
    url: object,
    downloader: object,
    *,
    fetch: bool = True,
) -> EmbedRenderResult:
    """Render a provider-specific static card, or a visible unknown fallback.

    downloader is the shared safe_media downloader. It must provide
    fetch_html/fetch_image methods accepting an allowed_url callback. Returning
    None gives the caller a visible partial card. No cookies or browser
    requests are used here.
    """

    provider = classify_embed(url)
    if provider == "keylol_video":
        return EmbedRenderResult(
            html=await _keylol_video_card_html(
                _keylol_video_fields(url) or {}, downloader, fetch=fetch
            ),
            status="success",
            provider="keylol_video",
        )
    if provider == "unknown":
        return EmbedRenderResult(
            html=_fallback_html(None), status="fetch_fallback", provider=None,
            reason="unsupported_provider",
        )
    canonical_url = normalize_steam_widget_url(url)
    app_id = _steam_app_id(canonical_url)
    diagnostics: dict[str, object] = {}

    def finish(reason: str, *, markup: str = "",
               status: Literal["success", "provider_error", "fetch_fallback"] = "fetch_fallback",
               partial: bool = False) -> EmbedRenderResult:
        diagnostics["reason"] = reason
        logger.debug("Steam widget app_id=%s status=%s diagnostics=%r",
                     app_id, status, diagnostics)
        return EmbedRenderResult(
            html=markup or _fallback_html("steam"), status=status, provider="steam",
            partial=partial, reason=reason, diagnostics=diagnostics,
        )

    if not fetch or downloader is None:
        return finish("fetch_disabled" if not fetch else "request_failed")

    page_diagnostics: dict[str, object] = {}
    diagnostics["request"] = page_diagnostics
    raw_page = await _call_fetch(
        downloader,
        "fetch_html",
        canonical_url,
        allowed_url=lambda candidate: _steam_widget_redirect_allowed(
            candidate, canonical_url
        ),
        max_bytes=STEAM_WIDGET_MAX_HTML_BYTES,
        diagnostics=page_diagnostics,
        provider="steam",
    )
    if raw_page is None:
        return finish(str(page_diagnostics.get("reason") or "request_failed"))
    page_result = _media_result_data(raw_page)
    if page_result is None:
        return finish("body_rejected")
    if page_result[2] is not None and not _steam_widget_redirect_allowed(
        page_result[2], canonical_url
    ):
        return finish("redirect_rejected")
    response_status = _mapping_or_attr(raw_page, "status", "status_code")
    if response_status is not None and response_status != 200:
        return finish("http_status")
    if page_result[1] not in {"text/html", "application/xhtml+xml"}:
        return finish("content_type_rejected")
    if not page_result[0] or len(page_result[0]) > STEAM_WIDGET_MAX_HTML_BYTES:
        return finish("body_rejected")

    try:
        document = BeautifulSoup(page_result[0], "html.parser")
        diagnostics["html_length"] = len(page_result[0])
        diagnostics["title"] = _first_text(document, ("title",), 300)
        diagnostics["root_nodes"] = [
            {"tag": node.name, "id": _clean_text(node.get("id", ""), 80),
             "class": _clean_text(" ".join(node.get("class", [])), 160)}
            for node in document.select("#widget, #widget [id], #widget [class]")[:40]
        ]
        fields = _steam_widget_fields(document, app_id)
        diagnostics["fields"] = fields
        provider_error = _steam_provider_error_fields(document)
        if provider_error is not None:
            return finish(
                "provider_error", markup=_steam_provider_error_card_html(provider_error),
                status="provider_error",
            )
    except Exception:
        return finish("body_rejected")

    # A Steam product title is needed to distinguish a parsed product widget
    # from an unrecognized/changed response. Artwork remains optional: the
    # existing partial-card path preserves safe product text when its image
    # cannot be fetched or validated.
    if not fields.get("name"):
        return finish("parse_missing_title")

    image_data_uri = ""
    image_ok = False
    image_url = fields.get("image_url", "")
    reason = "image_url_unrecognized"
    if image_url:
        image_diagnostics: dict[str, object] = {}
        diagnostics["image_request"] = image_diagnostics
        reason = "image_fetch_failed"
        raw_image = await _call_fetch(
            downloader,
            "fetch_image",
            image_url,
            allowed_url=lambda candidate: _steam_image_redirect_allowed(
                candidate, app_id
            ),
            max_bytes=STEAM_WIDGET_MAX_IMAGE_BYTES,
            diagnostics=image_diagnostics,
            provider="steam",
        )
        image_result = _media_result_data(raw_image)
        if (
            image_result is not None
            and image_result[1].startswith("image/")
            and len(image_result[0]) <= STEAM_WIDGET_MAX_IMAGE_BYTES
            and _sniff_image(image_result[0], image_result[1]) is not None
            and (
                image_result[2] is None
                or _steam_image_redirect_allowed(image_result[2], app_id)
            )
        ):
            image_data_uri = (
                f"data:{image_result[1]};base64,"
                + base64.b64encode(image_result[0]).decode("ascii")
            )
            image_ok = True
            reason = "success"

    # Missing price, description, or artwork can be a normal Steam product
    # state; metadata and the static card have still been parsed successfully.
    markup = _steam_card_html(fields, image_data_uri, partial=not image_ok)
    return finish(reason, markup=markup, status="success", partial=not image_ok)


__all__ = [
    "EmbedRenderResult",
    "normalize_steam_widget_url",
    "render_embed",
]


# Observed in t1050511-1-1; see the minimal live HTML fixture. These local
# wrappers read mp4/poster (videojs) or mp4/m3u8/poster (tcplayer). A CSS class
# alone, an onexin.com iframe, or another Keylol path is not a known player.
_KEYLOL_VIDEO_PATHS = {
    "/source/plugin/onexin_html5player/open/videojs/html5player.html": {"mp4", "poster"},
    "/source/plugin/onexin_html5player/open/tcplayer/html5player.html": {"mp4", "m3u8", "poster"},
}
_KEYLOL_POSTER_HOSTS = frozenset({
    "keylol.com", "www.keylol.com", "img.keylol.com", "blob.keylol.com",
})
_KEYLOL_POSTER_PATH_RE = re.compile(
    r"^/data/attachment/(?:[a-z0-9_-]+/)*[a-z0-9_-]+\.(?:png|jpe?g|gif|webp|avif)$",
    re.IGNORECASE,
)
_STEAM_VIDEO_PATH_RE = re.compile(
    r"^/store_item_assets/steam/apps/([0-9]{1,15})/extras/[a-zA-Z0-9_-]+\.(?:mp4|webm)$"
)
_STEAM_TRAILER_PATH_RE = re.compile(
    r"^/store_trailers/([0-9]{1,15})/(?:[a-zA-Z0-9_-]+/)+[a-zA-Z0-9_-]+\.m3u8$"
)


def _strict_https_parts(value: object) -> SplitResult | None:
    """Parse only an unambiguous HTTPS URL; this never authorizes a request."""

    if not isinstance(value, str) or not value or len(value) > 8192:
        return None
    if any(ord(char) <= 0x20 or ord(char) == 0x7F for char in value):
        return None
    try:
        parts = urlsplit(value)
        if (
            parts.scheme.lower() != "https"
            or not parts.hostname
            or parts.netloc.lower() != parts.hostname.lower()
            or parts.port is not None
            or parts.fragment
        ):
            return None
    except (ValueError, UnicodeError):
        return None
    return parts


def _steam_video_app_id(value: str) -> str:
    """Read a Steam source label/app ID without DNS or fetching video bytes."""

    parts = _strict_https_parts(value)
    if parts is None:
        return ""
    host = parts.hostname.lower()
    if host in STEAM_CDN_HOSTS:
        match = _STEAM_VIDEO_PATH_RE.fullmatch(parts.path)
    elif host == "video.akamai.steamstatic.com":
        match = _STEAM_TRAILER_PATH_RE.fullmatch(parts.path)
    else:
        return ""
    if parts.query and not _CACHE_QUERY_RE.fullmatch(parts.query):
        return ""
    return match.group(1) if match else ""


def _keylol_video_poster_allowed(url: str, app_id: str) -> bool:
    """Only existing Steam app headers or static Keylol attachment images."""

    parts = _strict_https_parts(url)
    if app_id and parts is not None and len(url) <= 2048:
        if (parts.hostname.lower() in STEAM_CDN_HOSTS
                and (not parts.query or _CACHE_QUERY_RE.fullmatch(parts.query))
                and re.fullmatch(
            rf"/(?:store_item_assets/)?steam/apps/{re.escape(app_id)}/header\.jpg",
            parts.path, re.IGNORECASE,
        )):
            return True
    return bool(
        parts is not None
        and parts.hostname.lower() in _KEYLOL_POSTER_HOSTS
        and not parts.query
        and _KEYLOL_POSTER_PATH_RE.fullmatch(parts.path)
    )


def _keylol_video_fields(url: object) -> dict[str, str] | None:
    parts = _strict_https_parts(url)
    if (
        parts is None
        or parts.hostname.lower() not in {"keylol.com", "www.keylol.com"}
        or parts.path not in _KEYLOL_VIDEO_PATHS
    ):
        return None
    # Malformed/unknown query fields never become proxy targets. The wrapper
    # is still recognized as video, even when no safe metadata can be read.
    fields = {"label": "", "poster_url": ""}
    try:
        pairs = parse_qsl(parts.query, keep_blank_values=True, max_num_fields=8, errors="strict")
    except (ValueError, UnicodeError):
        return fields
    params = dict(pairs)
    if len(params) != len(pairs) or not set(params) <= _KEYLOL_VIDEO_PATHS[parts.path]:
        return fields
    sources = [params[name] for name in ("mp4", "m3u8") if params.get(name)]
    app_ids = {_steam_video_app_id(source) for source in sources}
    app_id = next(iter(app_ids)) if len(app_ids) == 1 and "" not in app_ids else ""
    if app_id:
        fields["label"] = "Steam 视频"
    poster = params.get("poster", "")
    if poster and _keylol_video_poster_allowed(poster, app_id):
        fields["poster_url"] = poster
    return fields


def classify_embed(url: object) -> Literal["steam_widget", "keylol_video", "unknown"]:
    """Classify each provider independently without fetching iframe URLs."""

    if normalize_steam_widget_url(url) is not None:
        return "steam_widget"
    if _keylol_video_fields(url) is not None:
        return "keylol_video"
    return "unknown"


async def _keylol_video_card_html(
    fields: Mapping[str, str], downloader: object, *, fetch: bool
) -> str:
    """Render a complete static video representation, including on preview failure."""

    poster_url = fields.get("poster_url", "")
    image = ""
    if fetch and downloader is not None and poster_url:
        # Pin the preview to exactly this approved image, including redirects.
        # Neither the local wrapper nor any mp4/webm/m3u8/ts URL is fetched.
        raw_image = await _call_fetch(
            downloader, "fetch_image", poster_url,
            allowed_url=lambda candidate: candidate == poster_url,
            max_bytes=STEAM_WIDGET_MAX_IMAGE_BYTES,
        )
        result = _media_result_data(raw_image)
        if (
            result is not None
            and len(result[0]) <= STEAM_WIDGET_MAX_IMAGE_BYTES
            and _sniff_image(result[0], result[1]) is not None
            and result[2] in {None, poster_url}
        ):
            data_uri = f"data:{result[1]};base64," + base64.b64encode(result[0]).decode("ascii")
            image = (
                f'<img data-keylol-embed-image="1" src="{data_uri}" '
                'alt="视频预览图" loading="eager" decoding="async">'
            )
    label = html.escape(fields.get("label", ""), quote=True)
    detail = f"<span> · {label}</span>" if label else ""
    return (
        '<figure class="media-card media-card-video keylol-browser-media-card keylol-video-embed" '
        'data-keylol-embed-provider="keylol_video" data-keylol-embed-status="success">'
        f'{image}<figcaption><strong>▶ 视频内容</strong>{detail}'
        '<span>（静态截图无法播放）</span></figcaption></figure>'
    )
