"""Bounded, cookie-free downloads for public media used in screenshots.

Every request is HTTPS-only, resolves its hostname before connecting, rejects
the request if any DNS answer is non-public, and pins aiohttp to the validated
answers for that hop. Redirects are followed manually so the same checks run
before every subsequent connection. No environment proxy or cookie jar is
used.
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import re
import socket
import warnings
from dataclasses import dataclass
from io import BytesIO
from typing import Awaitable, Callable, Sequence
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

import aiohttp
from PIL import Image, UnidentifiedImageError
from yarl import URL


DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_TOTAL_BYTES = 20 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
DEFAULT_MAX_HTML_BYTES = 1024 * 1024
MAX_HTML_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_REDIRECTS = 4
MAX_REDIRECTS = 5
MAX_IMAGE_PIXELS = 20_000_000
_READ_CHUNK_BYTES = 64 * 1024
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_DENIED_DNS_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".test",
    ".invalid",
    ".example",
)
_NUMERIC_HOST_RE = re.compile(
    r"^(?:(?:0x[0-9a-f]+)|(?:[0-9]+))(?:\.(?:(?:0x[0-9a-f]+)|(?:[0-9]+)))*$",
    re.IGNORECASE,
)
_DOMAIN_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", re.I)
_STEAM_WIDGET_HOST = "store.steampowered.com"
_STEAM_CDN_HOSTS = frozenset(
    {
        "cdn.akamai.steamstatic.com",
        "cdn.cloudflare.steamstatic.com",
        "shared.akamai.steamstatic.com",
        "shared.cloudflare.steamstatic.com",
        "shared.fastly.steamstatic.com",
        "steamcdn-a.akamaihd.net",
        "shared.cdn.queniuqe.com",
        "shared.st.dl.eccdnx.com",
    }
)
_STEAM_WIDGET_PATH_RE = re.compile(r"^/widget/([0-9]{1,15})/$")
_STEAM_IMAGE_PATH_RE = re.compile(
    r"^/(?:store_item_assets/)?steam/apps/([0-9]{1,15})/"
    r"(?:[0-9a-f]{40}/)?(?:header|capsule_184x69|capsule_231x87)\.jpg$"
)
_STEAM_CACHE_QUERY_RE = re.compile(r"^t=[0-9]{1,20}$")


def _encode_proxy_basic_auth(username: str, password: str) -> str:
    """Encode an HTTP Basic proxy credential using UTF-8 on aiohttp 3.9+."""

    credentials = f"{username}:{password}".encode("utf-8")
    return "Basic " + base64.b64encode(credentials).decode("ascii")

URLPolicy = Callable[[str], bool]
AddressLookup = Callable[[str, int], Awaitable[Sequence[str]]]


@dataclass(frozen=True, slots=True)
class _ExplicitProxy:
    """An explicitly configured proxy; its credentials never enter diagnostics."""

    url: str
    hostname: str
    port: int
    username: str | None = None
    password: str | None = None


class _SteamPinnedProxyRequest(aiohttp.ClientRequest):
    """Restore the origin Host header only after the proxy tunnel is ready.

    aiohttp builds its CONNECT request from a plain ClientRequest. Leaving the
    Steam request's initial Host header at the pinned numeric URL makes both
    the CONNECT authority and CONNECT Host identify the same validated IP.
    The connector calls this request's send method only after CONNECT and TLS
    setup, so the origin then receives the original HTTPS hostname.
    """

    async def send(self, conn):  # aiohttp 3.9 through 3.14 use this signature
        if self.proxy is not None and self.server_hostname:
            self.headers["Host"] = self.server_hostname
        return await super().send(conn)


@dataclass(frozen=True, slots=True)
class SafeImage:
    """A validated raster image suitable for a browser response or data URI."""

    data: bytes
    content_type: str

    @property
    def body(self) -> bytes:
        """Alias useful to callers that treat all downloads as response bodies."""

        return self.data


@dataclass(frozen=True, slots=True)
class SafeHtml:
    """A bounded HTML response from a caller-approved public endpoint."""

    data: bytes
    content_type: str

    @property
    def body(self) -> bytes:
        return self.data


def _normalized_hostname(hostname: str) -> str | None:
    host = str(hostname or "").lower().rstrip(".")
    if not host or "%" in host:
        return None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            host = host.encode("idna").decode("ascii").lower()
        except (UnicodeError, ValueError):
            return None
        if len(host) > 253 or "." not in host:
            return None
        if _NUMERIC_HOST_RE.fullmatch(host):
            # Reject legacy numeric IPv4 spellings such as 127.1, octal, or
            # hexadecimal forms. Only canonical IP literals are unambiguous.
            return None
        labels = host.split(".")
        if any(not _DOMAIN_LABEL_RE.fullmatch(label) for label in labels):
            return None
        if host == "localhost" or any(
            host.endswith(suffix) for suffix in _DENIED_DNS_SUFFIXES
        ):
            return None
        return host

    if not _is_public_ip(address):
        return None
    return address.compressed.lower()


def _normalized_explicit_proxy_hostname(hostname: str) -> str | None:
    """Normalize an admin-configured proxy host without banning local proxies."""

    host = str(hostname or "").lower().rstrip(".")
    if not host or "%" in host:
        return None
    try:
        return ipaddress.ip_address(host).compressed.lower()
    except ValueError:
        try:
            host = host.encode("idna").decode("ascii").lower()
        except (UnicodeError, ValueError):
            return None
        if len(host) > 253 or _NUMERIC_HOST_RE.fullmatch(host):
            return None
        labels = host.split(".")
        if any(not _DOMAIN_LABEL_RE.fullmatch(label) for label in labels):
            return None
        return host


def _parse_explicit_proxy(value: object | None) -> _ExplicitProxy | None:
    """Parse only a configured HTTP(S) proxy endpoint, keeping auth out of logs."""

    raw = str(value or "")
    if not raw or raw != raw.strip() or any(
        ord(char) <= 0x20 or ord(char) == 0x7F for char in raw
    ):
        return None
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    if (
        scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        return None
    hostname = _normalized_explicit_proxy_hostname(parsed.hostname or "")
    if hostname is None:
        return None
    port = port or (443 if scheme == "https" else 80)
    if not 1 <= port <= 65535:
        return None
    auth_user = unquote(parsed.username) if parsed.username is not None else None
    auth_password = unquote(parsed.password) if parsed.password is not None else None
    if auth_user is not None and ":" in auth_user:
        return None
    host_for_url = f"[{hostname}]" if ":" in hostname else hostname
    safe_url = f"{scheme}://{host_for_url}:{port}"
    return _ExplicitProxy(
        url=safe_url,
        hostname=hostname,
        port=port,
        username=auth_user,
        password=auth_password,
    )


def _steam_url_app_id(url: str, resource_kind: str) -> str | None:
    """Accept only one Steam widget or one Steam artwork asset URL shape."""

    if len(url) > 2048 or any(ord(char) <= 0x20 or ord(char) == 0x7F for char in url):
        return None
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        if (
            parsed.scheme.lower() != "https"
            or parsed.netloc.lower() != host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            or parsed.fragment
        ):
            return None
        if resource_kind == "html":
            if host != _STEAM_WIDGET_HOST or parsed.query:
                return None
            match = _STEAM_WIDGET_PATH_RE.fullmatch(parsed.path)
        elif resource_kind == "image":
            if host not in _STEAM_CDN_HOSTS:
                return None
            if parsed.query and not _STEAM_CACHE_QUERY_RE.fullmatch(parsed.query):
                return None
            match = _STEAM_IMAGE_PATH_RE.fullmatch(parsed.path)
        else:
            return None
    except (ValueError, UnicodeError):
        return None
    return match.group(1) if match is not None else None


def _diagnostic_url(url: str) -> str | None:
    """Return a URL safe for diagnostics: no credentials, query, or fragment."""

    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        if not parsed.scheme or not hostname:
            return None
        try:
            port = parsed.port
        except ValueError:
            return None
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        authority = hostname
        if port is not None and not (
            (parsed.scheme.lower() == "https" and port == 443)
            or (parsed.scheme.lower() == "http" and port == 80)
        ):
            authority += f":{port}"
        return urlunsplit((parsed.scheme.lower(), authority, parsed.path, "", ""))
    except (TypeError, ValueError, UnicodeError):
        return None


def _diagnostics_start(diagnostics: dict[str, object] | None) -> None:
    if diagnostics is not None:
        diagnostics.update(
            {
                "reason": "request_failed",
                "http_status": None,
                "final_url": None,
                "content_type": "",
                "redirect_count": 0,
                "body_length": 0,
            }
        )


def _diagnostics_set(
    diagnostics: dict[str, object] | None,
    *,
    reason: str | None = None,
    **values: object,
) -> None:
    if diagnostics is None:
        return
    if reason is not None:
        diagnostics["reason"] = reason
    diagnostics.update(values)


def _acceptable_explicit_proxy_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """A configured proxy may be local, but cannot be an invalid IP target."""

    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return _acceptable_explicit_proxy_ip(address.ipv4_mapped)
        if address.sixtofour is not None or address.teredo is not None:
            return False
    return not (
        address.is_multicast or address.is_unspecified or address.is_reserved
    )


def _is_public_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return _is_public_ip(address.ipv4_mapped)
        # These transition mechanisms can carry a private IPv4 destination.
        if address.sixtofour is not None or address.teredo is not None:
            return False
    return bool(
        address.is_global
        and not address.is_private
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_multicast
        and not address.is_reserved
        and not address.is_unspecified
    )


def is_public_https_url(url: str) -> bool:
    """Perform lexical HTTPS/public-host validation without resolving DNS.

    DNS answers are checked separately by :class:`SafeMediaDownloader` before
    every request. This helper is intended for deciding whether a post URL is
    a candidate; a ``True`` result alone never authorizes a network request.
    """

    raw = str(url or "")
    if not raw or raw != raw.strip() or any(ord(char) <= 0x20 or ord(char) == 0x7F for char in raw):
        return False
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme.lower() != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.fragment
    ):
        return False
    host = _normalized_hostname(parsed.hostname or "")
    return host is not None


async def _system_address_lookup(hostname: str, port: int) -> Sequence[str]:
    """Resolve all stream addresses through the event loop's system resolver."""

    loop = asyncio.get_running_loop()
    records = await loop.getaddrinfo(
        hostname,
        port,
        family=socket.AF_UNSPEC,
        type=socket.SOCK_STREAM,
    )
    addresses: list[str] = []
    for _family, _socktype, _proto, _canonname, sockaddr in records:
        if not sockaddr:
            continue
        value = str(sockaddr[0])
        if value not in addresses:
            addresses.append(value)
    return addresses


class _PinnedResolver(aiohttp.abc.AbstractResolver):
    """aiohttp resolver that can return only the already-validated IPs."""

    def __init__(self, hostname: str, port: int, addresses: Sequence[str]):
        self._hostname = hostname.lower().rstrip(".")
        self._port = int(port)
        self._addresses: tuple[str, ...] = tuple(addresses)

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: int = socket.AF_INET,
    ) -> list[dict[str, object]]:
        requested_host = host.lower().rstrip(".")
        if requested_host != self._hostname or int(port) != self._port:
            raise OSError("Unpinned destination")

        results: list[dict[str, object]] = []
        for raw_address in self._addresses:
            address = ipaddress.ip_address(raw_address)
            address_family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
            if family not in {socket.AF_UNSPEC, address_family}:
                continue
            results.append(
                {
                    "hostname": host,
                    "host": address.compressed,
                    "port": self._port,
                    "family": address_family,
                    "proto": socket.IPPROTO_TCP,
                    "flags": socket.AI_NUMERICHOST,
                }
            )
        if not results:
            raise OSError("No pinned destination")
        return results

    async def close(self) -> None:
        return None


def _response_content_type(headers: object) -> str:
    try:
        value = headers.get("Content-Type", "")  # type: ignore[attr-defined]
        if not value:
            value = headers.get("content-type", "")  # type: ignore[attr-defined]
    except Exception:
        return ""
    return str(value).split(";", 1)[0].strip().lower()


def _response_content_length(headers: object) -> int | None:
    try:
        value = headers.get("Content-Length")  # type: ignore[attr-defined]
        if value is None:
            value = headers.get("content-length")  # type: ignore[attr-defined]
    except Exception:
        return None
    if value is None:
        return None
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return -1
    return parsed if parsed >= 0 else -1


def _at_eof(content: object) -> bool:
    try:
        return bool(content.at_eof())  # type: ignore[attr-defined]
    except Exception:
        return False


def _image_mime_and_format(payload: bytes) -> tuple[str, str] | None:
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", "PNG"
    if payload.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", "JPEG"
    if payload.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", "GIF"
    if len(payload) >= 12 and payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return "image/webp", "WEBP"
    if len(payload) >= 16 and payload[4:8] == b"ftyp":
        brands = [payload[index : index + 4] for index in range(8, min(len(payload), 64), 4)]
        if b"avif" in brands or b"avis" in brands:
            return "image/avif", "AVIF"
    return None


def _validate_image(payload: bytes, declared_type: str) -> str | None:
    sniffed = _image_mime_and_format(payload)
    if sniffed is None:
        return None
    mime, expected_format = sniffed
    declared = {
        "image/jpg": "image/jpeg",
        "image/x-png": "image/png",
    }.get(declared_type, declared_type)
    if declared != mime:
        return None

    # Pillow supplies a second structural check for raster formats it can
    # decode. AVIF is accepted by its strict ISO-BMFF magic check when the
    # installed Pillow build has no AVIF plugin.
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(payload)) as image:
                if image.format != expected_format:
                    return None
                if image.width <= 0 or image.height <= 0:
                    return None
                if image.width * image.height > MAX_IMAGE_PIXELS:
                    return None
                image.verify()
    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
        SyntaxError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        return None
    return mime


class _BodyLimitExceeded(Exception):
    """Internal signal used to discard a response without exposing its URL."""


class SafeMediaDownloader:
    """Cookie-free, bounded HTTP downloader shared for one post capture.

    A downloader instance owns one aggregate image-byte budget, so callers
    should share it across all images and TOC sections belonging to one post.
    Requests create a fresh connector for every redirect hop and pin that
    connector to the public addresses validated for that hop.

    ``address_lookup`` is an internal dependency-injection hook for tests. It
    should return every resolved IP address; unsafe or malformed answers cause
    the request to be rejected. ``steam_proxy_url`` is an explicit HTTP(S)
    proxy used only for calls marked ``provider="steam"``. The Steam origin
    remains DNS-validated and its CONNECT destination is pinned to that IP;
    the proxy host is independently pinned after resolution.
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_image_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
        max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
        max_html_bytes: int = DEFAULT_MAX_HTML_BYTES,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
        address_lookup: AddressLookup | None = None,
        steam_proxy_url: str | None = None,
    ) -> None:
        self.timeout_seconds = self._clamp_timeout(timeout_seconds, DEFAULT_TIMEOUT_SECONDS)
        self.max_image_bytes = self._clamp_size(
            max_image_bytes, DEFAULT_MAX_IMAGE_BYTES, MAX_IMAGE_BYTES
        )
        self.max_total_bytes = self._clamp_size(
            max_total_bytes, DEFAULT_MAX_TOTAL_BYTES, MAX_TOTAL_BYTES
        )
        self.max_html_bytes = self._clamp_size(
            max_html_bytes, DEFAULT_MAX_HTML_BYTES, MAX_HTML_BYTES
        )
        self.max_redirects = min(
            MAX_REDIRECTS,
            max(0, self._int_or_default(max_redirects, DEFAULT_MAX_REDIRECTS)),
        )
        self._address_lookup = address_lookup or _system_address_lookup
        self._steam_proxy_requested = bool(str(steam_proxy_url or ""))
        self._steam_proxy = _parse_explicit_proxy(steam_proxy_url)
        self._total_image_bytes = 0
        self._reserved_image_bytes = 0
        self._budget_lock = asyncio.Lock()
        self._closed = False

    @staticmethod
    def _int_or_default(value: object, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError):
            return default

    @classmethod
    def _clamp_size(cls, value: object, default: int, maximum: int) -> int:
        return min(maximum, max(0, cls._int_or_default(value, default)))

    @staticmethod
    def _clamp_timeout(value: object, default: float) -> float:
        try:
            timeout = float(value)
        except (TypeError, ValueError, OverflowError):
            timeout = default
        if timeout != timeout:  # NaN
            timeout = default
        return min(MAX_TIMEOUT_SECONDS, max(0.1, timeout))

    async def __aenter__(self) -> SafeMediaDownloader:
        if self._closed:
            raise RuntimeError("Downloader is closed")
        return self

    async def __aexit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        await self.close()

    async def close(self) -> None:
        """Mark this request-scoped downloader closed; no persistent session is kept."""

        self._closed = True

    def _url_is_allowed(self, url: str, policy: URLPolicy | None) -> bool:
        if not is_public_https_url(url):
            return False
        if policy is None:
            return True
        try:
            return bool(policy(url))
        except Exception:
            return False

    async def _validated_addresses(self, hostname: str, port: int) -> tuple[str, ...] | None:
        try:
            try:
                literal = ipaddress.ip_address(hostname)
            except ValueError:
                raw_addresses = await self._address_lookup(hostname, port)
            else:
                raw_addresses = (literal.compressed,)
            addresses: list[str] = []
            for raw_address in raw_addresses:
                address_text = str(raw_address)
                if "%" in address_text:
                    return None
                address = ipaddress.ip_address(address_text)
                if not _is_public_ip(address):
                    return None
                normalized = address.compressed
                if normalized not in addresses:
                    addresses.append(normalized)
            return tuple(addresses) if addresses else None
        except (OSError, ValueError, TypeError, asyncio.TimeoutError):
            return None

    async def _validated_proxy_addresses(
        self, hostname: str, port: int
    ) -> tuple[str, ...] | None:
        """Resolve a configured proxy once per hop and pin every valid answer.

        Private and loopback addresses are permitted only for this explicitly
        configured proxy endpoint. Origin addresses always use the stricter
        public-only validator above.
        """

        try:
            try:
                literal = ipaddress.ip_address(hostname)
            except ValueError:
                raw_addresses = await self._address_lookup(hostname, port)
            else:
                raw_addresses = (literal.compressed,)
            addresses: list[str] = []
            for raw_address in raw_addresses:
                address_text = str(raw_address)
                if "%" in address_text:
                    return None
                address = ipaddress.ip_address(address_text)
                if not _acceptable_explicit_proxy_ip(address):
                    return None
                normalized = address.compressed
                if normalized not in addresses:
                    addresses.append(normalized)
            return tuple(addresses) if addresses else None
        except (OSError, ValueError, TypeError, asyncio.TimeoutError):
            return None

    async def _read_image_body(
        self,
        response: object,
        limit: int,
        diagnostics: dict[str, object] | None = None,
    ) -> bytes:
        content = response.content  # type: ignore[attr-defined]
        chunks: list[bytes] = []
        size = 0
        while True:
            per_image_remaining = limit - size
            async with self._budget_lock:
                shared_remaining = (
                    self.max_total_bytes
                    - self._total_image_bytes
                    - self._reserved_image_bytes
                )
                allowance = min(_READ_CHUNK_BYTES, per_image_remaining, shared_remaining)
                if allowance <= 0:
                    if _at_eof(content):
                        break
                    raise _BodyLimitExceeded
                self._reserved_image_bytes += allowance

            try:
                chunk = await content.read(allowance)
            except BaseException:
                async with self._budget_lock:
                    self._reserved_image_bytes -= allowance
                raise

            async with self._budget_lock:
                self._reserved_image_bytes -= allowance
                self._total_image_bytes += len(chunk)
            if not chunk:
                break
            size += len(chunk)
            _diagnostics_set(diagnostics, body_length=size)
            if len(chunk) > allowance or size > limit:
                raise _BodyLimitExceeded
            chunks.append(bytes(chunk))
        return b"".join(chunks)

    async def _read_html_body(
        self,
        response: object,
        limit: int,
        diagnostics: dict[str, object] | None = None,
    ) -> bytes:
        content = response.content  # type: ignore[attr-defined]
        chunks: list[bytes] = []
        size = 0
        while True:
            remaining = limit - size
            if remaining <= 0:
                if _at_eof(content):
                    break
                raise _BodyLimitExceeded
            chunk = await content.read(min(_READ_CHUNK_BYTES, remaining))
            if not chunk:
                break
            size += len(chunk)
            _diagnostics_set(diagnostics, body_length=size)
            if size > limit:
                raise _BodyLimitExceeded
            chunks.append(bytes(chunk))
        return b"".join(chunks)

    async def _fetch(
        self,
        url: str,
        *,
        resource_kind: str,
        allowed_url: URLPolicy | None,
        max_bytes: int,
        timeout: float,
        provider: str | None,
        diagnostics: dict[str, object] | None,
    ) -> tuple[bytes, str] | None:
        _diagnostics_start(diagnostics)
        if provider not in {None, "steam"}:
            return None
        if self._closed or max_bytes <= 0 or not self._url_is_allowed(url, allowed_url):
            return None
        steam_app_id: str | None = None
        if provider == "steam":
            steam_app_id = _steam_url_app_id(url, resource_kind)
            if steam_app_id is None:
                return None
            # Do not silently bypass a malformed explicitly configured proxy.
            if self._steam_proxy_requested and self._steam_proxy is None:
                return None

        current_url = url
        seen: set[str] = set()
        redirect_count = 0
        for _redirect_index in range(self.max_redirects + 1):
            if (
                current_url in seen
                or not self._url_is_allowed(current_url, allowed_url)
            ):
                _diagnostics_set(
                    diagnostics,
                    reason="redirect_rejected" if redirect_count else "request_failed",
                )
                return None
            if provider == "steam" and (
                _steam_url_app_id(current_url, resource_kind) != steam_app_id
            ):
                _diagnostics_set(
                    diagnostics,
                    reason="redirect_rejected" if redirect_count else "request_failed",
                )
                return None
            seen.add(current_url)
            try:
                parsed = urlsplit(current_url)
                hostname = _normalized_hostname(parsed.hostname or "")
                port = parsed.port or 443
            except ValueError:
                _diagnostics_set(
                    diagnostics,
                    reason="redirect_rejected" if redirect_count else "request_failed",
                )
                return None
            if hostname is None:
                _diagnostics_set(
                    diagnostics,
                    reason="redirect_rejected" if redirect_count else "request_failed",
                )
                return None
            _diagnostics_set(
                diagnostics,
                final_url=_diagnostic_url(current_url),
                http_status=None,
                content_type="",
                redirect_count=redirect_count,
            )
            addresses = await self._validated_addresses(hostname, port)
            if addresses is None:
                _diagnostics_set(
                    diagnostics,
                    reason="redirect_rejected" if redirect_count else "request_failed",
                )
                return None

            proxy = self._steam_proxy if provider == "steam" else None
            proxy_addresses: tuple[str, ...] | None = None
            if proxy is not None:
                proxy_addresses = await self._validated_proxy_addresses(
                    proxy.hostname, proxy.port
                )
                if proxy_addresses is None:
                    _diagnostics_set(diagnostics, reason="request_failed")
                    return None

            # aiohttp's built-in proxy connector builds CONNECT authority from
            # the request URL. Use the already validated origin IP there, while
            # explicitly preserving the original HTTP Host and TLS name.
            if proxy is not None:
                try:
                    request_url = str(URL(current_url).with_host(addresses[0]))
                except (TypeError, ValueError):
                    _diagnostics_set(diagnostics, reason="request_failed")
                    return None
                resolver_hostname, resolver_port = proxy.hostname, proxy.port
                resolver_addresses = proxy_addresses
            else:
                request_url = current_url
                resolver_hostname, resolver_port = hostname, port
                resolver_addresses = addresses

            connector = aiohttp.TCPConnector(
                resolver=_PinnedResolver(
                    resolver_hostname, resolver_port, resolver_addresses
                ),
                use_dns_cache=False,
                force_close=True,
                family=socket.AF_UNSPEC,
            )
            request_timeout = aiohttp.ClientTimeout(
                total=timeout,
                connect=min(timeout, 8.0),
                sock_connect=min(timeout, 8.0),
                sock_read=timeout,
            )
            try:
                session_options: dict[str, object] = {
                    "connector": connector,
                    "timeout": request_timeout,
                    "trust_env": False,
                    "cookie_jar": aiohttp.DummyCookieJar(),
                    "auto_decompress": False,
                }
                if proxy is not None:
                    session_options["request_class"] = _SteamPinnedProxyRequest
                async with aiohttp.ClientSession(**session_options) as session:
                    headers = {
                        "Accept": (
                            "image/avif,image/webp,image/png,image/jpeg,image/gif"
                            if resource_kind == "image"
                            else "text/html"
                        ),
                        "Accept-Encoding": "identity",
                        "User-Agent": "astrbot-safe-media/1.0",
                    }
                    request_options: dict[str, object] = {
                        "headers": headers,
                        "allow_redirects": False,
                    }
                    if proxy is not None:
                        request_options["proxy"] = proxy.url
                        request_options["server_hostname"] = hostname
                        if proxy.username is not None:
                            request_options["proxy_headers"] = {
                                "Proxy-Authorization": _encode_proxy_basic_auth(
                                    proxy.username, proxy.password or ""
                                )
                            }
                    async with session.get(request_url, **request_options) as response:
                        status = int(response.status)
                        content_type = _response_content_type(response.headers)
                        _diagnostics_set(
                            diagnostics,
                            http_status=status,
                            final_url=_diagnostic_url(current_url),
                            content_type=content_type,
                            redirect_count=redirect_count,
                        )
                        if status in _REDIRECT_STATUSES:
                            redirect_count += 1
                            _diagnostics_set(
                                diagnostics, redirect_count=redirect_count
                            )
                            if redirect_count > self.max_redirects:
                                _diagnostics_set(
                                    diagnostics, reason="redirect_rejected"
                                )
                                return None
                            location = str(response.headers.get("Location", ""))
                            if not location:
                                _diagnostics_set(
                                    diagnostics, reason="redirect_rejected"
                                )
                                return None
                            try:
                                current_url = urljoin(current_url, location)
                            except ValueError:
                                _diagnostics_set(
                                    diagnostics, reason="redirect_rejected"
                                )
                                return None
                            # Validate before the next DNS lookup or connection.
                            if (
                                not self._url_is_allowed(current_url, allowed_url)
                                or (
                                    provider == "steam"
                                    and _steam_url_app_id(current_url, resource_kind)
                                    != steam_app_id
                                )
                            ):
                                _diagnostics_set(
                                    diagnostics, reason="redirect_rejected"
                                )
                                return None
                            continue
                        if status != 200:
                            _diagnostics_set(diagnostics, reason="http_status")
                            return None

                        content_encoding = str(
                            response.headers.get("Content-Encoding", "")
                        ).strip().lower()
                        if content_encoding not in {"", "identity"}:
                            _diagnostics_set(diagnostics, reason="body_rejected")
                            return None
                        if resource_kind == "image":
                            if content_type not in {
                                "image/avif",
                                "image/gif",
                                "image/jpeg",
                                "image/jpg",
                                "image/png",
                                "image/webp",
                                "image/x-png",
                            }:
                                _diagnostics_set(
                                    diagnostics, reason="content_type_rejected"
                                )
                                return None
                        elif content_type != "text/html":
                            _diagnostics_set(
                                diagnostics, reason="content_type_rejected"
                            )
                            return None

                        content_length = _response_content_length(response.headers)
                        if content_length == -1 or (
                            content_length is not None
                            and (content_length > max_bytes or content_length > MAX_TOTAL_BYTES)
                        ):
                            _diagnostics_set(diagnostics, reason="body_rejected")
                            return None
                        if resource_kind == "image":
                            if content_length is not None:
                                async with self._budget_lock:
                                    available = (
                                        self.max_total_bytes
                                        - self._total_image_bytes
                                        - self._reserved_image_bytes
                                    )
                                if content_length > available:
                                    _diagnostics_set(
                                        diagnostics, reason="body_rejected"
                                    )
                                    return None
                            payload = await self._read_image_body(
                                response, max_bytes, diagnostics
                            )
                            validated_type = _validate_image(payload, content_type)
                            if validated_type is None:
                                _diagnostics_set(
                                    diagnostics, reason="body_rejected"
                                )
                                return None
                            _diagnostics_set(diagnostics, reason="success")
                            return payload, validated_type

                        payload = await self._read_html_body(
                            response, max_bytes, diagnostics
                        )
                        _diagnostics_set(diagnostics, reason="success")
                        return payload, "text/html"
            except _BodyLimitExceeded:
                _diagnostics_set(diagnostics, reason="body_rejected")
                return None
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError):
                _diagnostics_set(diagnostics, reason="request_failed")
                return None
            finally:
                if not connector.closed:
                    await connector.close()
        return None

    @staticmethod
    def _per_call_size(value: object | None, default: int) -> int:
        if value is None:
            return default
        try:
            parsed = int(value)
        except (TypeError, ValueError, OverflowError):
            return default
        return max(0, parsed)

    def _per_call_timeout(self, value: object | None) -> float:
        if value is None:
            return self.timeout_seconds
        return min(self.timeout_seconds, self._clamp_timeout(value, self.timeout_seconds))

    async def fetch_image(
        self,
        url: str,
        *,
        allowed_url: URLPolicy | None = None,
        max_bytes: int | None = None,
        timeout: float | None = None,
        provider: str | None = None,
        diagnostics: dict[str, object] | None = None,
    ) -> SafeImage | None:
        """Fetch a public HTTPS raster image, applying policy to every hop.

        When supplied, ``diagnostics`` is filled in place for this request.
        Pass a distinct dict to each concurrent call; no diagnostics are kept
        on the downloader instance. Its final URL omits credentials, query,
        and fragment data.
        """

        limit = min(
            self.max_image_bytes,
            self._per_call_size(max_bytes, self.max_image_bytes),
        )
        duration = self._per_call_timeout(timeout)
        try:
            fetched = await asyncio.wait_for(
                self._fetch(
                    url,
                    resource_kind="image",
                    allowed_url=allowed_url,
                    max_bytes=limit,
                    timeout=duration,
                    provider=provider,
                    diagnostics=diagnostics,
                ),
                timeout=duration,
            )
        except (asyncio.TimeoutError, aiohttp.ClientError, OSError, ValueError):
            _diagnostics_set(diagnostics, reason="request_failed")
            return None
        if fetched is None:
            return None
        data, content_type = fetched
        return SafeImage(data=data, content_type=content_type)

    async def fetch_html(
        self,
        url: str,
        *,
        allowed_url: URLPolicy,
        max_bytes: int = 512 * 1024,
        timeout: float | None = None,
        provider: str | None = None,
        diagnostics: dict[str, object] | None = None,
    ) -> SafeHtml | None:
        """Fetch caller-allowlisted HTML with the same pinned, cookie-free path.

        An explicit URL policy is required because HTML must be kept to a
        narrowly trusted endpoint by the caller. The response must declare
        ``text/html`` and is bounded independently from the shared image budget.
        A fresh diagnostics dict may be passed for per-request, redacted detail.
        """

        limit = min(
            self.max_html_bytes,
            self._per_call_size(max_bytes, min(512 * 1024, self.max_html_bytes)),
        )
        duration = self._per_call_timeout(timeout)
        try:
            fetched = await asyncio.wait_for(
                self._fetch(
                    url,
                    resource_kind="html",
                    allowed_url=allowed_url,
                    max_bytes=limit,
                    timeout=duration,
                    provider=provider,
                    diagnostics=diagnostics,
                ),
                timeout=duration,
            )
        except (asyncio.TimeoutError, aiohttp.ClientError, OSError, ValueError):
            _diagnostics_set(diagnostics, reason="request_failed")
            return None
        if fetched is None:
            return None
        data, content_type = fetched
        return SafeHtml(data=data, content_type=content_type)


__all__ = [
    "SafeHtml",
    "SafeImage",
    "SafeMediaDownloader",
    "is_public_https_url",
]
