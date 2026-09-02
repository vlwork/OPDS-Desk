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
from urllib.parse import urljoin, urlsplit, urlunsplit


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_page_loader_module():
    """Загружает neutral page loader без runtime приложения."""
    source = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    wanted_definitions = {
        "SourceConfig",
        "normalize_app_config",
        "source_config_from_app_config",
        "normalize_opds_url",
        "resolve_opds_url",
        "source_namespace",
        "_opaque_key_part",
        "catalog_cache_key",
        "AcquisitionLink",
        "BookRecord",
        "CatalogRef",
        "RegisteredCatalogRef",
        "make_catalog_ref_token",
        "register_catalog_ref",
        "get_catalog_ref",
        "get_current_catalog_ref",
        "clear_catalog_ref_registry",
        "register_catalog_refs",
        "register_catalog_navigation",
        "OPDSFeed",
        "OPDSCatalogPage",
        "OPDS1Provider",
        "_catalog_acquisition_format",
        "book_record_to_catalog_book",
        "HTTPFetchResult",
        "load_opds_catalog_page",
        "current_source_config",
        "prepare_catalog_page_book",
        "registered_catalog_page_cache_key",
        "cached_registered_catalog_page",
        "_empty_registered_catalog_page",
        "load_registered_catalog_page",
    }
    wanted_assignments = {
        "CONFIG_VERSION",
        "MAX_CATALOG_REF_REGISTRY",
        "catalog_ref_registry",
        "catalog_ref_registry_lock",
        "MAX_CATALOG_PAGES",
        "CATALOG_CACHE_TTL",
        "catalog_page_cache",
        "catalog_page_cache_lock",
    }
    body = []
    for node in tree.body:
        if getattr(node, "name", None) in wanted_definitions:
            body.append(node)
            continue
        if not isinstance(node, ast.Assign):
            continue
        assigned_names = {
            target.id for target in node.targets if isinstance(target, ast.Name)
        }
        if assigned_names & wanted_assignments or any(
            name.startswith("OPDS1_") for name in assigned_names
        ) or "OPENSEARCH_1_1" in assigned_names:
            body.append(node)

    module = types.ModuleType("isolated_registered_catalog_page_loader_test")
    sys.modules[module.__name__] = module
    module.__dict__.update(
        dataclass=dataclasses.dataclass,
        copy=copy,
        hashlib=hashlib,
        ipaddress=ipaddress,
        json=json,
        threading=threading,
        time=time,
        ET=ET,
        urljoin=urljoin,
        urlsplit=urlsplit,
        urlunsplit=urlunsplit,
        DEFAULT_DESTINATION="test-default-library",
        APP_CONFIG={},
        apply_local_status=lambda book: book,
    )
    exec(
        compile(ast.Module(body=body, type_ignores=[]), "app.py", "exec"),
        module.__dict__,
    )
    module.__source_text__ = source
    module.__source_tree__ = tree
    return module


PAGE_MODULE = load_page_loader_module()


def feed_xml(title, item_id, next_href="", acquisition_href="book.epub"):
    next_link = f'<link rel="next" href="{next_href}" />' if next_href else ""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:dc="http://purl.org/dc/terms/">
  <id>tag:example.org,2026:{title}</id>
  <title>{title}</title>
  {next_link}
  <entry>
    <id>{item_id}</id>
    <title>{title} Book</title>
    <author><name>Example Author</name></author>
    <dc:language>en</dc:language>
    <link rel="http://opds-spec.org/acquisition/open-access"
          type="application/epub+zip"
          href="{acquisition_href}" />
  </entry>
</feed>""".encode("utf-8")


class FakeClient:
    def __init__(self, results, on_fetch=None):
        self.results = dict(results)
        self.on_fetch = on_fetch
        self.calls = []

    def fetch(self, url):
        self.calls.append(url)
        if self.on_fetch is not None:
            self.on_fetch(url, len(self.calls))
        return self.results[url]


def fetch_result(requested_url, content, final_url=None):
    return PAGE_MODULE.HTTPFetchResult(
        requested_url=requested_url,
        final_url=final_url or requested_url,
        content=content,
        content_type="application/atom+xml",
    )


def chain_client(page_count=3, host="catalog.example.org"):
    urls = [f"https://{host}/page{index}.xml" for index in range(page_count)]
    results = {}
    identifiers = (
        "urn:uuid:550e8400-e29b-41d4-a716-446655440000",
        "tag:example.org,2026:book-two",
        "https://ids.example.org/books/three",
        "uuid:7b98c250-7277-4fc4-b5d7-e04e323e294f",
    )
    for index, url in enumerate(urls):
        next_href = f"page{index + 1}.xml" if index + 1 < page_count else ""
        results[url] = fetch_result(
            url,
            feed_xml(
                f"Page {index}",
                identifiers[index % len(identifiers)],
                next_href=next_href,
            ),
        )
    return urls, FakeClient(results)


class RegisteredCatalogPageLoaderTests(unittest.TestCase):
    def setUp(self):
        PAGE_MODULE.clear_catalog_ref_registry()
        with PAGE_MODULE.catalog_page_cache_lock:
            PAGE_MODULE.catalog_page_cache.clear()
        PAGE_MODULE.APP_CONFIG = {
            "config_version": PAGE_MODULE.CONFIG_VERSION,
            "opds_url": "",
            "source_id": "",
            "source_name": "",
            "library_path": "X:/Books",
            "setup_complete": True,
        }

    def register_current(self, source_id, root_url, title="Example OPDS"):
        PAGE_MODULE.APP_CONFIG.update(
            opds_url=root_url,
            source_id=source_id,
            source_name=title,
        )
        ref = PAGE_MODULE.CatalogRef(
            source_id=source_id,
            url=root_url,
            title=title,
            kind="navigation",
        )
        return ref, PAGE_MODULE.register_catalog_ref(ref)

    def test_a_page_zero_uses_registered_root_once(self):
        urls, client = chain_client()
        _, token = self.register_current("source-a", urls[0])
        page = PAGE_MODULE.load_registered_catalog_page(token, client=client)
        self.assertEqual(client.calls, [urls[0]])
        self.assertEqual(page["page"], 0)
        self.assertEqual(page["page_url"], urls[0])

    def test_b_sequential_pages_follow_real_next_urls(self):
        urls, client = chain_client()
        _, token = self.register_current("source-a", urls[0])
        for page_number in range(3):
            PAGE_MODULE.load_registered_catalog_page(
                token, page=page_number, client=client
            )
        self.assertEqual(client.calls, urls)
        self.assertTrue(all("pageNumber" not in url for url in client.calls))

    def test_c_sequential_next_page_makes_one_new_request(self):
        urls, client = chain_client()
        _, token = self.register_current("source-a", urls[0])
        PAGE_MODULE.load_registered_catalog_page(token, page=0, client=client)
        calls_before = len(client.calls)
        PAGE_MODULE.load_registered_catalog_page(token, page=1, client=client)
        self.assertEqual(len(client.calls) - calls_before, 1)
        self.assertEqual(client.calls[-1], urls[1])

    def test_d_warm_direct_page_two_fetches_only_page_two(self):
        urls, client = chain_client()
        _, token = self.register_current("source-a", urls[0])
        PAGE_MODULE.load_registered_catalog_page(token, page=1, client=client)
        client.calls.clear()
        PAGE_MODULE.load_registered_catalog_page(token, page=2, client=client)
        self.assertEqual(client.calls, [urls[2]])

    def test_e_cold_direct_page_two_fetches_exact_chain(self):
        urls, client = chain_client()
        _, token = self.register_current("source-a", urls[0])
        page = PAGE_MODULE.load_registered_catalog_page(token, page=2, client=client)
        self.assertEqual(client.calls, urls)
        self.assertEqual(page["page"], 2)

    def test_f_cache_page_url_uses_final_redirect_url(self):
        requested = "https://example.org/start"
        final = "https://cdn.example.org/catalog/page0.xml"
        client = FakeClient(
            {
                requested: fetch_result(
                    requested,
                    feed_xml("Redirected", "urn:uuid:redirected"),
                    final_url=final,
                )
            }
        )
        ref, token = self.register_current("source-a", requested)
        page = PAGE_MODULE.load_registered_catalog_page(token, client=client)
        self.assertEqual(page["requested_url"], requested)
        self.assertEqual(page["page_url"], final)
        cached = PAGE_MODULE.cached_registered_catalog_page(ref, token, 0)
        self.assertEqual(cached["page_url"], final)

    def test_g_redirect_is_base_for_relative_next_and_acquisition(self):
        requested = "https://example.org/start"
        final = "https://cdn.example.org/catalog/page0.xml"
        client = FakeClient(
            {
                requested: fetch_result(
                    requested,
                    feed_xml(
                        "Redirected",
                        "urn:uuid:redirected",
                        next_href="page1.xml",
                        acquisition_href="books/book.epub",
                    ),
                    final_url=final,
                )
            }
        )
        _, token = self.register_current("source-a", requested)
        page = PAGE_MODULE.load_registered_catalog_page(token, client=client)
        self.assertEqual(
            page["next_url"],
            "https://cdn.example.org/catalog/page1.xml",
        )
        self.assertEqual(
            page["books"][0]["epub_url"],
            "https://cdn.example.org/catalog/books/book.epub",
        )

    def test_h_explicit_source_cache_namespaces_do_not_overlap(self):
        token = "catalog:" + "0" * 64
        ref_a = PAGE_MODULE.CatalogRef(
            "source-a", "https://example.org/a", "A", "navigation"
        )
        ref_b = PAGE_MODULE.CatalogRef(
            "source-b", "https://example.org/b", "B", "navigation"
        )
        key_a = PAGE_MODULE.registered_catalog_page_cache_key(ref_a, token, 0)
        key_b = PAGE_MODULE.registered_catalog_page_cache_key(ref_b, token, 0)
        self.assertNotEqual(key_a, key_b)

    def test_i_stale_token_is_rejected_after_source_change(self):
        urls, client = chain_client()
        _, token = self.register_current("source-a", urls[0])
        PAGE_MODULE.APP_CONFIG.update(
            opds_url="https://other.example.org/root.xml",
            source_id="source-b",
        )
        with self.assertRaisesRegex(ValueError, "недоступен или устарел"):
            PAGE_MODULE.load_registered_catalog_page(token, client=client)
        self.assertEqual(client.calls, [])

    def test_j_source_change_during_fetch_prevents_cache_write(self):
        urls, base_client = chain_client()
        ref, token = self.register_current("source-a", urls[0])

        def change_source(_url, _call_count):
            PAGE_MODULE.APP_CONFIG.update(
                opds_url="https://other.example.org/root.xml",
                source_id="source-b",
            )

        client = FakeClient(base_client.results, on_fetch=change_source)
        with self.assertRaisesRegex(ValueError, "изменён во время загрузки"):
            PAGE_MODULE.load_registered_catalog_page(token, client=client)
        key = PAGE_MODULE.registered_catalog_page_cache_key(ref, token, 0)
        self.assertNotIn(key, PAGE_MODULE.catalog_page_cache)

    def test_k_unknown_token_is_rejected_without_network(self):
        urls, client = chain_client()
        self.register_current("source-a", urls[0])
        with self.assertRaisesRegex(ValueError, "недоступен или устарел"):
            PAGE_MODULE.load_registered_catalog_page(
                "catalog:" + "f" * 64,
                client=client,
            )
        self.assertEqual(client.calls, [])

    def test_l_page_after_end_is_synthetic_and_not_cached(self):
        urls, client = chain_client()
        ref, token = self.register_current("source-a", urls[0])
        PAGE_MODULE.load_registered_catalog_page(token, page=2, client=client)
        calls_before = list(client.calls)
        result = PAGE_MODULE.load_registered_catalog_page(token, page=3, client=client)
        self.assertEqual(client.calls, calls_before)
        self.assertEqual(result["books"], [])
        self.assertFalse(result["has_next"])
        key = PAGE_MODULE.registered_catalog_page_cache_key(ref, token, 3)
        self.assertNotIn(key, PAGE_MODULE.catalog_page_cache)

    def test_m_cycle_stops_before_repeating_root_request(self):
        root = "https://cycle.example.org/page0.xml"
        page_one = "https://cycle.example.org/page1.xml"
        client = FakeClient(
            {
                root: fetch_result(
                    root,
                    feed_xml("Cycle 0", "urn:cycle:0", next_href="page1.xml"),
                ),
                page_one: fetch_result(
                    page_one,
                    feed_xml("Cycle 1", "urn:cycle:1", next_href="page0.xml"),
                ),
            }
        )
        ref, token = self.register_current("source-a", root)
        result = PAGE_MODULE.load_registered_catalog_page(token, page=2, client=client)
        self.assertEqual(client.calls, [root, page_one])
        self.assertEqual(result["books"], [])
        key = PAGE_MODULE.registered_catalog_page_cache_key(ref, token, 2)
        self.assertNotIn(key, PAGE_MODULE.catalog_page_cache)

    def test_n_page_at_maximum_limit_is_rejected(self):
        urls, client = chain_client()
        _, token = self.register_current("source-a", urls[0])
        with self.assertRaises(RuntimeError):
            PAGE_MODULE.load_registered_catalog_page(
                token,
                page=PAGE_MODULE.MAX_CATALOG_PAGES,
                client=client,
            )
        self.assertEqual(client.calls, [])

    def test_o_force_page_zero_invalidates_only_its_downstream(self):
        urls, client = chain_client()
        ref, token = self.register_current("source-a", urls[0])
        PAGE_MODULE.load_registered_catalog_page(token, page=2, client=client)

        other_ref = PAGE_MODULE.CatalogRef(
            "source-b", "https://other.example.org/root.xml", "Other", "navigation"
        )
        other_token = "catalog:" + "a" * 64
        other_key = PAGE_MODULE.registered_catalog_page_cache_key(
            other_ref, other_token, 7
        )
        PAGE_MODULE.catalog_page_cache[other_key] = {
            "title": "Other",
            "books": [],
            "page": 7,
            "has_next": False,
            "page_url": other_ref.url,
            "requested_url": other_ref.url,
            "next_url": "",
            "navigation": (),
            "time": time.time(),
        }

        client.calls.clear()
        PAGE_MODULE.load_registered_catalog_page(
            token, page=0, force=True, client=client
        )
        self.assertEqual(client.calls, [urls[0]])
        self.assertIn(
            PAGE_MODULE.registered_catalog_page_cache_key(ref, token, 0),
            PAGE_MODULE.catalog_page_cache,
        )
        for page_number in (1, 2):
            self.assertNotIn(
                PAGE_MODULE.registered_catalog_page_cache_key(
                    ref, token, page_number
                ),
                PAGE_MODULE.catalog_page_cache,
            )
        self.assertIn(other_key, PAGE_MODULE.catalog_page_cache)

    def test_p_force_page_n_uses_predecessor_and_invalidates_downstream(self):
        urls, client = chain_client(page_count=4)
        ref, token = self.register_current("source-a", urls[0])
        PAGE_MODULE.load_registered_catalog_page(token, page=3, client=client)
        client.calls.clear()
        PAGE_MODULE.load_registered_catalog_page(
            token, page=1, force=True, client=client
        )
        self.assertEqual(client.calls, [urls[1]])
        self.assertIn(
            PAGE_MODULE.registered_catalog_page_cache_key(ref, token, 0),
            PAGE_MODULE.catalog_page_cache,
        )
        self.assertIn(
            PAGE_MODULE.registered_catalog_page_cache_key(ref, token, 1),
            PAGE_MODULE.catalog_page_cache,
        )
        for page_number in (2, 3):
            self.assertNotIn(
                PAGE_MODULE.registered_catalog_page_cache_key(
                    ref, token, page_number
                ),
                PAGE_MODULE.catalog_page_cache,
            )

    def test_q_navigation_hides_urls_and_ignores_foreign_source(self):
        root = "https://example.org/root.xml"
        _, token = self.register_current("source-a", root)
        original_loader = PAGE_MODULE.load_opds_catalog_page
        PAGE_MODULE.load_opds_catalog_page = lambda *args, **kwargs: (
            PAGE_MODULE.OPDSCatalogPage(
                source_id="source-a",
                requested_url=root,
                final_url=root,
                title="Navigation",
                books=(),
                navigation=(
                    PAGE_MODULE.CatalogRef(
                        "source-a",
                        "https://example.org/allowed.xml",
                        "Allowed",
                        "navigation",
                    ),
                    PAGE_MODULE.CatalogRef(
                        "source-b",
                        "https://example.org/foreign.xml",
                        "Foreign",
                        "navigation",
                    ),
                ),
                next_url="",
            )
        )
        try:
            result = PAGE_MODULE.load_registered_catalog_page(
                token, client=object()
            )
        finally:
            PAGE_MODULE.load_opds_catalog_page = original_loader
        self.assertEqual(len(result["navigation"]), 1)
        registered = result["navigation"][0]
        self.assertIsInstance(registered, PAGE_MODULE.RegisteredCatalogRef)
        self.assertEqual(registered.title, "Allowed")
        self.assertFalse(hasattr(registered, "url"))

    def test_r_opaque_book_ids_remain_strings(self):
        urls, client = chain_client()
        _, token = self.register_current("source-a", urls[0])
        pages = [
            PAGE_MODULE.load_registered_catalog_page(
                token, page=page_number, client=client
            )
            for page_number in range(3)
        ]
        identifiers = [page["books"][0]["id"] for page in pages]
        self.assertTrue(all(isinstance(identifier, str) for identifier in identifiers))
        self.assertTrue(all(not identifier.isdecimal() for identifier in identifiers))
        self.assertTrue(any(identifier.startswith("urn:") for identifier in identifiers))
        self.assertTrue(any(identifier.startswith("https://") for identifier in identifiers))

    def test_s_new_and_legacy_loaders_remain_independent(self):
        protected = {"load_catalog_page"}
        found = set()
        for node in PAGE_MODULE.__source_tree__.body:
            name = getattr(node, "name", None)
            if name == "load_registered_catalog_page":
                called_names = {
                    child.func.id
                    for child in ast.walk(node)
                    if isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Name)
                }
                for legacy_name in (
                    "catalog_start_url",
                    "legacy_opds_get",
                    "parse_entry",
                    "load_catalog_page",
                ):
                    self.assertNotIn(legacy_name, called_names)
                source = (
                    ast.get_source_segment(PAGE_MODULE.__source_text__, node) or ""
                )
                self.assertNotIn("pageNumber", source)
            elif name in protected:
                found.add(name)
                called_names = {
                    child.func.id
                    for child in ast.walk(node)
                    if isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Name)
                }
                self.assertNotIn("load_registered_catalog_page", called_names)
        self.assertEqual(found, protected)

    def test_t_cached_read_returns_copy_without_mutating_stored_entry(self):
        urls, client = chain_client()
        ref, token = self.register_current("source-a", urls[0])
        PAGE_MODULE.load_registered_catalog_page(token, client=client)
        first = PAGE_MODULE.cached_registered_catalog_page(ref, token, 0)
        first["books"][0]["title"] = "Changed by caller"
        second = PAGE_MODULE.cached_registered_catalog_page(ref, token, 0)
        self.assertNotEqual(second["books"][0]["title"], "Changed by caller")


if __name__ == "__main__":
    unittest.main()
