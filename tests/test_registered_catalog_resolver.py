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
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = PROJECT_ROOT / "tests" / "fixtures" / "opds"


def load_resolver_module():
    source = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    wanted_definitions = {
        "SourceConfig",
        "normalize_app_config",
        "source_config_from_app_config",
        "normalize_opds_url",
        "resolve_opds_url",
        "same_origin",
        "is_safe_http_url",
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
        "normalize_catalog_semantic_title",
        "is_author_related_catalog_title",
        "is_alphabetical_catalog_title",
        "is_all_books_catalog_title",
        "is_recent_catalog_title",
        "select_preferred_registered_catalog_child",
        "OPDSFeed",
        "OPDSCatalogPage",
        "OPDS1Provider",
        "_catalog_acquisition_format",
        "book_record_to_catalog_book",
        "choose_catalog_book_format",
        "catalog_book_has_downloadable_acquisition",
        "HTTPFetchResult",
        "load_opds_catalog_page",
        "current_source_config",
        "prepare_catalog_page_book",
        "registered_catalog_page_cache_key",
        "cached_registered_catalog_page",
        "_empty_registered_catalog_page",
        "load_registered_catalog_page",
        "resolve_preferred_registered_catalog_token",
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

    module = types.ModuleType("isolated_registered_catalog_resolver_test")
    sys.modules[module.__name__] = module
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


RESOLVER_MODULE = load_resolver_module()


class FakeClient:
    def __init__(self, results):
        self.results = dict(results)
        self.calls = []

    def fetch(self, url):
        self.calls.append(url)
        return self.results[url]


def fixture_result(name, url):
    return RESOLVER_MODULE.HTTPFetchResult(
        requested_url=url,
        final_url=url,
        content=(FIXTURE_DIR / name).read_bytes(),
        content_type="application/atom+xml",
    )


def page(books=(), navigation=(), has_next=False):
    return {
        "title": "Test catalog",
        "books": list(books),
        "page": 0,
        "has_next": has_next,
        "navigation": tuple(navigation),
    }


def downloadable_book(file_format="epub", item_id="book"):
    book = {
        "id": item_id,
        "epub_url": "",
        "fb2_url": "",
    }
    book[f"{file_format}_url"] = (
        f"https://files.example.test/{item_id}.{file_format}"
    )
    return book


def metadata_book(item_id="metadata"):
    return {
        "id": item_id,
        "title": "Об авторе",
        "acquisition_links": [
            {
                "href": "https://files.example.test/about.pdf",
                "mime_type": "application/pdf",
            }
        ],
        "web_url": "https://catalog.example.test/about",
        "cover_url": "https://catalog.example.test/about.jpg",
        "epub_url": "",
        "fb2_url": "",
    }


class RegisteredCatalogResolverTests(unittest.TestCase):
    def setUp(self):
        RESOLVER_MODULE.clear_catalog_ref_registry()
        with RESOLVER_MODULE.catalog_page_cache_lock:
            RESOLVER_MODULE.catalog_page_cache.clear()
        RESOLVER_MODULE.APP_CONFIG = {
            "config_version": RESOLVER_MODULE.CONFIG_VERSION,
            "opds_url": "",
            "source_id": "",
            "source_name": "",
            "library_path": "X:/Books",
            "setup_complete": True,
        }
        self.original_loader = RESOLVER_MODULE.load_registered_catalog_page

    def tearDown(self):
        RESOLVER_MODULE.load_registered_catalog_page = self.original_loader

    def register_parent(
        self,
        title="Все книги автора Example Author",
        kind="related",
        url="https://catalog.example.test/author.xml",
    ):
        source_id = "source-test"
        RESOLVER_MODULE.APP_CONFIG.update(
            opds_url=url,
            source_id=source_id,
            source_name="Test OPDS",
        )
        ref = RESOLVER_MODULE.CatalogRef(source_id, url, title, kind)
        return RESOLVER_MODULE.register_catalog_ref(ref)

    def register_child(self, title, kind="acquisition", url=None):
        url = url or f"https://catalog.example.test/{len(title)}.xml"
        ref = RESOLVER_MODULE.CatalogRef(
            "source-test",
            url,
            title,
            kind,
        )
        token = RESOLVER_MODULE.register_catalog_ref(ref)
        return RESOLVER_MODULE.RegisteredCatalogRef(token, title, kind)

    def install_pages(self, pages):
        calls = []

        def loader(token, page=0, client=None):
            calls.append((token, page, client))
            value = pages[token]
            if isinstance(value, BaseException):
                raise value
            return value

        RESOLVER_MODULE.load_registered_catalog_page = loader
        return calls

    def test_a_unique_acquisition_child_with_books_is_selected(self):
        root_token = self.register_parent()
        child = self.register_child("Author books")
        calls = self.install_pages(
            {
                root_token: page(navigation=(child,)),
                child.token: page(books=(downloadable_book(),)),
            }
        )

        resolved = RESOLVER_MODULE.resolve_preferred_registered_catalog_token(
            root_token,
            client="client-sentinel",
        )

        self.assertEqual(resolved, child.token)
        self.assertEqual(
            calls,
            [
                (root_token, 0, "client-sentinel"),
                (child.token, 0, "client-sentinel"),
            ],
        )

    def test_b_unique_alphabetical_child_and_its_pagination_are_used(self):
        root_url = "https://catalog.example.test/registered_author_navigation.xml"
        alphabet_url = (
            "https://catalog.example.test/registered_author_alphabet_page_1.xml"
        )
        alphabet_page_two_url = (
            "https://catalog.example.test/registered_author_alphabet_page_2.xml"
        )
        root_token = self.register_parent(url=root_url)
        client = FakeClient(
            {
                root_url: fixture_result(
                    "registered_author_navigation.xml",
                    root_url,
                ),
                alphabet_url: fixture_result(
                    "registered_author_alphabet_page_1.xml",
                    alphabet_url,
                ),
                alphabet_page_two_url: fixture_result(
                    "registered_author_alphabet_page_2.xml",
                    alphabet_page_two_url,
                ),
            }
        )

        child_token = RESOLVER_MODULE.resolve_preferred_registered_catalog_token(
            root_token,
            client=client,
        )
        expected_token = RESOLVER_MODULE.make_catalog_ref_token(
            "source-test",
            alphabet_url,
        )
        second_page = RESOLVER_MODULE.load_registered_catalog_page(
            child_token,
            page=1,
            client=client,
        )

        self.assertEqual(child_token, expected_token)
        root_page = RESOLVER_MODULE.load_registered_catalog_page(
            root_token,
            client=client,
        )
        self.assertEqual(root_page["books"][0]["title"], "Об авторе")
        self.assertFalse(
            RESOLVER_MODULE.catalog_book_has_downloadable_acquisition(
                root_page["books"][0]
            )
        )
        self.assertEqual(
            {item.kind for item in root_page["navigation"]},
            {"unknown"},
        )
        self.assertEqual(second_page["page"], 1)
        self.assertEqual(second_page["books"][0]["title"], "Alphabetical Book Two")
        self.assertEqual(
            client.calls,
            [root_url, alphabet_url, alphabet_page_two_url],
        )

    def test_c_root_with_books_is_not_followed(self):
        root_token = self.register_parent()
        calls = self.install_pages(
            {root_token: page(books=(downloadable_book(),))}
        )
        self.assertEqual(
            RESOLVER_MODULE.resolve_preferred_registered_catalog_token(root_token),
            root_token,
        )
        self.assertEqual(len(calls), 1)

    def test_d_mixed_books_and_navigation_is_not_followed(self):
        root_token = self.register_parent()
        child = self.register_child(
            "Книги по алфавиту",
            kind="unknown",
        )
        calls = self.install_pages(
            {
                root_token: page(
                    books=(downloadable_book(),),
                    navigation=(child,),
                )
            }
        )
        self.assertEqual(
            RESOLVER_MODULE.resolve_preferred_registered_catalog_token(root_token),
            root_token,
        )
        self.assertEqual(len(calls), 1)

    def test_e_paginated_navigation_root_is_not_followed(self):
        root_token = self.register_parent()
        child = self.register_child("Author books")
        calls = self.install_pages(
            {root_token: page(navigation=(child,), has_next=True)}
        )
        self.assertEqual(
            RESOLVER_MODULE.resolve_preferred_registered_catalog_token(root_token),
            root_token,
        )
        self.assertEqual(len(calls), 1)

    def test_f_ambiguous_children_keep_root_navigation(self):
        root_url = "https://catalog.example.test/registered_author_ambiguous.xml"
        root_token = self.register_parent(url=root_url)
        client = FakeClient(
            {
                root_url: fixture_result(
                    "registered_author_ambiguous.xml",
                    root_url,
                )
            }
        )
        self.assertEqual(
            RESOLVER_MODULE.resolve_preferred_registered_catalog_token(
                root_token,
                client=client,
            ),
            root_token,
        )
        self.assertEqual(client.calls, [root_url])

    def test_g_series_parent_is_not_loaded(self):
        root_token = self.register_parent(title="Books in Example Series")
        child = self.register_child("Alphabetical", kind="unknown")
        calls = self.install_pages(
            {root_token: page(navigation=(child,))}
        )
        self.assertEqual(
            RESOLVER_MODULE.resolve_preferred_registered_catalog_token(root_token),
            root_token,
        )
        self.assertEqual(calls, [])

    def test_h_generic_related_parent_is_not_loaded(self):
        root_token = self.register_parent(title="Featured collection")
        child = self.register_child("Alphabetical", kind="unknown")
        calls = self.install_pages(
            {root_token: page(navigation=(child,))}
        )
        self.assertEqual(
            RESOLVER_MODULE.resolve_preferred_registered_catalog_token(root_token),
            root_token,
        )
        self.assertEqual(calls, [])

    def test_i_non_related_parent_is_not_loaded(self):
        for kind in ("navigation", "acquisition"):
            with self.subTest(kind=kind):
                RESOLVER_MODULE.clear_catalog_ref_registry()
                root_token = self.register_parent(kind=kind)
                calls = self.install_pages({})
                self.assertEqual(
                    RESOLVER_MODULE.resolve_preferred_registered_catalog_token(
                        root_token
                    ),
                    root_token,
                )
                self.assertEqual(calls, [])

    def test_j_metadata_only_preferred_child_keeps_root(self):
        root_token = self.register_parent()
        child = self.register_child("Author books")
        calls = self.install_pages(
            {
                root_token: page(navigation=(child,)),
                child.token: page(books=(metadata_book(),)),
            }
        )
        self.assertEqual(
            RESOLVER_MODULE.resolve_preferred_registered_catalog_token(root_token),
            root_token,
        )
        self.assertEqual(len(calls), 2)

    def test_k_cycle_to_root_token_stops_after_root_load(self):
        root_url = "https://catalog.example.test/registered_author_cycle.xml"
        root_token = self.register_parent(url=root_url)
        client = FakeClient(
            {
                root_url: fixture_result(
                    "registered_author_cycle.xml",
                    root_url,
                )
            }
        )
        self.assertEqual(
            RESOLVER_MODULE.resolve_preferred_registered_catalog_token(
                root_token,
                client=client,
            ),
            root_token,
        )
        self.assertEqual(client.calls, [root_url])

    def test_l_child_load_exception_propagates(self):
        root_token = self.register_parent()
        child = self.register_child("Author books")
        calls = self.install_pages(
            {
                root_token: page(navigation=(child,)),
                child.token: RuntimeError("child parser failed"),
            }
        )
        with self.assertRaisesRegex(RuntimeError, "child parser failed"):
            RESOLVER_MODULE.resolve_preferred_registered_catalog_token(root_token)
        self.assertEqual(len(calls), 2)

    def test_m_title_classifiers_are_exact_and_normalized(self):
        for title in (
            "  «ВСЕ   КНИГИ АВТОРА Example Author!»  ",
            "Книги автора Example Author",
            "All books by Example Author",
            "Books by Example Author",
        ):
            with self.subTest(parent=title):
                self.assertTrue(
                    RESOLVER_MODULE.is_author_related_catalog_title(title)
                )
        self.assertFalse(
            RESOLVER_MODULE.is_author_related_catalog_title("Author directory")
        )
        self.assertTrue(
            RESOLVER_MODULE.is_alphabetical_catalog_title("Books by title")
        )
        self.assertFalse(
            RESOLVER_MODULE.is_alphabetical_catalog_title(
                "Alphabetical magazines"
            )
        )

    def test_n_recent_child_is_never_the_unique_preference(self):
        recent = self.register_child("Recent books")
        selected = RESOLVER_MODULE.select_preferred_registered_catalog_child(
            (recent,)
        )
        self.assertIsNone(selected)

    def test_n2_alphabetical_navigation_child_is_not_an_acquisition(self):
        navigation = self.register_child(
            "Книги по алфавиту",
            kind="navigation",
        )
        selected = RESOLVER_MODULE.select_preferred_registered_catalog_child(
            (navigation,)
        )
        self.assertIsNone(selected)

    def test_n3_unique_all_books_is_the_bounded_fallback(self):
        all_books = self.register_child("All books")
        other = self.register_child(
            "By language",
            url="https://catalog.example.test/by-language.xml",
        )
        selected = RESOLVER_MODULE.select_preferred_registered_catalog_child(
            (other, all_books)
        )
        self.assertEqual(selected, all_books)

    def test_n4_two_alphabetical_candidates_are_ambiguous(self):
        first = self.register_child("Alphabetical")
        second = self.register_child(
            "Books by title",
            url="https://catalog.example.test/books-by-title.xml",
        )
        selected = RESOLVER_MODULE.select_preferred_registered_catalog_child(
            (first, second)
        )
        self.assertIsNone(selected)

    def test_n5_unique_unknown_alphabetical_is_selected(self):
        alphabetical = self.register_child(
            "Книги по алфавиту",
            kind="unknown",
        )
        selected = RESOLVER_MODULE.select_preferred_registered_catalog_child(
            (alphabetical,)
        )
        self.assertEqual(selected, alphabetical)

    def test_n6_two_unknown_alphabetical_candidates_are_ambiguous(self):
        first = self.register_child("Alphabetical", kind="unknown")
        second = self.register_child(
            "Books by title",
            kind="unknown",
            url="https://catalog.example.test/unknown-books-by-title.xml",
        )
        selected = RESOLVER_MODULE.select_preferred_registered_catalog_child(
            (first, second)
        )
        self.assertIsNone(selected)

    def test_n7_unknown_non_alphabetical_and_recent_are_not_selected(self):
        navigation = (
            self.register_child("Книги по сериям", kind="unknown"),
            self.register_child(
                "Книги вне серий",
                kind="unknown",
                url="https://catalog.example.test/no-series.xml",
            ),
            self.register_child(
                "Книги по дате поступления",
                kind="unknown",
                url="https://catalog.example.test/recent.xml",
            ),
        )
        selected = RESOLVER_MODULE.select_preferred_registered_catalog_child(
            navigation
        )
        self.assertIsNone(selected)

    def test_n8_metadata_root_allows_unique_unknown_alphabetical_child(self):
        root_token = self.register_parent()
        child = self.register_child(
            "Книги по алфавиту",
            kind="unknown",
        )
        calls = self.install_pages(
            {
                root_token: page(
                    books=(metadata_book(),),
                    navigation=(child,),
                ),
                child.token: page(books=(downloadable_book(),)),
            }
        )
        self.assertEqual(
            RESOLVER_MODULE.resolve_preferred_registered_catalog_token(root_token),
            child.token,
        )
        self.assertEqual(len(calls), 2)

    def test_n8b_unknown_all_books_is_not_selected(self):
        all_books = self.register_child("All books", kind="unknown")
        selected = RESOLVER_MODULE.select_preferred_registered_catalog_child(
            (all_books,)
        )
        self.assertIsNone(selected)

    def test_n9_fb2_child_passes_downloadable_validation(self):
        root_token = self.register_parent()
        child = self.register_child("Author books")
        calls = self.install_pages(
            {
                root_token: page(navigation=(child,)),
                child.token: page(books=(downloadable_book("fb2"),)),
            }
        )
        self.assertEqual(
            RESOLVER_MODULE.resolve_preferred_registered_catalog_token(root_token),
            child.token,
        )
        self.assertEqual(len(calls), 2)

    def test_o_new_resolver_helpers_have_no_source_specific_hardcodes(self):
        helper_names = {
            "normalize_catalog_semantic_title",
            "is_author_related_catalog_title",
            "is_alphabetical_catalog_title",
            "is_all_books_catalog_title",
            "is_recent_catalog_title",
            "select_preferred_registered_catalog_child",
            "catalog_book_has_downloadable_acquisition",
            "resolve_preferred_registered_catalog_token",
        }
        helper_source = "\n".join(
            ast.get_source_segment(RESOLVER_MODULE.__source_text__, node) or ""
            for node in RESOLVER_MODULE.__source_tree__.body
            if getattr(node, "name", None) in helper_names
        ).casefold()
        for forbidden in (
            "flibusta",
            "flibusta.is",
            "/opds/author/",
            "opds_base",
            "об авторе",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, helper_source)


if __name__ == "__main__":
    unittest.main()
