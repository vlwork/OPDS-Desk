import ast
import dataclasses
import hashlib
import ipaddress
import re
import sys
import types
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = PROJECT_ROOT / "tests" / "fixtures" / "opds"


def load_discovery_module():
    """Загружает только нейтральный URL-слой и OPDS parser."""
    source = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    wanted = {
        "normalize_opds_url",
        "resolve_opds_url",
        "same_origin",
        "is_safe_http_url",
        "AcquisitionLink",
        "BookRecord",
        "CatalogRef",
        "OPDSSearchRef",
        "OPDSFeed",
        "OPDS1Provider",
    }
    body = []
    for node in tree.body:
        if getattr(node, "name", None) in wanted:
            body.append(node)
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name)
            and (
                target.id.startswith("OPDS1_")
                or target.id == "OPENSEARCH_1_1"
            )
            for target in node.targets
        ):
            body.append(node)
    module = types.ModuleType("isolated_opds_search_discovery_test")
    sys.modules[module.__name__] = module
    module.__dict__.update(
        dataclass=dataclasses.dataclass,
        hashlib=hashlib,
        ipaddress=ipaddress,
        ET=ET,
        urljoin=urljoin,
        urlsplit=urlsplit,
        urlunsplit=urlunsplit,
    )
    exec(compile(ast.Module(body=body, type_ignores=[]), "app.py", "exec"), module.__dict__)
    module.__source_text__ = source
    module.__source_tree__ = tree
    return module


DISCOVERY_MODULE = load_discovery_module()
PROVIDER = DISCOVERY_MODULE.OPDS1Provider()


def parse_fixture(name, page_url):
    xml_text = (FIXTURE_DIR / name).read_text(encoding="utf-8")
    return PROVIDER.parse_feed(xml_text, page_url, source_id="source:opaque")


class OPDSSearchDiscoveryTests(unittest.TestCase):
    def test_a_opensearch_reference_is_discovered_and_resolved(self):
        feed = parse_fixture(
            "search_navigation.xml",
            "https://reader.example.test/catalog/root.xml",
        )
        self.assertIsInstance(feed.search, DISCOVERY_MODULE.OPDSSearchRef)
        self.assertEqual(
            feed.search.url,
            "https://reader.example.test/metadata/opensearch.xml",
        )
        self.assertEqual(
            feed.search.mime_type,
            "application/opensearchdescription+xml",
        )

    def test_b_absolute_search_href_is_preserved(self):
        xml_text = """<?xml version="1.0" encoding="UTF-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <id>urn:search:absolute</id><title>Absolute</title>
          <updated>2026-08-15T00:00:00Z</updated>
          <link rel="search" type="application/opensearchdescription+xml"
                href="https://search.example.test/description.xml" />
        </feed>"""
        feed = PROVIDER.parse_feed(xml_text, "https://reader.example.test/root.xml")
        self.assertEqual(
            feed.search.url,
            "https://search.example.test/description.xml",
        )

    def test_c_search_is_not_navigation_and_catalog_content_is_unchanged(self):
        feed = parse_fixture(
            "search_navigation.xml",
            "https://reader.example.test/catalog/root.xml",
        )
        self.assertEqual(len(feed.publications), 1)
        self.assertEqual(
            feed.publications[0].source_item_id,
            "urn:uuid:f9aae724-3019-43f2-b5f4-bc7ecc571cea",
        )
        self.assertEqual(len(feed.navigation), 1)
        self.assertNotIn(feed.search.url, {ref.url for ref in feed.navigation})
        self.assertEqual(
            feed.next_url,
            "https://reader.example.test/catalog/root.xml?page=2",
        )

    def test_d_feed_without_search_keeps_none_default(self):
        feed = parse_fixture(
            "no_search.xml",
            "https://reader.example.test/catalog/no_search.xml",
        )
        self.assertIsNone(feed.search)
        self.assertGreaterEqual(len(feed.navigation), 1)

    def test_e_malformed_optional_search_href_does_not_break_feed(self):
        xml_text = """<?xml version="1.0" encoding="UTF-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <id>urn:search:malformed</id><title>Malformed optional link</title>
          <updated>2026-08-15T00:00:00Z</updated>
          <link rel="search" type="application/opensearchdescription+xml"
                href="file:///description.xml" />
          <entry><id>tag:example.test,2026:book</id><title>Still parsed</title>
            <updated>2026-08-15T00:00:00Z</updated>
            <link rel="http://opds-spec.org/acquisition/open-access"
                  type="application/epub+zip" href="book.epub" />
          </entry>
        </feed>"""
        feed = PROVIDER.parse_feed(xml_text, "https://reader.example.test/root.xml")
        self.assertIsNone(feed.search)
        self.assertEqual(len(feed.publications), 1)

    def test_f_unknown_mime_is_skipped_and_opensearch_has_priority(self):
        feed = parse_fixture(
            "search_navigation.xml",
            "https://reader.example.test/catalog/root.xml",
        )
        self.assertEqual(
            feed.search.mime_type,
            "application/opensearchdescription+xml",
        )

    def test_g_direct_atom_search_is_discovered_without_expansion(self):
        feed = parse_fixture(
            "direct_atom_search.xml",
            "https://reader.example.test/catalog/direct_atom_search.xml",
        )
        self.assertEqual(
            feed.search.url,
            "https://reader.example.test/catalog/find?q={searchTerms}",
        )
        self.assertEqual(
            feed.search.mime_type,
            "application/atom+xml;profile=opds-catalog",
        )

    def test_h_discovery_layer_has_no_legacy_source_coupling(self):
        names = {"OPDSSearchRef", "OPDSFeed", "OPDS1Provider"}
        parser_source = "\n".join(
            ast.get_source_segment(DISCOVERY_MODULE.__source_text__, node) or ""
            for node in DISCOVERY_MODULE.__source_tree__.body
            if getattr(node, "name", None) in names
        )
        fixture_source = "\n".join(
            (FIXTURE_DIR / name).read_text(encoding="utf-8")
            for name in ("search_navigation.xml", "direct_atom_search.xml")
        )
        test_source = Path(__file__).read_text(encoding="utf-8")
        corpus = "\n".join((parser_source, fixture_source, test_source)).lower()
        corpus = corpus.replace("{searchterms}", "")
        forbidden_exact = (
            "opds" + "_base",
            "flibu" + "sta",
            "/opds/" + "search",
            "search" + "type",
            "search" + "term",
            "page" + "number",
        )
        forbidden_words = (
            "que" + "ue",
            "down" + "load",
            "pro" + "xy",
            "so" + "cks",
            "x" + "ray",
            "t" + "or",
        )
        for marker in forbidden_exact:
            with self.subTest(marker=marker):
                self.assertNotIn(marker, corpus)
        for marker in forbidden_words:
            with self.subTest(marker=marker):
                self.assertIsNone(re.search(rf"\b{re.escape(marker)}\b", corpus))


if __name__ == "__main__":
    unittest.main()
