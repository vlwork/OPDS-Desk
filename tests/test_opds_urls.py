import ast
import dataclasses
import ipaddress
import sys
import types
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit


ATOM = "http://www.w3.org/2005/Atom"
NS = {"atom": ATOM}
PROJECT_ROOT = Path(__file__).resolve().parents[1]
RELATIVE_LINKS_FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "opds" / "relative_links.xml"


def load_url_functions():
    """Загружает только URL helpers, не импортируя runtime приложения."""
    tree = ast.parse((PROJECT_ROOT / "app.py").read_text(encoding="utf-8"))
    wanted = {"normalize_opds_url", "resolve_opds_url", "same_origin", "is_safe_http_url"}
    body = [node for node in tree.body if getattr(node, "name", None) in wanted]
    module = types.ModuleType("isolated_opds_url_test")
    sys.modules[module.__name__] = module
    module.__dict__.update(
        dataclass=dataclasses.dataclass,
        ipaddress=ipaddress,
        urljoin=urljoin,
        urlsplit=urlsplit,
        urlunsplit=urlunsplit,
    )
    exec(compile(ast.Module(body=body, type_ignores=[]), "app.py", "exec"), module.__dict__)
    return module


URLS = load_url_functions()


class OPDSURLTests(unittest.TestCase):
    def test_a_normalize_http_urls(self):
        self.assertEqual(
            URLS.normalize_opds_url("  HTTPS://Example.ORG/opds#section  "),
            "https://example.org/opds",
        )
        self.assertEqual(
            URLS.normalize_opds_url("http://Example.ORG/catalog/feed.xml"),
            "http://example.org/catalog/feed.xml",
        )
        self.assertEqual(
            URLS.normalize_opds_url("https://example.org/opds?lang=ru#results"),
            "https://example.org/opds?lang=ru",
        )

    def test_b_reject_invalid_or_credentialed_urls(self):
        invalid = (
            "",
            "   ",
            "/catalog/feed.xml",
            "ftp://example.org/catalog",
            "file:///catalog/feed.xml",
            "javascript:alert(1)",
            "https://user:pass@example.org/catalog",
            "https:///catalog/feed.xml",
        )
        for value in invalid:
            with self.subTest(url=value):
                with self.assertRaises(ValueError):
                    URLS.normalize_opds_url(value)

    def test_c_resolve_against_current_page_url(self):
        base = "https://example.org/catalog/pages/page1.xml"
        expected = {
            "book.epub": "https://example.org/catalog/pages/book.epub",
            "./book.fb2": "https://example.org/catalog/pages/book.fb2",
            "../covers/cover.jpg": "https://example.org/catalog/covers/cover.jpg",
            "?page=2": "https://example.org/catalog/pages/page1.xml?page=2",
            "/catalog/root": "https://example.org/catalog/root",
            "https://cdn.example.org/book.epub": "https://cdn.example.org/book.epub",
        }
        for href, resolved in expected.items():
            with self.subTest(href=href):
                self.assertEqual(URLS.resolve_opds_url(base, href), resolved)

    def test_d_same_origin_uses_effective_ports(self):
        self.assertTrue(URLS.same_origin("https://example.org/a", "https://example.org/b"))
        self.assertTrue(URLS.same_origin("https://example.org/a", "https://example.org:443/b"))
        self.assertFalse(URLS.same_origin("http://example.org/a", "https://example.org/a"))
        self.assertFalse(URLS.same_origin("https://example.org", "https://other.org"))
        self.assertFalse(URLS.same_origin("https://example.org:8443", "https://example.org"))

    def test_e_safe_http_url_never_raises_for_invalid_values(self):
        for value in (None, 123, "", "/relative", "file:///tmp/catalog.xml"):
            with self.subTest(url=value):
                self.assertFalse(URLS.is_safe_http_url(value))
        self.assertTrue(URLS.is_safe_http_url("http://localhost:8080/catalog"))
        self.assertTrue(URLS.is_safe_http_url("https://192.168.1.20/opds"))

    def test_f_fixture_links_resolve_from_current_feed(self):
        root = ET.parse(RELATIVE_LINKS_FIXTURE).getroot()
        base = "https://example.org/catalog/pages/relative_links.xml"
        hrefs = {link.get("href") for link in root.iterfind(".//atom:link", NS)}
        expected = {
            "book.epub": "https://example.org/catalog/pages/book.epub",
            "./book.fb2": "https://example.org/catalog/pages/book.fb2",
            "../covers/cover.jpg": "https://example.org/catalog/covers/cover.jpg",
            "?page=2": "https://example.org/catalog/pages/relative_links.xml?page=2",
            "/catalog/root": "https://example.org/catalog/root",
        }
        for href in hrefs:
            with self.subTest(href=href):
                resolved = URLS.resolve_opds_url(base, href)
                self.assertTrue(URLS.is_safe_http_url(resolved))
        for href, resolved in expected.items():
            self.assertIn(href, hrefs)
            self.assertEqual(URLS.resolve_opds_url(base, href), resolved)

    def test_g_unicode_hostname_is_normalized_to_idna(self):
        self.assertEqual(
            URLS.normalize_opds_url("https://пример.рф/opds"),
            "https://xn--e1afmkfd.xn--p1ai/opds",
        )

    def test_h_ipv6_hostname_and_port_are_normalized(self):
        self.assertEqual(
            URLS.normalize_opds_url("https://[2001:db8::1]/opds"),
            "https://[2001:db8::1]/opds",
        )
        self.assertEqual(
            URLS.normalize_opds_url("https://[2001:db8::1]:8443/opds"),
            "https://[2001:db8::1]:8443/opds",
        )

    def test_i_same_origin_supports_ipv6_default_port(self):
        self.assertTrue(
            URLS.same_origin(
                "https://[2001:db8::1]/a",
                "https://[2001:db8::1]:443/b",
            )
        )

    def test_j_invalid_ports_ipv6_and_unicode_credentials_are_rejected(self):
        invalid = (
            "https://example.org:99999/opds",
            "https://[2001:db8::1/opds",
            "https://user:pass@пример.рф/opds",
        )
        for value in invalid:
            with self.subTest(url=value):
                with self.assertRaises(ValueError):
                    URLS.normalize_opds_url(value)


if __name__ == "__main__":
    unittest.main()
