import ast
import copy
import dataclasses
import hashlib
import ipaddress
import json
import re
import sys
import threading
import time
import types
import unicodedata
import unittest
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_full_catalog_module():
    """Загружает neutral full collector без runtime приложения."""
    source = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    wanted_definitions = {
        "SourceConfig",
        "normalize_app_config",
        "source_config_from_app_config",
        "normalize_opds_url",
        "source_namespace",
        "_opaque_key_part",
        "catalog_cache_key",
        "CatalogRef",
        "make_catalog_ref_token",
        "register_catalog_ref",
        "get_catalog_ref",
        "get_current_catalog_ref",
        "clear_catalog_ref_registry",
        "current_source_config",
        "parse_size_bytes",
        "parse_download_count",
        "normalize_duplicate_title",
        "duplicate_key",
        "technical_title_flags",
        "metadata_quality",
        "duplicate_score",
        "duplicate_id_tiebreak",
        "annotate_duplicates",
        "prepare_catalog_page_book",
        "registered_catalog_full_cache_key",
        "collect_registered_catalog",
    }
    wanted_assignments = {
        "CONFIG_VERSION",
        "MAX_CATALOG_REF_REGISTRY",
        "catalog_ref_registry",
        "catalog_ref_registry_lock",
        "MAX_CATALOG_PAGES",
        "CATALOG_CACHE_TTL",
        "catalog_cache",
        "catalog_lock",
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
        if assigned_names & wanted_assignments:
            body.append(node)

    module = types.ModuleType("isolated_registered_full_catalog_test")
    sys.modules[module.__name__] = module

    def apply_local_status(book):
        book.setdefault("exists_epub", False)
        book.setdefault("exists_fb2", False)
        book["exists_any"] = book["exists_epub"] or book["exists_fb2"]
        return book

    def apply_duplicate_local_status(book):
        book["duplicate_exists_epub"] = book.get("exists_epub", False)
        book["duplicate_exists_fb2"] = book.get("exists_fb2", False)
        book["duplicate_exists_any"] = book.get("exists_any", False)
        return book

    module.__dict__.update(
        dataclass=dataclasses.dataclass,
        copy=copy,
        hashlib=hashlib,
        ipaddress=ipaddress,
        json=json,
        re=re,
        threading=threading,
        time=time,
        unicodedata=unicodedata,
        urlsplit=urlsplit,
        urlunsplit=urlunsplit,
        DEFAULT_DESTINATION="test-default-library",
        APP_CONFIG={},
        apply_local_status=apply_local_status,
        apply_duplicate_local_status=apply_duplicate_local_status,
        load_registered_catalog_page=None,
    )
    exec(
        compile(ast.Module(body=body, type_ignores=[]), "app.py", "exec"),
        module.__dict__,
    )
    module.__source_text__ = source
    module.__source_tree__ = tree
    return module


FULL_MODULE = load_full_catalog_module()


def catalog_book(book_id, title="Book", author="Author"):
    return {
        "source_id": "source-a",
        "id": book_id,
        "title": title,
        "author": author,
        "authors": [author],
        "language": "en",
        "genres": ["fiction"],
        "series_links": [],
        "translator": "",
        "size": "",
        "size_bytes": 0,
        "downloads": "",
        "epub": True,
        "fb2": False,
        "cover_href": "",
        "exists_epub": False,
        "exists_fb2": False,
        "exists_any": False,
        "duplicate_count": 1,
        "duplicate_preferred": True,
        "duplicate_group": book_id,
        "duplicate_exists_epub": False,
        "duplicate_exists_fb2": False,
        "duplicate_exists_any": False,
    }


def page_result(page, books, has_next, title="Catalog"):
    url = f"https://catalog.example.org/page{page}.xml"
    return {
        "title": title,
        "books": copy.deepcopy(books),
        "page": page,
        "has_next": has_next,
        "page_url": url,
        "requested_url": url,
        "next_url": (
            f"https://catalog.example.org/page{page + 1}.xml"
            if has_next
            else ""
        ),
        "navigation": (),
        "time": time.time(),
    }


class RecordingPageLoader:
    def __init__(self, pages, callback=None):
        self.pages = pages
        self.callback = callback
        self.calls = []

    def __call__(self, token, page=0, force=False, client=None):
        self.calls.append(
            {"token": token, "page": page, "force": force, "client": client}
        )
        if self.callback is not None:
            self.callback(page)
        value = self.pages(page) if callable(self.pages) else self.pages[page]
        return copy.deepcopy(value)


class RegisteredFullCatalogTests(unittest.TestCase):
    def setUp(self):
        FULL_MODULE.clear_catalog_ref_registry()
        with FULL_MODULE.catalog_lock:
            FULL_MODULE.catalog_cache.clear()
        FULL_MODULE.APP_CONFIG = {
            "config_version": FULL_MODULE.CONFIG_VERSION,
            "opds_url": "",
            "source_id": "",
            "source_name": "",
            "library_path": "X:/Books",
            "setup_complete": True,
        }
        FULL_MODULE.load_registered_catalog_page = None

    def register_current(self, source_id="source-a", title="Example OPDS"):
        root_url = f"https://{source_id}.example.org/root.xml"
        FULL_MODULE.APP_CONFIG.update(
            opds_url=root_url,
            source_id=source_id,
            source_name=title,
        )
        ref = FULL_MODULE.CatalogRef(
            source_id=source_id,
            url=root_url,
            title=title,
            kind="navigation",
        )
        return ref, FULL_MODULE.register_catalog_ref(ref)

    def test_a_single_page_catalog(self):
        ref, token = self.register_current()
        loader = RecordingPageLoader(
            {0: page_result(0, [catalog_book("urn:book:one")], False, "One")}
        )
        FULL_MODULE.load_registered_catalog_page = loader
        result = FULL_MODULE.collect_registered_catalog(token, client="fake-client")
        self.assertEqual(result["pages"], 1)
        self.assertEqual(len(result["books"]), 1)
        self.assertEqual(result["title"], "One")
        self.assertEqual(
            FULL_MODULE.registered_catalog_full_cache_key(ref, token),
            FULL_MODULE.catalog_cache_key(ref.source_id, "opds", token),
        )

    def test_b_three_pages_are_collected_in_order(self):
        _, token = self.register_current()
        loader = RecordingPageLoader(
            {
                0: page_result(0, [catalog_book("urn:book:zero")], True),
                1: page_result(1, [catalog_book("tag:book:one")], True),
                2: page_result(2, [catalog_book("https://ids.example/book/two")], False),
            }
        )
        FULL_MODULE.load_registered_catalog_page = loader
        result = FULL_MODULE.collect_registered_catalog(token)
        self.assertEqual([call["page"] for call in loader.calls], [0, 1, 2])
        self.assertEqual(result["pages"], 3)
        self.assertEqual(len(result["books"]), 3)

    def test_c_collector_uses_only_registered_page_loader(self):
        node = next(
            node
            for node in FULL_MODULE.__source_tree__.body
            if getattr(node, "name", None) == "collect_registered_catalog"
        )
        called_names = {
            child.func.id
            for child in ast.walk(node)
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
        }
        self.assertIn("load_registered_catalog_page", called_names)
        for forbidden in (
            "load_opds_catalog_page",
            "legacy_opds_get",
            "catalog_start_url",
            "urljoin",
        ):
            self.assertNotIn(forbidden, called_names)
        source = ast.get_source_segment(FULL_MODULE.__source_text__, node) or ""
        self.assertNotIn("LEGACY_OPDS_BASE", source)
        self.assertNotIn("pageNumber", source)
        self.assertNotIn("next_url", source)

    def test_d_full_cache_hit_skips_page_loader_and_returns_copy(self):
        _, token = self.register_current()
        loader = RecordingPageLoader(
            {0: page_result(0, [catalog_book("urn:book:one")], False)}
        )
        FULL_MODULE.load_registered_catalog_page = loader
        first = FULL_MODULE.collect_registered_catalog(token)
        first["books"][0]["title"] = "Changed by caller"
        calls_before = len(loader.calls)
        second = FULL_MODULE.collect_registered_catalog(token)
        self.assertEqual(len(loader.calls), calls_before)
        self.assertNotEqual(second["books"][0]["title"], "Changed by caller")

    def test_e_force_only_marks_page_zero_and_replaces_full_cache(self):
        ref, token = self.register_current()
        initial_loader = RecordingPageLoader(
            {0: page_result(0, [catalog_book("urn:old")], False)}
        )
        FULL_MODULE.load_registered_catalog_page = initial_loader
        FULL_MODULE.collect_registered_catalog(token)

        forced_loader = RecordingPageLoader(
            {
                0: page_result(0, [catalog_book("urn:new:zero")], True),
                1: page_result(1, [catalog_book("urn:new:one")], False),
            }
        )
        FULL_MODULE.load_registered_catalog_page = forced_loader
        result = FULL_MODULE.collect_registered_catalog(token, force=True)
        self.assertEqual(
            [(call["page"], call["force"]) for call in forced_loader.calls],
            [(0, True), (1, False)],
        )
        self.assertEqual(
            {book["id"] for book in result["books"]},
            {"urn:new:zero", "urn:new:one"},
        )
        cache_key = FULL_MODULE.registered_catalog_full_cache_key(ref, token)
        self.assertEqual(
            {book["id"] for book in FULL_MODULE.catalog_cache[cache_key]["result"]["books"]},
            {"urn:new:zero", "urn:new:one"},
        )

    def test_f_source_namespaces_do_not_overlap(self):
        token = "catalog:" + "0" * 64
        ref_a = FULL_MODULE.CatalogRef(
            "source-a", "https://example.org/root", "A", "navigation"
        )
        ref_b = FULL_MODULE.CatalogRef(
            "source-b", "https://example.org/root", "B", "navigation"
        )
        key_a = FULL_MODULE.registered_catalog_full_cache_key(ref_a, token)
        key_b = FULL_MODULE.registered_catalog_full_cache_key(ref_b, token)
        self.assertNotEqual(key_a, key_b)

    def test_g_stale_token_is_rejected(self):
        _, token = self.register_current("source-a")
        FULL_MODULE.APP_CONFIG.update(
            opds_url="https://source-b.example.org/root.xml",
            source_id="source-b",
        )
        with self.assertRaisesRegex(ValueError, "недоступен или устарел"):
            FULL_MODULE.collect_registered_catalog(token)

    def test_h_source_change_during_traversal_prevents_full_cache_write(self):
        ref, token = self.register_current("source-a")

        def change_source(page):
            if page == 1:
                FULL_MODULE.APP_CONFIG.update(
                    opds_url="https://source-b.example.org/root.xml",
                    source_id="source-b",
                )

        loader = RecordingPageLoader(
            {
                0: page_result(0, [catalog_book("urn:zero")], True),
                1: page_result(1, [catalog_book("urn:one")], False),
            },
            callback=change_source,
        )
        FULL_MODULE.load_registered_catalog_page = loader
        with self.assertRaisesRegex(ValueError, "изменён во время загрузки"):
            FULL_MODULE.collect_registered_catalog(token)
        cache_key = FULL_MODULE.registered_catalog_full_cache_key(ref, token)
        self.assertNotIn(cache_key, FULL_MODULE.catalog_cache)

    def test_i_opaque_book_ids_are_preserved(self):
        _, token = self.register_current()
        identifiers = (
            "123",
            "550e8400-e29b-41d4-a716-446655440000",
            "https://ids.example.org/books/three",
            "sha256:" + "a" * 64,
        )
        loader = RecordingPageLoader(
            {0: page_result(0, [catalog_book(value) for value in identifiers], False)}
        )
        FULL_MODULE.load_registered_catalog_page = loader
        result = FULL_MODULE.collect_registered_catalog(token)
        self.assertEqual(
            tuple(book["id"] for book in result["books"]),
            identifiers,
        )
        self.assertTrue(all(isinstance(book["id"], str) for book in result["books"]))

    def test_j_duplicate_logic_supports_opaque_ids(self):
        _, token = self.register_current()
        books = [
            catalog_book("urn:uuid:first", title="Same", author="Author"),
            catalog_book(
                "https://ids.example.org/second", title="Same", author="Author"
            ),
        ]
        loader = RecordingPageLoader({0: page_result(0, books, False)})
        FULL_MODULE.load_registered_catalog_page = loader
        result = FULL_MODULE.collect_registered_catalog(token)
        self.assertEqual(result["duplicate_groups"], 1)
        self.assertEqual(result["duplicate_extra"], 1)
        self.assertTrue(all(book["duplicate_count"] == 2 for book in result["books"]))

    def test_k_max_pages_never_requests_page_at_limit(self):
        _, token = self.register_current()

        def endless_page(page):
            return page_result(page, [catalog_book(f"urn:book:{page}")], True)

        loader = RecordingPageLoader(endless_page)
        FULL_MODULE.load_registered_catalog_page = loader
        with self.assertRaisesRegex(RuntimeError, "Превышен лимит"):
            FULL_MODULE.collect_registered_catalog(token)
        self.assertEqual(len(loader.calls), FULL_MODULE.MAX_CATALOG_PAGES)
        self.assertEqual(loader.calls[-1]["page"], FULL_MODULE.MAX_CATALOG_PAGES - 1)
        self.assertNotIn(
            FULL_MODULE.MAX_CATALOG_PAGES,
            [call["page"] for call in loader.calls],
        )

    def test_l_pages_is_count_not_last_index(self):
        _, token = self.register_current()
        loader = RecordingPageLoader(
            {
                0: page_result(0, [catalog_book("urn:zero")], True),
                1: page_result(1, [catalog_book("urn:one")], False),
            }
        )
        FULL_MODULE.load_registered_catalog_page = loader
        result = FULL_MODULE.collect_registered_catalog(token)
        self.assertEqual(result["pages"], 2)

    def test_m_empty_first_title_falls_back_to_ref_title(self):
        _, token = self.register_current(title="Configured title")
        loader = RecordingPageLoader(
            {0: page_result(0, [catalog_book("urn:one")], False, title="")}
        )
        FULL_MODULE.load_registered_catalog_page = loader
        result = FULL_MODULE.collect_registered_catalog(token)
        self.assertEqual(result["title"], "Configured title")

    def test_n_full_cache_key_does_not_expose_url(self):
        ref, token = self.register_current()
        key = FULL_MODULE.registered_catalog_full_cache_key(ref, token)
        self.assertNotIn(ref.url, key)
        self.assertNotIn("example.org", key)
        self.assertNotIn("https", key)

    def test_o_legacy_full_catalog_functions_remain_independent(self):
        found = set()
        for node in FULL_MODULE.__source_tree__.body:
            name = getattr(node, "name", None)
            if name not in {"collect_catalog", "get_cached_catalog"}:
                continue
            found.add(name)
            called_names = {
                child.func.id
                for child in ast.walk(node)
                if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
            }
            self.assertNotIn("collect_registered_catalog", called_names)
            self.assertNotIn("load_registered_catalog_page", called_names)
        self.assertEqual(found, {"collect_catalog", "get_cached_catalog"})

    def test_p_failed_force_keeps_previous_full_cache(self):
        ref, token = self.register_current()
        initial_loader = RecordingPageLoader(
            {0: page_result(0, [catalog_book("urn:old")], False)}
        )
        FULL_MODULE.load_registered_catalog_page = initial_loader
        FULL_MODULE.collect_registered_catalog(token)
        cache_key = FULL_MODULE.registered_catalog_full_cache_key(ref, token)
        snapshot = copy.deepcopy(FULL_MODULE.catalog_cache[cache_key])

        def fail_loader(*args, **kwargs):
            raise RuntimeError("forced failure")

        FULL_MODULE.load_registered_catalog_page = fail_loader
        with self.assertRaisesRegex(RuntimeError, "forced failure"):
            FULL_MODULE.collect_registered_catalog(token, force=True)
        self.assertEqual(FULL_MODULE.catalog_cache[cache_key], snapshot)


if __name__ == "__main__":
    unittest.main()
