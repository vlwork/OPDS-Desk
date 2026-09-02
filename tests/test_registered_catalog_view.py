import ast
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


def load_view_module():
    """Загружает только read-only view layer без runtime приложения."""
    source = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    wanted_definitions = {
        "SourceConfig",
        "normalize_app_config",
        "source_config_from_app_config",
        "normalize_opds_url",
        "CatalogRef",
        "RegisteredCatalogRef",
        "RegisteredCatalogBookView",
        "RegisteredCatalogView",
        "make_catalog_ref_token",
        "register_catalog_ref",
        "get_catalog_ref",
        "get_current_catalog_ref",
        "clear_catalog_ref_registry",
        "register_catalog_refs",
        "current_source_config",
        "catalog_book_to_readonly_view",
        "build_registered_catalog_view",
    }
    wanted_assignments = {
        "CONFIG_VERSION",
        "MAX_CATALOG_REF_REGISTRY",
        "catalog_ref_registry",
        "catalog_ref_registry_lock",
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

    module = types.ModuleType("isolated_registered_catalog_view_test")
    sys.modules[module.__name__] = module
    module.__dict__.update(
        dataclass=dataclasses.dataclass,
        hashlib=hashlib,
        ipaddress=ipaddress,
        json=json,
        threading=threading,
        urlsplit=urlsplit,
        urlunsplit=urlunsplit,
        DEFAULT_DESTINATION="test-default-library",
        APP_CONFIG={},
        load_registered_catalog_page=None,
        collect_registered_catalog=None,
    )
    exec(
        compile(ast.Module(body=body, type_ignores=[]), "app.py", "exec"),
        module.__dict__,
    )
    module.__source_text__ = source
    module.__source_tree__ = tree
    return module


VIEW_MODULE = load_view_module()


def compatibility_book(book_id="urn:book:one"):
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
    }


class RegisteredCatalogViewTests(unittest.TestCase):
    def setUp(self):
        VIEW_MODULE.clear_catalog_ref_registry()
        VIEW_MODULE.APP_CONFIG = {
            "config_version": VIEW_MODULE.CONFIG_VERSION,
            "opds_url": "",
            "source_id": "",
            "source_name": "",
            "library_path": "X:/Books",
            "setup_complete": True,
        }
        VIEW_MODULE.load_registered_catalog_page = None
        VIEW_MODULE.collect_registered_catalog = None

    def register_current(self, source_id="source-a", title="Configured OPDS"):
        root_url = "https://catalog.example.org/root.xml"
        VIEW_MODULE.APP_CONFIG.update(
            opds_url=root_url,
            source_id=source_id,
            source_name=title,
        )
        ref = VIEW_MODULE.CatalogRef(
            source_id=source_id,
            url=root_url,
            title=title,
            kind="navigation",
        )
        return ref, VIEW_MODULE.register_catalog_ref(ref)

    def page_result(self, page=0, has_next=False, navigation=()):
        return {
            "title": "Page title",
            "books": [compatibility_book()],
            "page": page,
            "has_next": has_next,
            "navigation": navigation,
        }

    def test_a_page_mode_preserves_page_state(self):
        _, token = self.register_current()
        calls = []

        def load_page(call_token, page=0, client=None):
            calls.append((call_token, page, client))
            return self.page_result(page=1, has_next=True)

        VIEW_MODULE.load_registered_catalog_page = load_page
        view = VIEW_MODULE.build_registered_catalog_view(
            token, page=1, client="fake-client"
        )
        self.assertEqual(calls, [(token, 1, "fake-client")])
        self.assertEqual(view.title, "Page title")
        self.assertEqual(view.page, 1)
        self.assertTrue(view.has_previous)
        self.assertTrue(view.has_next)
        self.assertFalse(view.view_all)

    def test_b_page_zero_has_no_previous(self):
        _, token = self.register_current()
        VIEW_MODULE.load_registered_catalog_page = (
            lambda *args, **kwargs: self.page_result(page=0, has_next=True)
        )
        view = VIEW_MODULE.build_registered_catalog_view(token)
        self.assertFalse(view.has_previous)
        self.assertEqual(view.pages, 1)

    def test_c_page_two_has_previous(self):
        _, token = self.register_current()
        VIEW_MODULE.load_registered_catalog_page = (
            lambda *args, **kwargs: self.page_result(page=2, has_next=False)
        )
        view = VIEW_MODULE.build_registered_catalog_view(token, page=2)
        self.assertEqual(view.page, 2)
        self.assertEqual(view.pages, 3)
        self.assertTrue(view.has_previous)

    def test_d_view_all_uses_only_full_collector(self):
        _, token = self.register_current()
        page_calls = []
        full_calls = []
        VIEW_MODULE.load_registered_catalog_page = (
            lambda *args, **kwargs: page_calls.append((args, kwargs))
        )

        def collect(call_token, client=None):
            full_calls.append((call_token, client))
            return {
                "title": "Full title",
                "books": [compatibility_book()],
                "pages": 7,
            }

        VIEW_MODULE.collect_registered_catalog = collect
        view = VIEW_MODULE.build_registered_catalog_view(
            token, page=4, view_all=True, client="fake-client"
        )
        self.assertEqual(full_calls, [(token, "fake-client")])
        self.assertEqual(page_calls, [])
        self.assertEqual(view.page, 0)
        self.assertEqual(view.pages, 7)
        self.assertFalse(view.has_previous)
        self.assertFalse(view.has_next)
        self.assertTrue(view.view_all)
        self.assertEqual(view.navigation, ())

    def test_e_book_metadata_and_opaque_ids_are_preserved(self):
        identifiers = (
            "550e8400-e29b-41d4-a716-446655440000",
            "https://ids.example.org/books/two",
            "sha256:" + "a" * 64,
        )
        views = tuple(
            VIEW_MODULE.catalog_book_to_readonly_view(
                compatibility_book(identifier)
            )
            for identifier in identifiers
        )
        self.assertEqual(tuple(view.id for view in views), identifiers)
        first = views[0]
        self.assertEqual(first.title, "Example Book")
        self.assertEqual(first.author, "First Author, Second Author")
        self.assertEqual(first.authors, ("First Author", "Second Author"))
        self.assertEqual(first.language, "en")
        self.assertEqual(first.genres, ("fiction", "adventure"))

    def test_f_formats_are_stable_names_without_links(self):
        view = VIEW_MODULE.catalog_book_to_readonly_view(compatibility_book())
        self.assertEqual(view.formats, ("EPUB", "FB2"))
        self.assertTrue(view.has_cover)

    def test_g_book_schema_excludes_external_and_acquisition_urls(self):
        field_names = {
            field.name
            for field in dataclasses.fields(VIEW_MODULE.RegisteredCatalogBookView)
        }
        for forbidden in (
            "epub_url",
            "fb2_url",
            "acquisition_links",
            "cover_url",
            "thumbnail_url",
            "web_url",
        ):
            self.assertNotIn(forbidden, field_names)

    def test_h_navigation_contains_only_registered_safe_fields(self):
        _, token = self.register_current()
        navigation = VIEW_MODULE.RegisteredCatalogRef(
            token="catalog:" + "b" * 64,
            title="Subcatalog",
            kind="navigation",
        )
        VIEW_MODULE.load_registered_catalog_page = (
            lambda *args, **kwargs: self.page_result(navigation=(navigation,))
        )
        view = VIEW_MODULE.build_registered_catalog_view(token)
        self.assertEqual(view.navigation, (navigation,))
        self.assertEqual(
            {field.name for field in dataclasses.fields(type(navigation))},
            {"token", "title", "kind"},
        )

    def test_i_view_models_have_no_source_url_fields(self):
        view_fields = {
            field.name for field in dataclasses.fields(VIEW_MODULE.RegisteredCatalogView)
        }
        book_fields = {
            field.name
            for field in dataclasses.fields(VIEW_MODULE.RegisteredCatalogBookView)
        }
        self.assertTrue(all("url" not in name for name in view_fields))
        self.assertTrue(all("url" not in name for name in book_fields))

    def test_j_unknown_and_stale_tokens_are_rejected(self):
        self.register_current()
        with self.assertRaisesRegex(ValueError, "недоступен или устарел"):
            VIEW_MODULE.build_registered_catalog_view(
                "catalog:" + "f" * 64
            )

        _, token = self.register_current("source-a")
        VIEW_MODULE.APP_CONFIG.update(
            opds_url="https://other.example.org/root.xml",
            source_id="source-b",
        )
        with self.assertRaisesRegex(ValueError, "недоступен или устарел"):
            VIEW_MODULE.build_registered_catalog_view(token)

    def test_k_builders_do_not_mutate_app_config(self):
        _, token = self.register_current()
        VIEW_MODULE.APP_CONFIG["custom_test"] = 123
        snapshot = dict(VIEW_MODULE.APP_CONFIG)
        VIEW_MODULE.load_registered_catalog_page = (
            lambda *args, **kwargs: self.page_result()
        )
        VIEW_MODULE.build_registered_catalog_view(token)
        self.assertEqual(VIEW_MODULE.APP_CONFIG, snapshot)

    def test_l_numeric_like_values_are_only_stringified(self):
        view = VIEW_MODULE.catalog_book_to_readonly_view(
            compatibility_book(123)
        )
        self.assertEqual(view.id, "123")

    def test_m_legacy_renderer_and_template_do_not_use_new_view(self):
        render_node = next(
            node
            for node in VIEW_MODULE.__source_tree__.body
            if getattr(node, "name", None) == "render_catalog"
        )
        called_names = {
            child.func.id
            for child in ast.walk(render_node)
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
        }
        self.assertNotIn("build_registered_catalog_view", called_names)
        catalog_template = next(
            node
            for node in VIEW_MODULE.__source_tree__.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "CATALOG_HTML"
                for target in node.targets
            )
        )
        template_source = (
            ast.get_source_segment(VIEW_MODULE.__source_text__, catalog_template) or ""
        )
        self.assertNotIn("RegisteredCatalogView", template_source)

    def test_n_readonly_layer_has_no_backend_capabilities(self):
        wanted = {
            "RegisteredCatalogBookView",
            "RegisteredCatalogView",
            "catalog_book_to_readonly_view",
            "build_registered_catalog_view",
        }
        layer_source = "\n".join(
            ast.get_source_segment(VIEW_MODULE.__source_text__, node) or ""
            for node in VIEW_MODULE.__source_tree__.body
            if getattr(node, "name", None) in wanted
        ).lower()
        for marker in (
            "opds_base",
            "flibusta",
            "queue",
            "queue_active_book_ids",
            "bulk",
            "download",
            "save_epub",
            "save_fb2",
            "sessionstorage",
        ):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, layer_source)

    def test_o_book_view_registers_only_safe_related_refs(self):
        refs = (
            VIEW_MODULE.CatalogRef(
                source_id="source-a",
                url="https://related.example.test/first.xml",
                title="First related catalog",
                kind="related",
            ),
            VIEW_MODULE.CatalogRef(
                source_id="source-a",
                url="https://related.example.test/second.xml",
                title="Second related catalog",
                kind="related",
            ),
        )
        book = compatibility_book()
        book["related"] = refs
        view = VIEW_MODULE.catalog_book_to_readonly_view(book)
        self.assertEqual(
            [item.title for item in view.related],
            ["First related catalog", "Second related catalog"],
        )
        self.assertTrue(
            all(
                isinstance(item, VIEW_MODULE.RegisteredCatalogRef)
                for item in view.related
            )
        )
        self.assertNotIn("related.example.test", repr(view))
        self.assertFalse(any(isinstance(item, VIEW_MODULE.CatalogRef) for item in view.related))


if __name__ == "__main__":
    unittest.main()
