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
PAGE_ONE = (FIXTURE_DIR / "search_results_page_1.xml").read_bytes()
PAGE_TWO = (FIXTURE_DIR / "search_results_page_2.xml").read_bytes()


def synthetic_feed(item_id, title, next_href=""):
    next_link = (
        f'<link rel="next" type="application/atom+xml;profile=opds-catalog" '
        f'href="{next_href}" />'
        if next_href
        else ""
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <id>urn:synthetic:{item_id}</id><title>{title}</title>
      <updated>2026-08-16T09:00:00Z</updated>{next_link}
      <entry><id>{item_id}</id><title>{title}</title>
        <updated>2026-08-16T09:00:00Z</updated>
        <author><name>Example Author</name></author>
        <link rel="http://opds-spec.org/acquisition/open-access"
              type="application/epub+zip" href="book.epub" />
      </entry>
    </feed>""".encode("utf-8")


PAGE_TWO_WITH_NEXT = synthetic_feed(
    "urn:uuid:13bba4bf-b790-47cb-93de-fc3582f53aa1",
    "Intermediate results",
    "?cursor=last",
)
PAGE_TWO_WITH_CYCLE = synthetic_feed(
    "urn:uuid:ba5b98ca-55d8-4fe5-8293-bb692629cff9",
    "Cyclic results",
    "?cursor=next",
)
PAGE_THREE = synthetic_feed(
    "tag:catalog.example.test,2026:last-result",
    "Last result",
)


def load_cache_module():
    """Загружает neutral search chain и существующий OPDS page pipeline."""
    source = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    wanted = {
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
        "normalize_opds_search_query",
        "expand_opds_search_template",
        "_catalog_acquisition_format",
        "book_record_to_catalog_book",
        "load_opds_catalog_page",
        "load_opds_search_page",
        "_validate_opds_search_page_number",
        "opds_search_cache_identity",
        "opds_search_page_cache_key",
        "_cached_opds_search_page",
        "_store_opds_search_page",
        "load_cached_opds_search_page",
    }
    body = []
    for node in tree.body:
        if getattr(node, "name", None) in wanted:
            body.append(node)
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name)
            and (
                target.id.startswith("OPDS1_")
                or target.id.startswith("OPENSEARCH_")
            )
            for target in node.targets
        ):
            body.append(node)
    module = types.ModuleType("isolated_opds_search_cache_test")
    sys.modules[module.__name__] = module
    module.__dict__.update(
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
        MAX_CATALOG_PAGES=5,
        SEARCH_CACHE_TTL=30 * 60,
        opds_search_page_cache={},
        opds_search_page_cache_lock=threading.Lock(),
    )
    exec(compile(ast.Module(body=body, type_ignores=[]), "app.py", "exec"), module.__dict__)
    module.__source_text__ = source
    module.__source_tree__ = tree
    return module


CACHE_MODULE = load_cache_module()


class RoutingFakeClient:
    def __init__(self, page_two=PAGE_TWO, page_three=PAGE_THREE):
        self.page_two = page_two
        self.page_three = page_three
        self.calls = []

    def fetch(self, url):
        self.calls.append(url)
        if "cursor=last" in url:
            content = self.page_three
        elif "cursor=next" in url:
            content = self.page_two
        else:
            content = PAGE_ONE
        return types.SimpleNamespace(
            requested_url=url,
            final_url=url,
            content=content,
        )


def descriptor(parameter="term", path="find", page_offset=1):
    return CACHE_MODULE.OPDSSearchDescriptor(
        template=f"https://reader.example.test/{path}?{parameter}={{searchTerms}}",
        mime_type="application/atom+xml;profile=opds-catalog",
        page_offset=page_offset,
    )


class OPDSSearchCacheTests(unittest.TestCase):
    def setUp(self):
        with CACHE_MODULE.opds_search_page_cache_lock:
            CACHE_MODULE.opds_search_page_cache.clear()

    def test_a_cache_key_is_stable_and_includes_page(self):
        first = CACHE_MODULE.opds_search_page_cache_key(
            "source-a", descriptor(), "  Dune  ", 0
        )
        second = CACHE_MODULE.opds_search_page_cache_key(
            "source-a", descriptor(), "Dune", 0
        )
        next_page = CACHE_MODULE.opds_search_page_cache_key(
            "source-a", descriptor(), "Dune", 1
        )
        self.assertEqual(first, second)
        self.assertEqual(first[0], next_page[0])
        self.assertEqual((first[1], next_page[1]), (0, 1))
        self.assertNotIn("Dune", first[0])
        self.assertNotIn("reader.example.test", first[0])

    def test_b_page_zero_uses_expanded_search_template(self):
        client = RoutingFakeClient()
        result = self.load(client, page=0)
        self.assertEqual(client.calls, ["https://reader.example.test/find?term=Dune"])
        self.assertEqual(result.requested_url, client.calls[0])

    def test_c_page_one_uses_only_real_previous_next_url(self):
        client = RoutingFakeClient()
        result = self.load(client, page=1)
        self.assertEqual(
            client.calls,
            [
                "https://reader.example.test/find?term=Dune",
                "https://reader.example.test/find?cursor=next",
            ],
        )
        self.assertEqual(result.requested_url, client.calls[1])
        self.assertEqual(result.total_results, 57)
        self.assertEqual(sum("term=" in url for url in client.calls), 1)

    def test_d_cold_page_two_builds_contiguous_chain(self):
        client = RoutingFakeClient(page_two=PAGE_TWO_WITH_NEXT)
        result = self.load(client, page=2)
        self.assertEqual(
            client.calls,
            [
                "https://reader.example.test/find?term=Dune",
                "https://reader.example.test/find?cursor=next",
                "https://reader.example.test/find?cursor=last",
            ],
        )
        self.assertEqual(result.requested_url, client.calls[2])

    def test_e_cache_hit_does_not_repeat_http(self):
        client = RoutingFakeClient()
        first = self.load(client, page=1)
        call_count = len(client.calls)
        second = self.load(client, page=1)
        self.assertEqual(len(client.calls), call_count)
        self.assertEqual(first, second)
        self.assertIsNot(first, second)
        self.assertEqual(first.total_results, 57)
        self.assertEqual(second.total_results, 57)

    def test_f_force_reloads_requested_page_only(self):
        client = RoutingFakeClient()
        self.load(client, page=1)
        self.load(client, page=1, force=True)
        self.assertEqual(len(client.calls), 3)
        self.assertEqual(client.calls[-1], "https://reader.example.test/find?cursor=next")

    def test_g_force_page_zero_invalidates_downstream(self):
        client = RoutingFakeClient()
        self.load(client, page=1)
        self.load(client, page=0, force=True)
        self.assertEqual(len(client.calls), 3)
        self.load(client, page=1)
        self.assertEqual(len(client.calls), 4)
        self.assertEqual(client.calls[-1], "https://reader.example.test/find?cursor=next")

    def test_h_force_page_n_invalidates_only_its_downstream(self):
        client = RoutingFakeClient(page_two=PAGE_TWO_WITH_NEXT)
        self.load(client, page=2)
        self.load(client, page=1, force=True)
        self.assertEqual(len(client.calls), 4)
        self.load(client, page=0)
        self.assertEqual(len(client.calls), 4)
        self.load(client, page=2)
        self.assertEqual(len(client.calls), 5)
        self.assertEqual(client.calls[-1], "https://reader.example.test/find?cursor=last")

    def test_i_query_normalization_and_isolation(self):
        client = RoutingFakeClient()
        self.load(client, query="  Dune  ")
        self.load(client, query="Dune")
        self.assertEqual(len(client.calls), 1)
        self.load(client, query="Dune  Messiah")
        self.assertEqual(len(client.calls), 2)

    def test_j_source_isolation_uses_opaque_strings(self):
        client = RoutingFakeClient()
        first = self.load(client, source_id="source-a")
        second = self.load(client, source_id="urn:source:b")
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(first.source_id, "source-a")
        self.assertEqual(second.source_id, "urn:source:b")
        self.assertTrue(all(book["source_id"] == "urn:source:b" for book in second.books))

    def test_k_descriptor_isolation_uses_template_mime_and_page_offset(self):
        client = RoutingFakeClient()
        self.load(client, search_descriptor=descriptor())
        self.load(client, search_descriptor=descriptor(parameter="catalog-key"))
        alternate_mime = CACHE_MODULE.OPDSSearchDescriptor(
            template=descriptor().template,
            mime_type="application/atom+xml;charset=utf-8",
        )
        self.load(client, search_descriptor=alternate_mime)
        self.load(client, search_descriptor=descriptor(page_offset=-2))
        self.assertEqual(len(client.calls), 4)

    def test_l_page_limits_are_controlled(self):
        client = RoutingFakeClient()
        for page in (-1, CACHE_MODULE.MAX_CATALOG_PAGES):
            with self.subTest(page=page), self.assertRaisesRegex(
                ValueError,
                "Некорректный номер страницы",
            ):
                self.load(client, page=page)
        self.assertEqual(client.calls, [])

    def test_m_missing_next_does_not_invent_url(self):
        client = RoutingFakeClient()
        with self.assertRaisesRegex(ValueError, "отсутствует следующая страница"):
            self.load(client, page=2)
        self.assertEqual(
            client.calls,
            [
                "https://reader.example.test/find?term=Dune",
                "https://reader.example.test/find?cursor=next",
            ],
        )

    def test_n_cycle_is_detected_before_repeated_request(self):
        client = RoutingFakeClient(page_two=PAGE_TWO_WITH_CYCLE)
        with self.assertRaisesRegex(ValueError, "цикл"):
            self.load(client, page=2)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(len(set(client.calls)), 2)

    def test_o_source_identity_and_opaque_ids_survive_cached_pages(self):
        client = RoutingFakeClient()
        result = self.load(client, page=1, source_id="sha256:opaque-source")
        self.assertEqual(result.source_id, "sha256:opaque-source")
        self.assertEqual(result.books[0]["id"], "tag:catalog.example.test,2026:silver-orchard")
        cached = self.load(client, page=1, source_id="sha256:opaque-source")
        self.assertEqual(cached.books[0]["id"], result.books[0]["id"])
        self.assertEqual(len(client.calls), 2)

    def test_p_new_cache_layer_reuses_existing_loaders_without_direct_http(self):
        source = self.cache_layer_source()
        self.assertIn("load_opds_search_page(", source)
        self.assertIn("load_opds_catalog_page(", source)
        self.assertNotIn("requests.", source)

    def test_q_new_cache_layer_has_no_legacy_source_coupling(self):
        source = self.cache_layer_source()
        fixture_source = (FIXTURE_DIR / "search_results_page_2.xml").read_text(
            encoding="utf-8"
        )
        test_source = Path(__file__).read_text(encoding="utf-8")
        corpus = "\n".join((source, fixture_source, test_source)).lower()
        corpus = corpus.replace("{searchterms}", "")
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
                self.assertNotIn(marker, corpus)

    def test_r_start_page_is_used_only_for_initial_request_before_rel_next(self):
        client = RoutingFakeClient()
        search_descriptor = CACHE_MODULE.OPDSSearchDescriptor(
            template=(
                "https://reader.example.test/find?term={searchTerms}"
                "&page={startPage?}"
            ),
            mime_type="application/atom+xml;profile=opds-catalog",
            page_offset=1,
        )
        result = self.load(
            client,
            page=1,
            search_descriptor=search_descriptor,
        )
        self.assertEqual(
            client.calls,
            [
                "https://reader.example.test/find?term=Dune&page=1",
                "https://reader.example.test/find?cursor=next",
            ],
        )
        self.assertEqual(result.requested_url, client.calls[1])
        self.assertNotIn("page=2", "\n".join(client.calls))

    def load(
        self,
        client,
        page=0,
        force=False,
        query="Dune",
        source_id="source-a",
        search_descriptor=None,
    ):
        return CACHE_MODULE.load_cached_opds_search_page(
            search_descriptor or descriptor(),
            query,
            source_id=source_id,
            page=page,
            force=force,
            client=client,
        )

    def function_source(self, name):
        for node in CACHE_MODULE.__source_tree__.body:
            if getattr(node, "name", None) == name:
                return ast.get_source_segment(CACHE_MODULE.__source_text__, node) or ""
        self.fail(f"Function not found: {name}")

    def cache_layer_source(self):
        return "\n".join(
            self.function_source(name)
            for name in (
                "_validate_opds_search_page_number",
                "opds_search_cache_identity",
                "opds_search_page_cache_key",
                "_cached_opds_search_page",
                "_store_opds_search_page",
                "load_cached_opds_search_page",
            )
        )


if __name__ == "__main__":
    unittest.main()
