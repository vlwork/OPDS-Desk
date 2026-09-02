import ast
import copy
import dataclasses
import hashlib
import ipaddress
import json
import sys
import threading
import types
import unittest
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_registry_module():
    source = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    wanted = {
        "normalize_opds_url",
        "CatalogRef",
        "RegisteredCatalogRef",
        "RegisteredCatalogBookView",
        "OPDSSearchView",
        "OPDSCatalogPage",
        "normalize_opds_search_query",
        "make_opds_search_book_token",
        "register_opds_search_book",
        "get_opds_search_book",
        "resolve_opds_search_book",
        "clear_opds_search_book_registry",
        "_validate_opds_search_page_number",
        "make_catalog_ref_token",
        "register_catalog_ref",
        "register_catalog_refs",
        "catalog_book_to_readonly_view",
        "build_opds_search_view",
    }
    assignments = {
        "MAX_CATALOG_REF_REGISTRY",
        "catalog_ref_registry",
        "catalog_ref_registry_lock",
        "MAX_OPDS_SEARCH_BOOK_REGISTRY",
        "opds_search_book_registry",
        "opds_search_book_registry_lock",
    }
    body = []
    for node in tree.body:
        if getattr(node, "name", None) in wanted:
            body.append(node)
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in assignments
            for target in node.targets
        ):
            body.append(node)

    module = types.ModuleType("isolated_opds_search_book_registry_test")
    sys.modules[module.__name__] = module
    module.__dict__.update(
        copy=copy,
        dataclass=dataclasses.dataclass,
        hashlib=hashlib,
        ipaddress=ipaddress,
        json=json,
        threading=threading,
        urlsplit=urlsplit,
        urlunsplit=urlunsplit,
        MAX_CATALOG_PAGES=5,
    )
    exec(
        compile(ast.Module(body=body, type_ignores=[]), "app.py", "exec"),
        module.__dict__,
    )
    module.__source_text__ = source
    module.__source_tree__ = tree
    return module


REGISTRY = load_registry_module()


def full_book(book_id="opaque-id", source_id="wrong-source", title="Example"):
    return {
        "source_id": source_id,
        "id": book_id,
        "title": title,
        "author": "Writer",
        "authors": ["Writer"],
        "language": "en",
        "genres": ["fiction"],
        "categories": ["novel"],
        "epub": True,
        "fb2": True,
        "epub_url": "https://files.example/book.epub",
        "fb2_url": "https://files.example/book.fb2",
        "epub_mime_type": "application/epub+zip",
        "fb2_mime_type": "application/x-fictionbook+xml",
        "acquisition_links": [
            {
                "href": "https://files.example/book.epub",
                "type": "application/epub+zip",
            }
        ],
        "cover_url": "https://files.example/cover.jpg",
        "thumbnail_url": "https://files.example/thumb.jpg",
        "web_url": "https://catalog.example/books/opaque-id",
        "related": (),
        "unknown": {"nested": ["preserved"]},
    }


class OPDSSearchBookRegistryTests(unittest.TestCase):
    def setUp(self):
        self.original_limit = REGISTRY.MAX_OPDS_SEARCH_BOOK_REGISTRY
        REGISTRY.MAX_OPDS_SEARCH_BOOK_REGISTRY = 8192
        REGISTRY.clear_opds_search_book_registry()
        REGISTRY.catalog_ref_registry.clear()

    def tearDown(self):
        REGISTRY.MAX_OPDS_SEARCH_BOOK_REGISTRY = self.original_limit
        REGISTRY.clear_opds_search_book_registry()
        REGISTRY.catalog_ref_registry.clear()

    def test_a_token_is_deterministic_private_and_uses_existing_query_normalization(self):
        source_id = "private-source-a"
        query = "Private Dune Query"
        source_item_id = "book?id=10&edition=2"
        first = REGISTRY.make_opds_search_book_token(
            source_id,
            f"  {query}  ",
            source_item_id,
        )
        second = REGISTRY.make_opds_search_book_token(
            source_id,
            query,
            source_item_id,
        )
        self.assertEqual(first, second)
        self.assertRegex(first, r"^search-book:[0-9a-f]{64}$")
        for private_value in (source_id, query, source_item_id, "edition=2"):
            self.assertNotIn(private_value, first)
        self.assertNotEqual(
            REGISTRY.make_opds_search_book_token(
                source_id,
                "Dune  Messiah",
                source_item_id,
            ),
            REGISTRY.make_opds_search_book_token(
                source_id,
                "Dune Messiah",
                source_item_id,
            ),
        )

    def test_b_registration_enforces_source_authority_and_deep_copy_isolation(self):
        original = full_book()
        token = REGISTRY.register_opds_search_book(
            "source-a",
            "Dune",
            original,
        )
        original["source_id"] = "mutated-source"
        original["epub_url"] = "https://attacker.example/changed.epub"
        original["unknown"]["nested"].append("mutated")

        first = REGISTRY.get_opds_search_book(token)
        self.assertEqual(first["source_id"], "source-a")
        self.assertEqual(first["id"], "opaque-id")
        self.assertEqual(first["epub_url"], "https://files.example/book.epub")
        self.assertEqual(first["fb2_url"], "https://files.example/book.fb2")
        self.assertEqual(first["epub_mime_type"], "application/epub+zip")
        self.assertEqual(
            first["fb2_mime_type"],
            "application/x-fictionbook+xml",
        )
        self.assertEqual(first["unknown"], {"nested": ["preserved"]})

        first["acquisition_links"][0]["href"] = "changed"
        first["unknown"]["nested"].append("changed")
        second = REGISTRY.resolve_opds_search_book(
            "source-a",
            "Dune",
            "opaque-id",
        )
        self.assertEqual(
            second["acquisition_links"][0]["href"],
            "https://files.example/book.epub",
        )
        self.assertEqual(second["unknown"], {"nested": ["preserved"]})

    def test_c_query_context_is_isolated_but_edge_whitespace_is_normalized(self):
        REGISTRY.register_opds_search_book("source-a", "Dune", full_book())
        self.assertIsNotNone(
            REGISTRY.resolve_opds_search_book(
                "source-a",
                "  Dune  ",
                "opaque-id",
            )
        )
        self.assertIsNone(
            REGISTRY.resolve_opds_search_book(
                "source-a",
                "Foundation",
                "opaque-id",
            )
        )

    def test_d_sources_are_isolated_for_the_same_query_and_item_id(self):
        token_a = REGISTRY.register_opds_search_book(
            "source-a",
            "query",
            full_book(title="From A"),
        )
        token_b = REGISTRY.register_opds_search_book(
            "source-b",
            "query",
            full_book(title="From B"),
        )
        self.assertNotEqual(token_a, token_b)
        self.assertEqual(
            REGISTRY.resolve_opds_search_book(
                "source-a", "query", "opaque-id"
            )["title"],
            "From A",
        )
        self.assertEqual(
            REGISTRY.resolve_opds_search_book(
                "source-b", "query", "opaque-id"
            )["title"],
            "From B",
        )

    def test_e_opaque_ids_are_exact_and_need_no_numeric_conversion(self):
        item_ids = (
            "urn:uuid:0d508a30-073f-4028-b522-592a2acbdb98",
            "tag:catalog.example,2026:item",
            "book?id=10&edition=2",
        )
        for item_id in item_ids:
            with self.subTest(item_id=item_id):
                token = REGISTRY.register_opds_search_book(
                    "source-a",
                    "query",
                    full_book(book_id=item_id),
                )
                self.assertEqual(
                    token,
                    REGISTRY.make_opds_search_book_token(
                        "source-a",
                        "query",
                        item_id,
                    ),
                )
                self.assertEqual(
                    REGISTRY.resolve_opds_search_book(
                        "source-a",
                        "query",
                        item_id,
                    )["id"],
                    item_id,
                )

    def test_f_reregister_replaces_snapshot_and_keeps_token(self):
        first = full_book()
        first["epub_url"] = "https://files.example/first.epub"
        token_a = REGISTRY.register_opds_search_book(
            "source-a", "query", first
        )
        second = full_book()
        second["epub_url"] = "https://files.example/second.epub"
        token_b = REGISTRY.register_opds_search_book(
            "source-a", "query", second
        )
        self.assertEqual(token_a, token_b)
        self.assertEqual(
            REGISTRY.get_opds_search_book(token_a)["epub_url"],
            "https://files.example/second.epub",
        )
        self.assertEqual(len(REGISTRY.opds_search_book_registry), 1)

    def test_g_registry_evicts_oldest_identity_at_configured_bound(self):
        REGISTRY.MAX_OPDS_SEARCH_BOOK_REGISTRY = 2
        tokens = [
            REGISTRY.register_opds_search_book(
                "source-a",
                "query",
                full_book(book_id=f"item-{index}"),
            )
            for index in range(3)
        ]
        self.assertIsNone(REGISTRY.get_opds_search_book(tokens[0]))
        self.assertIsNotNone(REGISTRY.get_opds_search_book(tokens[1]))
        self.assertIsNotNone(REGISTRY.get_opds_search_book(tokens[2]))
        self.assertEqual(len(REGISTRY.opds_search_book_registry), 2)

    def test_h_invalid_registration_inputs_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "source_id должен быть строкой"):
            REGISTRY.make_opds_search_book_token(123, "query", "item")
        with self.assertRaisesRegex(ValueError, "Ожидается словарь книги"):
            REGISTRY.register_opds_search_book("source-a", "query", [])
        with self.assertRaisesRegex(ValueError, "source item ID"):
            REGISTRY.register_opds_search_book(
                "source-a",
                "query",
                {"id": ""},
            )
        self.assertIsNone(REGISTRY.get_opds_search_book("search-book:unknown"))

    def test_i_resolver_defensively_rejects_tampered_snapshot_identity(self):
        token = REGISTRY.register_opds_search_book(
            "source-a", "query", full_book()
        )
        with REGISTRY.opds_search_book_registry_lock:
            REGISTRY.opds_search_book_registry[token]["source_id"] = "source-b"
        self.assertIsNone(
            REGISTRY.resolve_opds_search_book(
                "source-a",
                "query",
                "opaque-id",
            )
        )

    def test_j_build_view_registers_full_snapshot_but_exposes_readonly_book(self):
        source_book = full_book()
        page = REGISTRY.OPDSCatalogPage(
            source_id="source-a",
            requested_url="https://catalog.example/search?q=query",
            final_url="https://catalog.example/search?q=query",
            title="Results",
            books=(source_book,),
            navigation=(),
            next_url="",
        )
        view = REGISTRY.build_opds_search_view(page, "  query  ")
        readonly_book = view.books[0]
        self.assertEqual(readonly_book.id, "opaque-id")
        for private_field in (
            "epub_url",
            "fb2_url",
            "epub_mime_type",
            "fb2_mime_type",
            "acquisition_links",
        ):
            self.assertFalse(hasattr(readonly_book, private_field))
        snapshot = REGISTRY.resolve_opds_search_book(
            "source-a",
            "query",
            "opaque-id",
        )
        self.assertEqual(snapshot["source_id"], "source-a")
        self.assertEqual(snapshot["epub_url"], source_book["epub_url"])
        self.assertEqual(snapshot["fb2_url"], source_book["fb2_url"])
        self.assertEqual(snapshot["acquisition_links"], source_book["acquisition_links"])

    def test_k_registry_helpers_have_no_network_cache_or_runtime_config_lookup(self):
        helper_names = {
            "make_opds_search_book_token",
            "register_opds_search_book",
            "get_opds_search_book",
            "resolve_opds_search_book",
            "clear_opds_search_book_registry",
        }
        source = "\n".join(
            ast.get_source_segment(REGISTRY.__source_text__, node) or ""
            for node in REGISTRY.__source_tree__.body
            if getattr(node, "name", None) in helper_names
        )
        for forbidden in (
            "current_source_config",
            "current_source_id",
            "APP_CONFIG",
            "OPDSHTTPClient",
            "requests",
            "opds_search_page_cache",
            "_cached_opds_search_page",
            "load_cached_opds_search_page",
            "load_current_opds_search_page",
            "load_opds_catalog_page",
            "load_opds_search_page",
            "queue_add_book",
            "int(",
            "isdigit",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)
        self.assertIsNot(
            REGISTRY.opds_search_book_registry,
            REGISTRY.catalog_ref_registry,
        )


if __name__ == "__main__":
    unittest.main()
