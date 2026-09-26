import asyncio
import base64
import unittest
from io import BytesIO
from unittest.mock import patch

from PIL import Image

import safe_media


def png_bytes():
    stream = BytesIO()
    with Image.new("RGB", (3, 2), "red") as image:
        image.save(stream, format="PNG")
    return stream.getvalue()


class SafeMediaPolicyTests(unittest.TestCase):
    def test_public_https_policy_rejects_http_credentials_private_and_nonstandard_ports(self):
        self.assertTrue(safe_media.is_public_https_url("https://example.org/image.png"))
        for value in (
            "http://example.org/image.png",
            "https://user:pass@example.org/image.png",
            "https://example.org:8443/image.png",
            "https://localhost/image.png",
            "https://127.0.0.1/image.png",
            "https://192.168.1.2/image.png",
            "https://[::1]/image.png",
        ):
            with self.subTest(value=value):
                self.assertFalse(safe_media.is_public_https_url(value))

    def test_image_magic_and_declared_mime_must_match(self):
        payload = png_bytes()
        self.assertEqual(safe_media._validate_image(payload, "image/png"), "image/png")
        self.assertIsNone(safe_media._validate_image(payload, "text/html"))
        self.assertIsNone(safe_media._validate_image(b"<html>not an image</html>", "image/png"))

    def test_dns_answers_are_all_required_to_be_public(self):
        downloader = safe_media.SafeMediaDownloader(
            address_lookup=lambda _host, _port: None  # type: ignore[arg-type]
        )
        self.assertFalse(safe_media._is_public_ip(__import__("ipaddress").ip_address("127.0.0.1")))
        self.assertTrue(safe_media._is_public_ip(__import__("ipaddress").ip_address("93.184.216.34")))
        self.assertIsNotNone(downloader)


class SafeMediaResultTests(unittest.TestCase):
    def test_result_body_alias_is_available_for_provider_adapter(self):
        result = safe_media.SafeImage(b"bytes", "image/png")
        self.assertEqual(result.body, b"bytes")


class _FakeContent:
    def __init__(self, payload: bytes = b"") -> None:
        self.payload = payload
        self.read_offset = 0

    async def read(self, size: int) -> bytes:
        chunk = self.payload[self.read_offset : self.read_offset + size]
        self.read_offset += len(chunk)
        return chunk

    def at_eof(self) -> bool:
        return self.read_offset >= len(self.payload)


class _FakeResponse:
    def __init__(self, status: int, headers: dict[str, str], payload: bytes = b"") -> None:
        self.status = status
        self.headers = headers
        self.content = _FakeContent(payload)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


class _FakeConnector:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class SafeMediaRequestBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def _run_fetch(
        self,
        *,
        address_lookup,
        responses: list[object],
        resource_kind: str = "html",
        url: str = "https://media.example.org/item",
        allowed_url=None,
        provider: str | None = None,
        diagnostics: dict[str, object] | None = None,
        steam_proxy_url: str | None = None,
    ):
        requests: list[tuple[str, dict[str, object]]] = []
        sessions: list[dict[str, object]] = []
        connectors: list[_FakeConnector] = []

        class FakeSession:
            def __init__(self, **kwargs: object) -> None:
                sessions.append(kwargs)
                self.kwargs = kwargs

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args: object) -> None:
                return None

            def get(self, request_url: str, **kwargs: object):
                requests.append((request_url, kwargs))
                if not responses:
                    raise AssertionError("unexpected additional HTTP request")
                response = responses.pop(0)
                if isinstance(response, Exception):
                    raise response
                return response

        def connector_factory(**kwargs: object) -> _FakeConnector:
            connector = _FakeConnector(**kwargs)
            connectors.append(connector)
            return connector

        downloader = safe_media.SafeMediaDownloader(
            address_lookup=address_lookup,
            steam_proxy_url=steam_proxy_url,
        )
        with (
            patch.object(safe_media.aiohttp, "ClientSession", FakeSession),
            patch.object(safe_media.aiohttp, "TCPConnector", connector_factory),
        ):
            if resource_kind == "image":
                result = await downloader.fetch_image(
                    url,
                    allowed_url=allowed_url,
                    max_bytes=1024,
                    provider=provider,
                    diagnostics=diagnostics,
                )
            else:
                result = await downloader.fetch_html(
                    url,
                    allowed_url=allowed_url or (lambda _candidate: True),
                    max_bytes=1024,
                    provider=provider,
                    diagnostics=diagnostics,
                )
        return result, requests, sessions, connectors

    async def test_private_dns_answer_prevents_any_connection(self):
        lookups: list[str] = []

        async def lookup(host: str, _port: int):
            lookups.append(host)
            return ["93.184.216.34", "10.0.0.8"]

        result, requests, sessions, connectors = await self._run_fetch(
            address_lookup=lookup,
            responses=[_FakeResponse(200, {"Content-Type": "text/html"}, b"ok")],
            url="https://media.example.org/item",
        )

        self.assertIsNone(result)
        self.assertEqual(lookups, ["media.example.org"])
        self.assertEqual(requests, [])
        self.assertEqual(sessions, [])
        self.assertEqual(connectors, [])

    async def test_redirect_policy_is_checked_before_resolving_or_connecting_next_hop(self):
        lookups: list[str] = []

        async def lookup(host: str, _port: int):
            lookups.append(host)
            return ["93.184.216.34"]

        start = "https://media.example.org/item"
        result, requests, _sessions, _connectors = await self._run_fetch(
            address_lookup=lookup,
            responses=[
                _FakeResponse(
                    302,
                    {"Location": "https://other.example.org/private"},
                )
            ],
            url=start,
            allowed_url=lambda candidate: candidate == start,
        )

        self.assertIsNone(result)
        self.assertEqual(lookups, ["media.example.org"])
        self.assertEqual([url for url, _ in requests], [start])

    async def test_each_redirect_hop_gets_fresh_dns_validation(self):
        lookups: list[str] = []

        async def lookup(host: str, _port: int):
            lookups.append(host)
            return ["93.184.216.34"] if host == "media.example.org" else ["127.0.0.1"]

        start = "https://media.example.org/item"
        result, requests, _sessions, connectors = await self._run_fetch(
            address_lookup=lookup,
            responses=[
                _FakeResponse(
                    302,
                    {"Location": "https://redirect.example.org/item"},
                )
            ],
            url=start,
            allowed_url=lambda _candidate: True,
        )

        self.assertIsNone(result)
        self.assertEqual(lookups, ["media.example.org", "redirect.example.org"])
        self.assertEqual([url for url, _ in requests], [start])
        self.assertEqual(len(connectors), 1)

    async def test_request_is_cookie_free_and_does_not_use_environment_proxy(self):
        async def lookup(_host: str, _port: int):
            return ["93.184.216.34"]

        result, requests, sessions, _connectors = await self._run_fetch(
            address_lookup=lookup,
            responses=[
                _FakeResponse(
                    200,
                    {"Content-Type": "text/html", "Set-Cookie": "sid=secret"},
                    b"safe html",
                )
            ],
            url="https://media.example.org/item",
        )

        self.assertIsNotNone(result)
        self.assertEqual(len(sessions), 1)
        self.assertFalse(sessions[0]["trust_env"])
        self.assertIsInstance(sessions[0]["cookie_jar"], safe_media.aiohttp.DummyCookieJar)
        self.assertNotIn("Cookie", requests[0][1]["headers"])
        self.assertNotIn("cookie", requests[0][1]["headers"])

    async def test_declared_mime_and_image_bytes_must_both_be_valid(self):
        async def lookup(_host: str, _port: int):
            return ["93.184.216.34"]

        result, requests, _sessions, _connectors = await self._run_fetch(
            address_lookup=lookup,
            responses=[_FakeResponse(200, {"Content-Type": "text/html"}, png_bytes())],
            resource_kind="image",
            url="https://media.example.org/image.png",
        )
        self.assertIsNone(result)
        self.assertEqual(len(requests), 1)

        result, requests, _sessions, _connectors = await self._run_fetch(
            address_lookup=lookup,
            responses=[_FakeResponse(200, {"Content-Type": "image/jpeg"}, png_bytes())],
            resource_kind="image",
            url="https://media.example.org/image.png",
        )
        self.assertIsNone(result)
        self.assertEqual(len(requests), 1)

    async def test_html_fetch_rejects_non_html_content_type(self):
        async def lookup(_host: str, _port: int):
            return ["93.184.216.34"]

        diagnostics: dict[str, object] = {}
        result, requests, _sessions, _connectors = await self._run_fetch(
            address_lookup=lookup,
            responses=[_FakeResponse(200, {"Content-Type": "application/json"}, b"{}")],
            diagnostics=diagnostics,
        )
        self.assertIsNone(result)
        self.assertEqual(len(requests), 1)
        self.assertEqual(diagnostics["reason"], "content_type_rejected")

    async def test_diagnostics_record_success_and_redact_query_credentials(self):
        async def lookup(_host: str, _port: int):
            return ["93.184.216.34"]

        diagnostics: dict[str, object] = {}
        result, _requests, _sessions, _connectors = await self._run_fetch(
            address_lookup=lookup,
            responses=[_FakeResponse(200, {"Content-Type": "text/html; charset=utf-8"}, b"safe html")],
            url="https://media.example.org/item?token=do-not-log",
            diagnostics=diagnostics,
        )

        self.assertIsNotNone(result)
        self.assertEqual(
            diagnostics,
            {
                "reason": "success",
                "http_status": 200,
                "final_url": "https://media.example.org/item",
                "content_type": "text/html",
                "redirect_count": 0,
                "body_length": 9,
            },
        )
        self.assertNotIn("do-not-log", repr(diagnostics))

    async def test_diagnostics_distinguish_status_body_and_network_failures(self):
        async def lookup(_host: str, _port: int):
            return ["93.184.216.34"]

        cases = (
            (
                _FakeResponse(503, {"Content-Type": "text/html"}, b"unavailable"),
                "http_status",
            ),
            (
                _FakeResponse(200, {"Content-Type": "text/html", "Content-Length": "9999"}),
                "body_rejected",
            ),
            (safe_media.aiohttp.ClientConnectionError(), "request_failed"),
        )
        for response, expected_reason in cases:
            diagnostics: dict[str, object] = {}
            result, _requests, _sessions, _connectors = await self._run_fetch(
                address_lookup=lookup,
                responses=[response],  # type: ignore[list-item]
                diagnostics=diagnostics,
            )
            self.assertIsNone(result)
            self.assertEqual(diagnostics["reason"], expected_reason)

    async def test_diagnostics_distinguish_rejected_redirect(self):
        async def lookup(_host: str, _port: int):
            return ["93.184.216.34"]

        start = "https://media.example.org/item?token=hidden"
        diagnostics: dict[str, object] = {}
        result, requests, _sessions, _connectors = await self._run_fetch(
            address_lookup=lookup,
            responses=[
                _FakeResponse(
                    302,
                    {"Location": "https://other.example.org/item?secret=hidden"},
                )
            ],
            url=start,
            allowed_url=lambda candidate: candidate == start,
            diagnostics=diagnostics,
        )

        self.assertIsNone(result)
        self.assertEqual([url for url, _ in requests], [start])
        self.assertEqual(diagnostics["reason"], "redirect_rejected")
        self.assertEqual(diagnostics["http_status"], 302)
        self.assertEqual(diagnostics["redirect_count"], 1)
        self.assertEqual(diagnostics["final_url"], "https://media.example.org/item")
        self.assertNotIn("secret", repr(diagnostics))

    async def test_steam_proxy_pins_connect_target_and_preserves_host_and_tls_name(self):
        lookups: list[tuple[str, int]] = []

        async def lookup(host: str, port: int):
            lookups.append((host, port))
            return ["93.184.216.34"]

        url = "https://store.steampowered.com/widget/3575980/"
        diagnostics: dict[str, object] = {}
        result, requests, sessions, connectors = await self._run_fetch(
            address_lookup=lookup,
            responses=[_FakeResponse(200, {"Content-Type": "text/html"}, b"widget")],
            url=url,
            allowed_url=lambda candidate: candidate == url,
            provider="steam",
            diagnostics=diagnostics,
            steam_proxy_url="http://proxy-user:proxy-secret@127.0.0.1:8899",
        )

        self.assertIsNotNone(result)
        self.assertEqual(lookups, [("store.steampowered.com", 443)])
        self.assertEqual(requests[0][0], "https://93.184.216.34/widget/3575980/")
        options = requests[0][1]
        self.assertEqual(options["proxy"], "http://127.0.0.1:8899")
        self.assertEqual(options["server_hostname"], "store.steampowered.com")
        self.assertNotIn("Host", options["headers"])
        self.assertIs(sessions[0]["request_class"], safe_media._SteamPinnedProxyRequest)
        proxy_auth = options["proxy_headers"]["Proxy-Authorization"]  # type: ignore[index]
        self.assertTrue(proxy_auth.startswith("Basic "))
        resolver = connectors[0].kwargs["resolver"]
        self.assertEqual(resolver._hostname, "127.0.0.1")
        self.assertEqual(resolver._addresses, ("127.0.0.1",))
        self.assertFalse(sessions[0]["trust_env"])
        self.assertEqual(diagnostics["reason"], "success")
        self.assertNotIn("proxy-secret", repr(diagnostics))

    async def test_authenticated_steam_proxy_works_without_aiohttp_encode_helper(self):
        async def lookup(_host: str, _port: int):
            return ["93.184.216.34"]

        url = "https://store.steampowered.com/widget/3575980/"
        # This helper is absent in aiohttp 3.9-3.13. Keep the request working
        # there while exercising UTF-8 credentials on newer aiohttp versions.
        with patch.object(safe_media.aiohttp, "encode_basic_auth", None, create=True):
            result, requests, _sessions, _connectors = await self._run_fetch(
                address_lookup=lookup,
                responses=[_FakeResponse(200, {"Content-Type": "text/html"}, b"widget")],
                url=url,
                allowed_url=lambda candidate: candidate == url,
                provider="steam",
                steam_proxy_url=(
                    "http://proxy-user:%E5%AF%86%E7%A0%81@127.0.0.1:8899"
                ),
            )

        self.assertIsNotNone(result)
        proxy_headers = requests[0][1]["proxy_headers"]
        self.assertEqual(
            proxy_headers["Proxy-Authorization"],
            "Basic " + base64.b64encode("proxy-user:密码".encode("utf-8")).decode("ascii"),
        )

    async def test_steam_proxy_is_not_used_for_other_providers(self):
        async def lookup(_host: str, _port: int):
            return ["93.184.216.34"]

        result, requests, _sessions, connectors = await self._run_fetch(
            address_lookup=lookup,
            responses=[_FakeResponse(200, {"Content-Type": "text/html"}, b"ordinary")],
            url="https://media.example.org/item",
            steam_proxy_url="http://127.0.0.1:8899",
        )

        self.assertIsNotNone(result)
        self.assertEqual(requests[0][0], "https://media.example.org/item")
        self.assertNotIn("proxy", requests[0][1])
        self.assertNotIn("server_hostname", requests[0][1])
        resolver = connectors[0].kwargs["resolver"]
        self.assertEqual(resolver._hostname, "media.example.org")

    async def test_steam_proxy_does_not_allow_private_origin_or_other_app_redirect(self):
        url = "https://store.steampowered.com/widget/3575980/"
        diagnostics: dict[str, object] = {}

        async def private_lookup(_host: str, _port: int):
            return ["10.0.0.7"]

        result, requests, _sessions, _connectors = await self._run_fetch(
            address_lookup=private_lookup,
            responses=[_FakeResponse(200, {"Content-Type": "text/html"}, b"ignored")],
            url=url,
            allowed_url=lambda _candidate: True,
            provider="steam",
            diagnostics=diagnostics,
            steam_proxy_url="http://127.0.0.1:8899",
        )
        self.assertIsNone(result)
        self.assertEqual(requests, [])
        self.assertEqual(diagnostics["reason"], "request_failed")

        async def public_lookup(_host: str, _port: int):
            return ["93.184.216.34"]

        diagnostics = {}
        result, requests, _sessions, _connectors = await self._run_fetch(
            address_lookup=public_lookup,
            responses=[
                _FakeResponse(302, {"Location": "https://store.steampowered.com/widget/4813850/"})
            ],
            url=url,
            allowed_url=lambda _candidate: True,
            provider="steam",
            diagnostics=diagnostics,
            steam_proxy_url="http://127.0.0.1:8899",
        )
        self.assertIsNone(result)
        self.assertEqual(len(requests), 1)
        self.assertEqual(diagnostics["reason"], "redirect_rejected")
        self.assertEqual(diagnostics["redirect_count"], 1)

    async def test_real_aiohttp_connects_to_pinned_ip_and_sends_original_tls_sni(self):
        observed: asyncio.Future[tuple[str, str, str]] = asyncio.get_running_loop().create_future()

        def client_hello_sni(payload: bytes) -> str:
            if not payload or payload[0] != 1:
                raise AssertionError("expected TLS ClientHello")
            offset = 1 + 3 + 2 + 32
            session_id_length = payload[offset]
            offset += 1 + session_id_length
            cipher_length = int.from_bytes(payload[offset : offset + 2], "big")
            offset += 2 + cipher_length
            compression_length = payload[offset]
            offset += 1 + compression_length
            extensions_length = int.from_bytes(payload[offset : offset + 2], "big")
            offset += 2
            extensions_end = offset + extensions_length
            while offset + 4 <= extensions_end:
                extension_type = int.from_bytes(payload[offset : offset + 2], "big")
                extension_length = int.from_bytes(payload[offset + 2 : offset + 4], "big")
                offset += 4
                extension = payload[offset : offset + extension_length]
                offset += extension_length
                if extension_type != 0:
                    continue
                name_offset = 2
                while name_offset + 3 <= len(extension):
                    name_type = extension[name_offset]
                    name_length = int.from_bytes(
                        extension[name_offset + 1 : name_offset + 3], "big"
                    )
                    name_offset += 3
                    value = extension[name_offset : name_offset + name_length]
                    name_offset += name_length
                    if name_type == 0:
                        return value.decode("ascii")
            raise AssertionError("ClientHello omitted SNI")

        async def proxy_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
            try:
                connect_line = (await reader.readline()).decode("ascii").strip()
                connect_headers: dict[str, str] = {}
                while True:
                    header_line = await reader.readline()
                    if header_line == b"\r\n":
                        break
                    name, separator, value = header_line.decode("ascii").partition(":")
                    if separator:
                        connect_headers[name.lower()] = value.strip()
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
                record_header = await asyncio.wait_for(reader.readexactly(5), 2)
                record_length = int.from_bytes(record_header[3:5], "big")
                payload = await asyncio.wait_for(reader.readexactly(record_length), 2)
                observed.set_result(
                    (connect_line, connect_headers.get("host", ""), client_hello_sni(payload))
                )
            except Exception as error:
                if not observed.done():
                    observed.set_exception(error)
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(proxy_handler, "127.0.0.1", 0)
        proxy_port = server.sockets[0].getsockname()[1]
        url = "https://store.steampowered.com/widget/3575980/"

        async def lookup(host: str, _port: int):
            self.assertEqual(host, "store.steampowered.com")
            return ["93.184.216.34"]

        downloader = safe_media.SafeMediaDownloader(
            address_lookup=lookup,
            steam_proxy_url=f"http://127.0.0.1:{proxy_port}",
            timeout_seconds=3,
        )
        diagnostics: dict[str, object] = {}
        try:
            result = await downloader.fetch_html(
                url,
                allowed_url=lambda candidate: candidate == url,
                provider="steam",
                diagnostics=diagnostics,
                timeout=3,
            )
            connect_line, connect_host, sni = await asyncio.wait_for(observed, 3)
        finally:
            server.close()
            await server.wait_closed()

        # The local tunnel ends after the ClientHello; a failed origin TLS
        # handshake is expected here. The captured wire values verify aiohttp
        # used the validated IP for CONNECT and retained Steam's TLS name.
        self.assertIsNone(result)
        self.assertEqual(connect_line, "CONNECT 93.184.216.34:443 HTTP/1.1")
        self.assertEqual(connect_host, "93.184.216.34")
        self.assertEqual(sni, "store.steampowered.com")
        self.assertEqual(diagnostics["reason"], "request_failed")

    async def test_steam_proxy_request_restores_original_host_after_connect(self):
        request = safe_media._SteamPinnedProxyRequest(
            "GET",
            safe_media.URL("https://93.184.216.34/widget/3575980/"),
            proxy=safe_media.URL("http://127.0.0.1:8899"),
            server_hostname="store.steampowered.com",
            loop=asyncio.get_running_loop(),
        )
        self.assertEqual(request.headers["Host"], "93.184.216.34")
        seen_hosts: list[str] = []

        async def capture_parent_send(instance, _connection):
            seen_hosts.append(instance.headers["Host"])
            return "sent"

        with patch.object(safe_media.aiohttp.ClientRequest, "send", capture_parent_send):
            response = await request.send(object())

        self.assertEqual(response, "sent")
        self.assertEqual(seen_hosts, ["store.steampowered.com"])

    async def test_concurrent_calls_keep_diagnostics_separate(self):
        async def lookup(host: str, _port: int):
            await asyncio.sleep(0)
            return ["93.184.216.34"]

        responses = {
            "https://one.example.org/a?token=one": _FakeResponse(
                200, {"Content-Type": "text/html"}, b"one"
            ),
            "https://two.example.org/b?token=two": _FakeResponse(
                404, {"Content-Type": "text/html"}, b""
            ),
        }
        requests: list[tuple[str, dict[str, object]]] = []

        class FakeSession:
            def __init__(self, **_kwargs: object) -> None:
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args: object) -> None:
                return None

            def get(self, request_url: str, **kwargs: object):
                requests.append((request_url, kwargs))
                return responses[request_url]

        def connector_factory(**kwargs: object) -> _FakeConnector:
            return _FakeConnector(**kwargs)

        downloader = safe_media.SafeMediaDownloader(address_lookup=lookup)
        first_diagnostics: dict[str, object] = {}
        second_diagnostics: dict[str, object] = {}
        with (
            patch.object(safe_media.aiohttp, "ClientSession", FakeSession),
            patch.object(safe_media.aiohttp, "TCPConnector", connector_factory),
        ):
            first, second = await asyncio.gather(
                downloader.fetch_html(
                    "https://one.example.org/a?token=one",
                    allowed_url=lambda _url: True,
                    diagnostics=first_diagnostics,
                ),
                downloader.fetch_html(
                    "https://two.example.org/b?token=two",
                    allowed_url=lambda _url: True,
                    diagnostics=second_diagnostics,
                ),
            )

        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(first_diagnostics["reason"], "success")
        self.assertEqual(first_diagnostics["body_length"], 3)
        self.assertEqual(first_diagnostics["final_url"], "https://one.example.org/a")
        self.assertEqual(second_diagnostics["reason"], "http_status")
        self.assertEqual(second_diagnostics["http_status"], 404)
        self.assertEqual(second_diagnostics["body_length"], 0)
        self.assertNotIn("token=", repr(first_diagnostics))
        self.assertNotIn("token=", repr(second_diagnostics))


if __name__ == "__main__":
    unittest.main()
