import ast
import copy
import dataclasses
import hashlib
import ipaddress
import json
import sys
import threading
import time
import types
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit, urlunsplit


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = PROJECT_ROOT / "tests" / "fixtures" / "opds"
OPENSEARCH_DESCRIPTION = (FIXTURE_DIR / "opensearch_description.xml").read_bytes()
DIRECT_ATOM_ROOT = (FIXTURE_DIR / "direct_atom_search.xml").read_bytes()
NO_SEARCH_ROOT = (FIXTURE_DIR / "no_search.xml").read_bytes()
PAGE_ONE = (FIXTURE_DIR / "search_results_page_1.xml").read_bytes()
PAGE_TWO = (FIXTURE_DIR / "search_results_page_2.xml").read_bytes()

OPENSEARCH_ROOT = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <id>urn:test:search-root</id>
  <title>Search root</title>
  <updated>2026-08-16T09:00:00Z</updated>
  <link rel="search" type="application/opensearchdescription+xml"
        href="/metadata/opensearch.xml" />
</feed>
"""


def load_current_search_module():
    """Загружает current-source orchestration и существующий neutral pipeline."""
    source = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    wanted = {
        "SourceConfig",
        "normalize_app_config",
        "source_config_from_app_config",
        "normalize_opds_url",
        "resolve_opds_url",
        "is_safe_http_url",
        "AcquisitionLink",
        "BookRecord",
        "CatalogRef",
        "OPDSSearchRef",
        "OPDSSearchDescriptor",
        "OPDSFeed",
        "OPDSCatalogPage",
        "OPDS1Provider",
        "_atom_search_mime_priority",
        "resolve_opensearch_template",
        "parse_opensearch_description",
        "resolve_direct_atom_search",
        "normalize_opds_search_query",
        "expand_opds_search_template",
        "_catalog_acquisition_format",
        "book_record_to_catalog_book",
        "resolve_opds_search_descriptor",
        "load_opds_catalog_page",
        "load_opds_search_page",
        "_validate_opds_search_page_number",
        "opds_search_cache_identity",
        "opds_search_page_cache_key",
        "_cached_opds_search_page",
        "_store_opds_search_page",
        "load_cached_opds_search_page",
        "current_source_config",
        "has_configured_opds_source",
        "load_current_opds_feed",
        "resolve_current_opds_search_descriptor",
        "load_current_opds_search_page",
    }
    constants = {
        "CONFIG_VERSION",
        "OPDS1_ATOM",
        "OPDS1_DC",
        "OPDS1_ACQUISITION_PREFIX",
        "OPDS1_IMAGE_RELS",
        "OPDS1_THUMBNAIL_RELS",
        "OPDS1_NS",
        "OPENSEARCH_1_1",
        "OPENSEARCH_TERMS_PLACEHOLDER",
        "OPENSEARCH_START_PAGE_PLACEHOLDER",
        "OPENSEARCH_OPTIONAL_START_PAGE_PLACEHOLDER",
    }
    body = []
    for node in tree.body:
        if getattr(node, "name", None) in wanted:
            body.append(node)
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in constants
            for target in node.targets
        ):
            body.append(node)

    module = types.ModuleType("isolated_current_opds_search_test")
    sys.modules[module.__name__] = module
    module.__dict__.update(
        APP_CONFIG={},
        DEFAULT_DESTINATION="test-default-library",
        MAX_CATALOG_PAGES=5,
        SEARCH_CACHE_TTL=30 * 60,
        copy=copy,
        dataclass=dataclasses.dataclass,
        hashlib=hashlib,
        ipaddress=ipaddress,
        json=json,
        ET=ET,
        quote=quote,
        threading=threading,
        time=time,
        urljoin=urljoin,
        urlsplit=urlsplit,
        urlunsplit=urlunsplit,
        opds_search_page_cache={},
        opds_search_page_cache_lock=threading.Lock(),
    )
    exec(compile(ast.Module(body=body, type_ignores=[]), "app.py", "exec"), module.__dict__)
    module.__source_text__ = source
    module.__source_tree__ = tree
    return module


SEARCH_MODULE = load_current_search_module()


class RoutingFakeClient:
    def __init__(self, routes):
        self.routes = dict(routes)
        self.calls = []

    def fetch(self, url):
        self.calls.append(url)
        content, final_url = self.routes[url]
        return types.SimpleNamespace(
            requested_url=url,
            final_url=final_url,
            content=content,
            content_type="application/xml",
        )

    def fetch_feed(self, url, source_id=""):
        result = self.fetch(url)
        return SEARCH_MODULE.OPDS1Provider().parse_feed(
            result.content,
            result.final_url,
            source_id=source_id,
        )


def open_search_routes(root_url="https://source.test/opds/root.xml"):
    descriptor_url = "https://source.test/metadata/opensearch.xml"
    result_url = "https://source.test/feeds/search.atom?q=Dune"
    next_url = "https://source.test/feeds/search.atom?cursor=next"
    return {
        root_url: (OPENSEARCH_ROOT, root_url),
        descriptor_url: (OPENSEARCH_DESCRIPTION, descriptor_url),
        result_url: (PAGE_ONE, result_url),
        next_url: (PAGE_TWO, next_url),
    }


def direct_atom_routes(root_url="https://source.test/opds/root.xml"):
    result_url = "https://source.test/opds/find?q=Dune"
    return {
        root_url: (DIRECT_ATOM_ROOT, root_url),
        result_url: (PAGE_ONE, result_url),
    }


class CurrentOPDSSearchTests(unittest.TestCase):
    def setUp(self):
        SEARCH_MODULE.APP_CONFIG = {
            "config_version": SEARCH_MODULE.CONFIG_VERSION,
            "opds_url": "",
            "source_id": "",
            "source_name": "",
            "library_path": "X:/Books",
            "setup_complete": True,
        }
        with SEARCH_MODULE.opds_search_page_cache_lock:
            SEARCH_MODULE.opds_search_page_cache.clear()

    def configure(self, root_url="https://source.test/opds/root.xml", source_id="source-a"):
        SEARCH_MODULE.APP_CONFIG.update(
            opds_url=root_url,
            source_id=source_id,
            source_name="Test catalog",
        )

    def test_a_unconfigured_source_is_controlled_and_does_no_http(self):
        client = RoutingFakeClient({})
        for helper, args in (
            (SEARCH_MODULE.resolve_current_opds_search_descriptor, ()),
            (SEARCH_MODULE.load_current_opds_search_page, ("Dune",)),
        ):
            with self.subTest(helper=helper.__name__), self.assertRaisesRegex(
                ValueError,
                "OPDS-источник не настроен",
            ):
                helper(*args, client=client)
        self.assertEqual(client.calls, [])

    def test_b_current_source_root_url_is_loaded(self):
        root_url = "https://source.test/opds/root.xml"
        self.configure(root_url)
        client = RoutingFakeClient(open_search_routes(root_url))
        SEARCH_MODULE.resolve_current_opds_search_descriptor(client=client)
        self.assertEqual(client.calls[0], root_url)

    def test_c_root_feed_search_ref_drives_resolution(self):
        self.configure()
        client = RoutingFakeClient(open_search_routes())
        descriptor = SEARCH_MODULE.resolve_current_opds_search_descriptor(client=client)
        self.assertEqual(
            descriptor.template,
            "https://source.test/feeds/search.atom?q={searchTerms}",
        )

    def test_d_opensearch_descriptor_is_loaded_after_root(self):
        self.configure()
        client = RoutingFakeClient(open_search_routes())
        descriptor = SEARCH_MODULE.resolve_current_opds_search_descriptor(client=client)
        self.assertIsInstance(descriptor, SEARCH_MODULE.OPDSSearchDescriptor)
        self.assertEqual(
            client.calls,
            [
                "https://source.test/opds/root.xml",
                "https://source.test/metadata/opensearch.xml",
            ],
        )

    def test_e_results_are_loaded_after_opensearch_descriptor(self):
        self.configure()
        client = RoutingFakeClient(open_search_routes())
        SEARCH_MODULE.load_current_opds_search_page("Dune", client=client)
        self.assertEqual(
            client.calls,
            [
                "https://source.test/opds/root.xml",
                "https://source.test/metadata/opensearch.xml",
                "https://source.test/feeds/search.atom?q=Dune",
            ],
        )

    def test_f_one_default_client_crosses_root_descriptor_and_results(self):
        self.configure()
        created = []

        def client_factory():
            client = RoutingFakeClient(open_search_routes())
            created.append(client)
            return client

        original = SEARCH_MODULE.__dict__.get("OPDSHTTPClient")
        SEARCH_MODULE.OPDSHTTPClient = client_factory
        try:
            SEARCH_MODULE.load_current_opds_search_page("Dune")
        finally:
            if original is None:
                SEARCH_MODULE.__dict__.pop("OPDSHTTPClient", None)
            else:
                SEARCH_MODULE.OPDSHTTPClient = original
        self.assertEqual(len(created), 1)
        self.assertEqual(len(created[0].calls), 3)

    def test_g_source_id_reaches_page_and_books(self):
        self.configure(source_id="sha256:opaque-source")
        result = SEARCH_MODULE.load_current_opds_search_page(
            "Dune",
            client=RoutingFakeClient(open_search_routes()),
        )
        self.assertEqual(result.source_id, "sha256:opaque-source")
        self.assertTrue(
            all(book["source_id"] == "sha256:opaque-source" for book in result.books)
        )

    def test_h_page_zero_returns_existing_catalog_page(self):
        self.configure()
        result = SEARCH_MODULE.load_current_opds_search_page(
            "Dune",
            page=0,
            client=RoutingFakeClient(open_search_routes()),
        )
        self.assertIsInstance(result, SEARCH_MODULE.OPDSCatalogPage)
        self.assertEqual(result.title, "Example search results")

    def test_i_page_is_delegated_to_cached_pagination_backend(self):
        self.configure()
        client = RoutingFakeClient(open_search_routes())
        result = SEARCH_MODULE.load_current_opds_search_page(
            "Dune",
            page=1,
            client=client,
        )
        self.assertEqual(result.title, "Example search results — second page")
        self.assertEqual(
            client.calls[-2:],
            [
                "https://source.test/feeds/search.atom?q=Dune",
                "https://source.test/feeds/search.atom?cursor=next",
            ],
        )

    def test_j_force_is_delegated_to_existing_cache_loader(self):
        self.configure()
        calls = []
        original = SEARCH_MODULE.load_cached_opds_search_page

        def fake_loader(descriptor, query, **kwargs):
            calls.append(kwargs)
            return object()

        SEARCH_MODULE.load_cached_opds_search_page = fake_loader
        try:
            SEARCH_MODULE.load_current_opds_search_page(
                "Dune",
                force=True,
                client=RoutingFakeClient(open_search_routes()),
            )
        finally:
            SEARCH_MODULE.load_cached_opds_search_page = original
        self.assertIs(calls[0]["force"], True)

    def test_k_source_without_search_has_clear_error(self):
        root_url = "https://source.test/opds/root.xml"
        self.configure(root_url)
        client = RoutingFakeClient({root_url: (NO_SEARCH_ROOT, root_url)})
        with self.assertRaisesRegex(
            ValueError,
            "Этот OPDS-источник не предоставляет поиск",
        ):
            SEARCH_MODULE.resolve_current_opds_search_descriptor(client=client)

    def test_l_source_without_search_stops_after_root_request(self):
        root_url = "https://source.test/opds/root.xml"
        self.configure(root_url)
        client = RoutingFakeClient({root_url: (NO_SEARCH_ROOT, root_url)})
        with self.assertRaises(ValueError):
            SEARCH_MODULE.load_current_opds_search_page("Dune", client=client)
        self.assertEqual(client.calls, [root_url])

    def test_m_direct_atom_skips_descriptor_request(self):
        self.configure()
        client = RoutingFakeClient(direct_atom_routes())
        SEARCH_MODULE.load_current_opds_search_page("Dune", client=client)
        self.assertEqual(
            client.calls,
            [
                "https://source.test/opds/root.xml",
                "https://source.test/opds/find?q=Dune",
            ],
        )

    def test_n_direct_atom_query_uses_source_template(self):
        self.configure()
        routes = direct_atom_routes()
        expected_url = "https://source.test/opds/find?q=Dune%20Messiah"
        routes[expected_url] = (PAGE_ONE, expected_url)
        client = RoutingFakeClient(routes)
        SEARCH_MODULE.load_current_opds_search_page("Dune Messiah", client=client)
        self.assertEqual(client.calls[-1], expected_url)

    def test_o_source_switching_does_not_share_search_page_cache(self):
        root_a = "https://source.test/opds/a.xml"
        root_b = "https://source.test/opds/b.xml"
        routes = open_search_routes(root_a)
        routes[root_b] = (OPENSEARCH_ROOT, root_b)
        client = RoutingFakeClient(routes)
        result_url = "https://source.test/feeds/search.atom?q=Dune"

        self.configure(root_a, "source-a")
        first = SEARCH_MODULE.load_current_opds_search_page("Dune", client=client)
        self.configure(root_b, "source-b")
        second = SEARCH_MODULE.load_current_opds_search_page("Dune", client=client)

        self.assertEqual(client.calls.count(result_url), 2)
        self.assertEqual((first.source_id, second.source_id), ("source-a", "source-b"))

    def test_p_opaque_book_ids_are_preserved(self):
        self.configure()
        result = SEARCH_MODULE.load_current_opds_search_page(
            "Dune",
            client=RoutingFakeClient(open_search_routes()),
        )
        self.assertIn(
            "tag:catalog.example.test,2026:amber-observatory",
            {book["id"] for book in result.books},
        )

    def test_q_orchestration_does_not_call_requests_directly(self):
        source = self.orchestration_source()
        self.assertNotIn("requests.get", source)
        self.assertNotIn("requests.", source)

    def test_r_orchestration_contains_no_legacy_markers(self):
        source = self.orchestration_source().lower()
        forbidden = (
            "opds" + "_base",
            "flibu" + "sta",
            "/opds/" + "search",
            "search" + "type",
            "search" + "term",
            "page" + "number",
        )
        for marker in forbidden:
            with self.subTest(marker=marker):
                self.assertNotIn(marker, source)

    def orchestration_source(self):
        wanted = {
            "resolve_current_opds_search_descriptor",
            "load_current_opds_search_page",
        }
        return "\n".join(
            ast.get_source_segment(SEARCH_MODULE.__source_text__, node) or ""
            for node in SEARCH_MODULE.__source_tree__.body
            if getattr(node, "name", None) in wanted
        )


if __name__ == "__main__":
    unittest.main()
