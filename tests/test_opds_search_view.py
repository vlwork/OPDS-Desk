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


def load_search_view_module():
    """Загружает только pure search presentation layer."""
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
        "get_catalog_ref",
        "clear_catalog_ref_registry",
        "register_catalog_refs",
        "catalog_book_to_readonly_view",
        "build_opds_search_view",
    }
    wanted_assignments = {
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
            continue
        if not isinstance(node, ast.Assign):
            continue
        assigned_names = {
            target.id for target in node.targets if isinstance(target, ast.Name)
        }
        if assigned_names & wanted_assignments:
            body.append(node)
    module = types.ModuleType("isolated_opds_search_view_test")
    sys.modules[module.__name__] = module
    module.__dict__.update(
        dataclass=dataclasses.dataclass,
        copy=copy,
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


VIEW_MODULE = load_search_view_module()


def compatibility_book(book_id="tag:catalog.example.test,2026:opaque-book"):
    return {
        "id": book_id,
        "title": "Example Book",
        "author": "First Author, Second Author",
        "authors": ["First Author", "Second Author"],
        "language": "en",
        "genres": ["fiction", "adventure"],
        "epub": True,
        "fb2": True,
        "translator": "Translator",
        "size": "1.2 MB",
        "epub_url": "https://files.example.org/book.epub",
        "fb2_url": "https://files.example.org/book.fb2",
        "acquisition_links": [
            {"href": "https://files.example.org/book.epub"}
        ],
        "cover_url": "https://images.example.org/cover.jpg",
        "thumbnail_url": "https://images.example.org/thumb.jpg",
        "web_url": "https://www.example.org/book",
        "download_url": "https://files.example.org/download",
        "related": (
            VIEW_MODULE.CatalogRef(
                source_id="private-source-id",
                url="https://private.example.org/related/catalog.xml",
                title="Related catalog",
                kind="related",
            ),
        ),
    }


def search_page(
    books=None,
    title="OPDS search results",
    next_url="",
    total_results=None,
):
    if books is None:
        books = (compatibility_book(),)
    return VIEW_MODULE.OPDSCatalogPage(
        source_id="private-source-id",
        requested_url="https://catalog.example.org/search?q=Example",
        final_url="https://redirected.example.org/results?q=Example",
        title=title,
        books=books,
        navigation=(),
        next_url=next_url,
        total_results=total_results,
    )


class OPDSSearchViewTests(unittest.TestCase):
    def setUp(self):
        VIEW_MODULE.clear_catalog_ref_registry()
        VIEW_MODULE.clear_opds_search_book_registry()

    def tearDown(self):
        VIEW_MODULE.clear_catalog_ref_registry()
        VIEW_MODULE.clear_opds_search_book_registry()

    def test_a_search_view_is_immutable(self):
        view = VIEW_MODULE.build_opds_search_view(search_page(), "Example")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            view.page = 2
        with self.assertRaises(dataclasses.FrozenInstanceError):
            view.total_results = 57
        self.assertIsInstance(view.books, tuple)

    def test_b_existing_registered_book_view_is_reused(self):
        view = VIEW_MODULE.build_opds_search_view(search_page(), "Example")
        self.assertIsInstance(view.books[0], VIEW_MODULE.RegisteredCatalogBookView)
        self.assertEqual(
            {field.name for field in dataclasses.fields(type(view.books[0]))},
            {
                "id",
                "title",
                "author",
                "authors",
                "language",
                "genres",
                "formats",
                "translator",
                "size",
                "has_cover",
                "related",
            },
        )

    def test_c_existing_book_adapter_is_called(self):
        calls = []
        expected = VIEW_MODULE.RegisteredCatalogBookView(
            id="opaque",
            title="Adapted",
            author="Author",
            authors=("Author",),
            language="",
            genres=(),
            formats=(),
            translator="",
            size="",
            has_cover=False,
        )
        original = VIEW_MODULE.catalog_book_to_readonly_view

        def adapter(book):
            calls.append(book)
            return expected

        VIEW_MODULE.catalog_book_to_readonly_view = adapter
        book = compatibility_book()
        try:
            view = VIEW_MODULE.build_opds_search_view(
                search_page(books=(book,)),
                "Example",
            )
        finally:
            VIEW_MODULE.catalog_book_to_readonly_view = original
        self.assertEqual(calls, [book])
        self.assertEqual(view.books, (expected,))

    def test_d_query_is_trimmed_at_edges(self):
        view = VIEW_MODULE.build_opds_search_view(search_page(), "  Example  ")
        self.assertEqual(view.query, "Example")

    def test_e_internal_query_spaces_are_preserved(self):
        view = VIEW_MODULE.build_opds_search_view(
            search_page(),
            "  Dune   Messiah  ",
        )
        self.assertEqual(view.query, "Dune   Messiah")

    def test_f_unicode_query_is_preserved(self):
        view = VIEW_MODULE.build_opds_search_view(
            search_page(),
            "  Мастер и Маргарита  ",
        )
        self.assertEqual(view.query, "Мастер и Маргарита")
        self.assertNotIn("%", view.query)

    def test_g_page_zero_has_no_previous(self):
        view = VIEW_MODULE.build_opds_search_view(search_page(), "Example", page=0)
        self.assertEqual(view.page, 0)
        self.assertFalse(view.has_previous)

    def test_h_positive_page_has_previous_and_uses_existing_validator(self):
        view = VIEW_MODULE.build_opds_search_view(search_page(), "Example", page=2)
        self.assertEqual(view.page, 2)
        self.assertTrue(view.has_previous)
        for invalid_page in (-1, VIEW_MODULE.MAX_CATALOG_PAGES):
            with self.subTest(page=invalid_page), self.assertRaises(ValueError):
                VIEW_MODULE.build_opds_search_view(
                    search_page(),
                    "Example",
                    page=invalid_page,
                )

    def test_i_next_url_sets_has_next_even_when_books_are_empty(self):
        view = VIEW_MODULE.build_opds_search_view(
            search_page(books=[], next_url="https://catalog.example.org/next"),
            "Example",
        )
        self.assertEqual(view.books, ())
        self.assertTrue(view.has_next)

    def test_j_missing_next_url_clears_has_next(self):
        view = VIEW_MODULE.build_opds_search_view(search_page(next_url=""), "Example")
        self.assertFalse(view.has_next)

    def test_k_next_url_is_absent_from_search_view(self):
        field_names = self.search_view_fields()
        self.assertNotIn("next_url", field_names)
        view = VIEW_MODULE.build_opds_search_view(
            search_page(next_url="https://catalog.example.org/private-next"),
            "Example",
        )
        self.assertFalse(hasattr(view, "next_url"))

    def test_l_requested_and_final_urls_are_absent(self):
        field_names = self.search_view_fields()
        self.assertNotIn("requested_url", field_names)
        self.assertNotIn("final_url", field_names)

    def test_m_source_id_is_absent(self):
        self.assertNotIn("source_id", self.search_view_fields())

    def test_n_descriptor_and_template_are_absent(self):
        field_names = self.search_view_fields()
        self.assertNotIn("descriptor", field_names)
        self.assertNotIn("template", field_names)

    def test_o_opaque_book_id_is_preserved(self):
        opaque_id = "tag:catalog.example.test,2026:opaque/book#7"
        view = VIEW_MODULE.build_opds_search_view(
            search_page(books=(compatibility_book(opaque_id),)),
            "Example",
        )
        self.assertEqual(view.books[0].id, opaque_id)

    def test_p_empty_results_build_an_empty_tuple(self):
        view = VIEW_MODULE.build_opds_search_view(
            search_page(books=[]),
            "Example",
        )
        self.assertEqual(view.books, ())

    def test_q_feed_title_is_used(self):
        view = VIEW_MODULE.build_opds_search_view(
            search_page(title="Catalog results"),
            "Example",
        )
        self.assertEqual(view.title, "Catalog results")

    def test_r_empty_title_uses_neutral_fallback(self):
        for title in ("", "   "):
            with self.subTest(title=title):
                view = VIEW_MODULE.build_opds_search_view(
                    search_page(title=title),
                    "Example",
                )
                self.assertEqual(view.title, "Результаты поиска")

    def test_s_builder_does_not_make_http_calls(self):
        def forbidden(*args, **kwargs):
            raise AssertionError("Presentation builder called a backend helper")

        names = (
            "OPDSHTTPClient",
            "load_current_opds_search_page",
            "load_cached_opds_search_page",
        )
        originals = {name: VIEW_MODULE.__dict__.get(name) for name in names}
        VIEW_MODULE.__dict__.update({name: forbidden for name in names})
        try:
            view = VIEW_MODULE.build_opds_search_view(search_page(), "Example")
        finally:
            for name, original in originals.items():
                if original is None:
                    VIEW_MODULE.__dict__.pop(name, None)
                else:
                    VIEW_MODULE.__dict__[name] = original
        self.assertEqual(view.query, "Example")

    def test_t_builder_calls_no_current_source_config_or_cache_helpers(self):
        called_names = {
            child.func.id
            for child in ast.walk(self.builder_node())
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
        }
        for forbidden in (
            "current_source_config",
            "has_configured_opds_source",
            "load_current_opds_search_page",
            "load_cached_opds_search_page",
            "resolve_current_opds_search_descriptor",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, called_names)
        self.assertIn("catalog_book_to_readonly_view", called_names)
        self.assertIn("_validate_opds_search_page_number", called_names)

    def test_u_view_layer_has_no_legacy_markers_or_unsafe_book_urls(self):
        source = "\n".join(
            ast.get_source_segment(VIEW_MODULE.__source_text__, node) or ""
            for node in VIEW_MODULE.__source_tree__.body
            if getattr(node, "name", None)
            in {"OPDSSearchView", "build_opds_search_view"}
        ).lower()
        forbidden_markers = (
            "opds" + "_base",
            "flibu" + "sta",
            "/opds/" + "search",
            "search" + "type",
            "search" + "term",
            "page" + "number",
        )
        for marker in forbidden_markers:
            with self.subTest(marker=marker):
                self.assertNotIn(marker, source)

        book_fields = {
            field.name
            for field in dataclasses.fields(VIEW_MODULE.RegisteredCatalogBookView)
        }
        for forbidden_field in (
            "acquisition_url",
            "acquisition_links",
            "download_url",
            "href",
            "epub_url",
            "fb2_url",
            "cover_url",
            "thumbnail_url",
            "web_url",
        ):
            with self.subTest(field=forbidden_field):
                self.assertNotIn(forbidden_field, book_fields)

    def test_u2_related_catalog_urls_do_not_reach_readonly_book_view(self):
        view = VIEW_MODULE.build_opds_search_view(search_page(), "Example")
        related = view.books[0].related
        self.assertIsInstance(related, tuple)
        self.assertEqual(len(related), 1)
        self.assertIsInstance(related[0], VIEW_MODULE.RegisteredCatalogRef)
        self.assertEqual(related[0].title, "Related catalog")
        self.assertEqual(related[0].kind, "related")
        self.assertTrue(related[0].token.startswith("catalog:"))
        self.assertEqual(
            {field.name for field in dataclasses.fields(related[0])},
            {"token", "title", "kind"},
        )
        self.assertNotIn(
            "private.example.org",
            repr(view.books[0]),
        )
        self.assertNotIn("private-source-id", repr(view.books[0]))
        self.assertFalse(
            any(isinstance(item, VIEW_MODULE.CatalogRef) for item in related)
        )
        stored = VIEW_MODULE.get_catalog_ref(related[0].token)
        self.assertEqual(
            stored.url,
            "https://private.example.org/related/catalog.xml",
        )
        self.assertEqual(stored.source_id, "private-source-id")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            view.books[0].related = ()

    def test_u3_multiple_related_refs_keep_provider_order(self):
        refs = (
            VIEW_MODULE.CatalogRef(
                source_id="private-source-id",
                url="https://private.example.org/related/first.xml",
                title="First related catalog",
                kind="related",
            ),
            VIEW_MODULE.CatalogRef(
                source_id="private-source-id",
                url="https://private.example.org/related/second.xml",
                title="Second related catalog",
                kind="related",
            ),
        )
        book = compatibility_book()
        book["related"] = refs
        view = VIEW_MODULE.build_opds_search_view(
            search_page(books=(book,)),
            "Example",
        )
        self.assertEqual(
            [item.title for item in view.books[0].related],
            ["First related catalog", "Second related catalog"],
        )

    def test_v_total_results_is_part_of_search_view(self):
        self.assertIn("total_results", self.search_view_fields())
        view = VIEW_MODULE.build_opds_search_view(
            search_page(total_results=57),
            "Example",
        )
        self.assertEqual(view.total_results, 57)

    def test_w_zero_total_results_is_preserved(self):
        view = VIEW_MODULE.build_opds_search_view(
            search_page(total_results=0),
            "Example",
        )
        self.assertEqual(view.total_results, 0)

    def test_x_missing_total_results_stays_none(self):
        view = VIEW_MODULE.build_opds_search_view(
            search_page(books=(compatibility_book(),)),
            "Example",
        )
        self.assertIsNone(view.total_results)

    def search_view_fields(self):
        return {
            field.name
            for field in dataclasses.fields(VIEW_MODULE.OPDSSearchView)
        }

    def builder_node(self):
        return next(
            node
            for node in VIEW_MODULE.__source_tree__.body
            if getattr(node, "name", None) == "build_opds_search_view"
        )


if __name__ == "__main__":
    unittest.main()
