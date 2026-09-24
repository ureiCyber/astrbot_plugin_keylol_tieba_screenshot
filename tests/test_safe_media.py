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
        responses: list[_FakeResponse],
        resource_kind: str = "html",
        url: str = "https://media.example.org/item",
        allowed_url=None,
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
                return responses.pop(0)

        def connector_factory(**kwargs: object) -> _FakeConnector:
            connector = _FakeConnector(**kwargs)
            connectors.append(connector)
            return connector

        downloader = safe_media.SafeMediaDownloader(address_lookup=address_lookup)
        with (
            patch.object(safe_media.aiohttp, "ClientSession", FakeSession),
            patch.object(safe_media.aiohttp, "TCPConnector", connector_factory),
        ):
            if resource_kind == "image":
                result = await downloader.fetch_image(
                    url, allowed_url=allowed_url, max_bytes=1024
                )
            else:
                result = await downloader.fetch_html(
                    url, allowed_url=allowed_url or (lambda _candidate: True), max_bytes=1024
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

        result, requests, _sessions, _connectors = await self._run_fetch(
            address_lookup=lookup,
            responses=[_FakeResponse(200, {"Content-Type": "application/json"}, b"{}")],
        )
        self.assertIsNone(result)
        self.assertEqual(len(requests), 1)


if __name__ == "__main__":
    unittest.main()
