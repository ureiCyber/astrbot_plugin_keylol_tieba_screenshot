import unittest

import keylol_browser


class KeylolBrowserUrlTests(unittest.TestCase):
    def test_short_url_is_normalized_to_https_first_page_without_mobile_flag(self):
        normalized = keylol_browser.normalize_keylol_browser_url(
            "<http://www.keylol.com/t1047774-1-1/?foo=bar&mobile=yes>"
        )
        self.assertEqual(
            normalized,
            "https://www.keylol.com/t1047774-1-1/?foo=bar",
        )
        self.assertNotIn("mobile=", normalized)

    def test_discuz_url_is_accepted_without_mobile_flag(self):
        normalized = keylol_browser.normalize_keylol_browser_url(
            "https://keylol.com/forum.php?mod=viewthread&tid=1047774&page=1&mobile=yes"
        )
        self.assertEqual(
            normalized,
            "https://keylol.com/forum.php?mod=viewthread&tid=1047774&page=1",
        )

    def test_url_rejects_non_keylol_hosts_credentials_ports_fragments_and_page_two(self):
        rejected = (
            "https://example.com/t1047774-1-1",
            "https://user:pass@keylol.com/t1047774-1-1",
            "https://keylol.com:443/t1047774-1-1",
            "https://keylol.com/t1047774-1-1#post",
            "https://keylol.com/t1047774-2-1",
            "https://keylol.com/forum.php?mod=viewthread&tid=1047774&page=2",
            "https://keylol.com/forum.php?mod=forum&tid=1047774",
            "https://keylol.com/not-a-thread",
        )
        for value in rejected:
            with self.subTest(value=value):
                with self.assertRaises(keylol_browser.KeylolBrowserUrlError):
                    keylol_browser.normalize_keylol_browser_url(value)

    def test_url_rejects_empty_and_malformed_port(self):
        for value in ("", "   ", "https://keylol.com:bad/t1047774-1-1"):
            with self.subTest(value=value):
                with self.assertRaises(keylol_browser.KeylolBrowserUrlError):
                    keylol_browser.normalize_keylol_browser_url(value)


class KeylolBrowserCookieTests(unittest.TestCase):
    def test_cookie_header_parses_pairs_and_ignores_attributes(self):
        self.assertEqual(
            keylol_browser.parse_keylol_cookie_header(
                "Cookie: uid=123; sid=abc==; Path=/; Secure; HttpOnly; SameSite=Lax"
            ),
            [("uid", "123"), ("sid", "abc==")],
        )

    def test_cookie_parser_is_empty_for_blank_input(self):
        self.assertEqual(keylol_browser.parse_keylol_cookie_header("  "), [])
        self.assertEqual(keylol_browser.parse_keylol_cookie_header(None), [])

    def test_cookie_parser_rejects_newlines_invalid_pairs_and_control_values(self):
        rejected = (
            "sid=abc\nInjected: yes",
            "sid=abc\rInjected: yes",
            "sid",
            "bad name=value",
            "bad;name=value",
            "sid=abc\x00def",
        )
        for value in rejected:
            with self.subTest(value=repr(value)):
                with self.assertRaises(keylol_browser.KeylolBrowserCaptureError):
                    keylol_browser.parse_keylol_cookie_header(value)

    def test_cookie_attributes_and_dollar_names_are_not_installed(self):
        parsed = keylol_browser.parse_keylol_cookie_header(
            "$Version=1; $Path=/; path=/; expires=tomorrow; sid=abc"
        )
        self.assertEqual(parsed, [("sid", "abc")])


class KeylolBrowserBoundsAndProxyTests(unittest.TestCase):
    def test_timeout_and_width_are_clamped_to_safe_bounds(self):
        self.assertEqual(keylol_browser._bounded_timeout(None), 45_000)
        self.assertEqual(keylol_browser._bounded_timeout("not-a-number"), 45_000)
        self.assertEqual(keylol_browser._bounded_timeout(1), 5_000)
        self.assertEqual(keylol_browser._bounded_timeout(999_999), 120_000)
        self.assertEqual(keylol_browser._bounded_timeout(12_345), 12_345)

        self.assertEqual(keylol_browser._bounded_width(None), 390)
        self.assertEqual(keylol_browser._bounded_width("not-a-number"), 390)
        self.assertEqual(keylol_browser._bounded_width(1), 320)
        self.assertEqual(keylol_browser._bounded_width(999_999), 440)
        self.assertEqual(keylol_browser._bounded_width(375), 375)

    def test_proxy_parsing_preserves_credentials_separately(self):
        self.assertEqual(
            keylol_browser._playwright_proxy("http://alice:secret@proxy.example:8080"),
            {
                "server": "http://proxy.example:8080",
                "username": "alice",
                "password": "secret",
            },
        )
        self.assertEqual(
            keylol_browser._playwright_proxy("socks5://proxy.example:1080"),
            {"server": "socks5://proxy.example:1080"},
        )
        self.assertIsNone(keylol_browser._playwright_proxy("  "))

    def test_proxy_parser_rejects_unsafe_or_ambiguous_forms(self):
        rejected = (
            "ftp://proxy.example:8080",
            "proxy.example:8080",
            "http://proxy.example/path",
            "http://proxy.example:8080/?x=1",
            "http://proxy.example:8080#fragment",
            "http://proxy.example:bad",
            "http://user@",
            "http://proxy.example:8080\nX-Injected: yes",
        )
        for value in rejected:
            with self.subTest(value=repr(value)):
                with self.assertRaises(keylol_browser.KeylolBrowserCaptureError):
                    keylol_browser._playwright_proxy(value)


class KeylolBrowserRoutingAndTransformContractTests(unittest.TestCase):
    def test_document_and_image_routing_accept_only_expected_keylol_endpoints(self):
        self.assertTrue(
            keylol_browser._is_allowed_thread_document(
                "https://keylol.com/t1047774-1-1", "1047774"
            )
        )
        self.assertFalse(
            keylol_browser._is_allowed_thread_document(
                "https://keylol.com/t1047774-1-1", "1047775"
            )
        )
        self.assertTrue(
            keylol_browser._is_allowed_image_request(
                "https://blob.keylol.com/forum/202608/30/abc123.jpg"
            )
        )
        self.assertTrue(
            keylol_browser._is_allowed_image_request(
                "https://keylol.com/forum.php?mod=attachment&aid=123"
            )
        )
        for value in (
            "https://evil.example/forum/abc.jpg",
            "http://blob.keylol.com/forum/abc.jpg",
            "https://blob.keylol.com/forum/abc.exe",
            "https://blob.keylol.com/other/abc.jpg",
        ):
            with self.subTest(value=value):
                self.assertFalse(keylol_browser._is_allowed_image_request(value))

    def test_transform_script_declares_lazy_sources_and_mobile_capture_contract(self):
        script = keylol_browser._TRANSFORM_SCRIPT
        required_lazy_attributes = (
            "zoomfile",
            "file",
            "data-original",
            "data-src",
            "data-lazy-src",
            "data-actualsrc",
            "data-lazyload",
            "data-zoomfile",
            "data-file",
            "data-url",
            "data-image-url",
            "data-ori-src",
            "origin-src",
            "picurl",
            "src",
        )
        for attribute in required_lazy_attributes:
            with self.subTest(attribute=attribute):
                self.assertIn('"' + attribute + '"', script)
        for marker in (
            "const placeholder = \"data:image/",
            "keylolPending",
            'image.src = placeholder',
            'image.decoding = "async"',
            "keylol-capture-footer",
            "sourceUrl",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, script)
        # ``dataset.keylolCandidates`` is serialized by the browser as the
        # required ``data-keylol-candidates`` attribute.
        self.assertTrue(
            "data-keylol-candidates" in script or "keylolCandidates" in script
        )

    def test_scroll_script_is_bounded_and_scripts_never_interpolate_cookie(self):
        scroll_script = keylol_browser._SCROLL_SCRIPT
        self.assertIn("maxImages", scroll_script)
        self.assertIn("maxHeight", scroll_script)
        self.assertIn("perImageTimeoutMs", scroll_script)
        self.assertIn("for (const image of images)", scroll_script)
        self.assertIn("image.scrollIntoView", scroll_script)
        self.assertIn("attempt < 2", scroll_script)
        self.assertIn("await loadOnce(image, source)", scroll_script)
        self.assertIn("tooMany", scroll_script)
        self.assertIn("tooTall", scroll_script)
        self.assertIn("if (document.documentElement.scrollHeight > maxHeight)", scroll_script)
        self.assertIn("await wait(80)", scroll_script)
        self.assertLessEqual(keylol_browser.MAX_BROWSER_IMAGE_COUNT, 500)
        self.assertLessEqual(keylol_browser.MAX_BROWSER_PAGE_HEIGHT, 100_000)

    def test_finalize_script_marks_loaded_and_failed_images_with_cards(self):
        finalize_script = keylol_browser._FINALIZE_IMAGES_SCRIPT
        self.assertIn("keylolLoaded", finalize_script)
        self.assertIn("keylol-browser-image-failed", finalize_script)
        self.assertIn("图片加载失败", finalize_script)
        # Failed state is recorded by the bounded loader and consumed by the
        # finalizer as one cross-script contract.
        self.assertIn("keylolFailed", keylol_browser._SCROLL_SCRIPT)

    def test_capture_scripts_never_interpolate_cookie(self):
        for name in ("_TRANSFORM_SCRIPT", "_SCROLL_SCRIPT", "_FINALIZE_IMAGES_SCRIPT"):
            script = getattr(keylol_browser, name).lower()
            with self.subTest(name=name):
                self.assertNotIn("document.cookie", script)
                self.assertNotIn("${cookie}", script)


if __name__ == "__main__":
    unittest.main()
