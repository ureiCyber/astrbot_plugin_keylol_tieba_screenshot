import unittest
from io import BytesIO

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


if __name__ == "__main__":
    unittest.main()
