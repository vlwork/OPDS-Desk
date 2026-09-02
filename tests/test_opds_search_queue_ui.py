import ast
import dataclasses
import hashlib
import html
import re
import sys
import types
import unittest
from pathlib import Path

import requests
from flask import Flask, render_template_string, request, session, url_for


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_ui_module():
    source = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    wanted = {
        "CatalogRef",
        "RegisteredCatalogRef",
        "RegisteredCatalogBookView",
        "OPDSSearchView",
        "OPDSCatalogPage",
        "source_namespace",
        "_opaque_key_part",
        "catalog_selection_storage_key",
        "normalize_opds_search_query",
        "_validate_opds_search_page_number",
        "current_source_id",
        "catalog_selection_clear_token",
        "mark_catalog_selection_clear",
        "catalog_selection_clear_pending",
        "opds_search_page",
    }
    body = []
    for node in tree.body:
        if getattr(node, "name", None) in wanted:
            body.append(node)
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "OPDS_SEARCH_HTML"
            for target in node.targets
        ):
            body.append(node)

    module = types.ModuleType("isolated_opds_search_queue_ui_test")
    sys.modules[module.__name__] = module
    module.__dict__.update(
        RequestException=requests.RequestException,
        app=Flask(module.__name__),
        dataclass=dataclasses.dataclass,
        hashlib=hashlib,
        request=request,
        render_template_string=render_template_string,
        session=session,
        url_for=url_for,
        MAX_CATALOG_PAGES=5,
        load_current_opds_search_page=None,
        build_opds_search_view=None,
    )
    exec(
        compile(ast.Module(body=body, type_ignores=[]), "app.py", "exec"),
        module.__dict__,
    )
    module.app.add_url_rule(
        "/",
        endpoint="index",
        view_func=lambda: "index",
    )
    module.app.add_url_rule(
        "/settings/opds",
        endpoint="opds_settings_page",
        view_func=lambda: "settings",
    )
    module.app.add_url_rule(
        "/catalog/opds/<token>",
        endpoint="registered_catalog_page",
        view_func=lambda token: token,
    )
    module.app.add_url_rule(
        "/queue",
        endpoint="queue_page",
        view_func=lambda: "queue",
    )
    module.app.add_url_rule(
        "/search/opds/queue",
        endpoint="opds_search_queue_add",
        view_func=lambda: "queue",
        methods=["POST"],
    )
    module.app.secret_key = "test-secret"
    module.app.testing = True
    module.__source_text__ = source
    return module


UI = load_ui_module()


def readonly_book(book_id="urn:uuid:opaque-book"):
    return UI.RegisteredCatalogBookView(
        id=book_id,
        title="Example",
        author="Writer",
        authors=("Writer",),
        language="en",
        genres=("fiction",),
        formats=("EPUB", "FB2"),
        translator="",
        size="",
        has_cover=False,
    )


class OPDSSearchQueueUITests(unittest.TestCase):
    def setUp(self):
        self.source = types.SimpleNamespace(
            source_id="source-a",
            root_url="https://catalog.example/opds",
        )
        self.books = (readonly_book(),)
        UI.current_source_config = lambda: self.source
        UI.load_current_opds_search_page = self.load_page
        UI.build_opds_search_view = self.build_view
        self.client = UI.app.test_client()

    def load_page(self, query, page=0, force=False):
        return UI.OPDSCatalogPage(
            source_id=self.source.source_id,
            requested_url="https://catalog.example/search",
            final_url="https://catalog.example/search",
            title="Results",
            books=(),
            navigation=(),
            next_url="",
        )

    def build_view(self, search_page, query, page=0):
        return UI.OPDSSearchView(
            query=query,
            books=self.books,
            page=page,
            has_previous=page > 0,
            has_next=False,
            title="Results",
        )

    def get(self, query="query", page=None):
        params = {"q": query}
        if page is not None:
            params["page"] = str(page)
        return self.client.get("/search/opds", query_string=params)

    def clear_token(self, query="query"):
        return "clear:" + UI.catalog_selection_storage_key(
            self.source.source_id,
            "search",
            UI.normalize_opds_search_query(query),
        )

    def seed_clear_marker(self, query="query"):
        with self.client.session_transaction() as flask_session:
            flask_session["catalog_selections_to_clear"] = {
                self.clear_token(query): True
            }

    def pending_markers(self):
        with self.client.session_transaction() as flask_session:
            return dict(flask_session.get("catalog_selections_to_clear", {}))

    def template_source(self):
        return UI.OPDS_SEARCH_HTML

    def test_a_form_posts_only_query_and_format_controls(self):
        body = html.unescape(self.get().get_data(as_text=True))
        self.assertIn(
            '<form id="opdsQueueForm" class="selection-toolbar" '
            'method="post" action="/search/opds/queue">',
            body,
        )
        self.assertIn('<input type="hidden" name="q" value="query">', body)
        for mode in ("auto", "epub", "fb2"):
            self.assertIn(f'name="format_mode" value="{mode}"', body)
        self.assertRegex(
            body,
            r'name="format_mode" value="auto" checked',
        )
        self.assertIn("Добавить выбранные в очередь", body)
        checkbox = re.search(r'<input class="book-check"[^>]*>', body).group(0)
        self.assertNotIn("name=", checkbox)
        markup_fields = re.findall(r'<input[^>]+name="([^"]+)"', body)
        self.assertEqual(set(markup_fields), {"q", "format_mode"})

    def test_b_javascript_appends_full_stored_selection_as_opaque_ids(self):
        template = self.template_source()
        for required in (
            "appendStoredSelectionToOpdsQueueForm",
            "selectedBookIds.forEach",
            "input.type = 'hidden'",
            "input.name = 'book_id'",
            "input.value = String(id)",
            "input.className = 'stored-selection-input'",
            "form.appendChild(input)",
            "form.querySelectorAll('.stored-selection-input')",
        ):
            self.assertIn(required, template)
        for forbidden in (
            "epub_url",
            "fb2_url",
            "epub_mime_type",
            "fb2_mime_type",
            "acquisition_links",
            "source_id",
        ):
            self.assertNotIn(forbidden, template)

    def test_c_empty_selection_disables_and_prevents_submit_without_clearing_storage(self):
        template = self.template_source()
        self.assertIn('id="opdsQueueSubmit" type="submit" disabled', template)
        self.assertIn("submitButton.disabled = selectedBookIds.size === 0", template)
        self.assertIn("if (selectedBookIds.size === 0)", template)
        self.assertIn("event.preventDefault()", template)
        submit_handler_match = re.search(
            r"opdsQueueForm\.addEventListener\('submit', \(event\) => \{"
            r"(?P<body>.*?)\n\s*\}\);",
            template,
            re.DOTALL,
        )
        self.assertIsNotNone(submit_handler_match)
        submit_handler = submit_handler_match.group("body")
        self.assertNotIn("selectedBookIds.clear", submit_handler)
        self.assertNotIn("sessionStorage.removeItem", submit_handler)

    def test_d_successful_get_renders_clear_flag_then_consumes_marker(self):
        self.seed_clear_marker("  query  ")
        marker_seen_during_render = []
        original = UI.render_template_string

        def capture(template, **context):
            marker_seen_during_render.append(
                UI.catalog_selection_clear_pending("search", "query")
            )
            return original(template, **context)

        UI.render_template_string = capture
        try:
            response = self.get("query")
        finally:
            UI.render_template_string = original
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(marker_seen_during_render, [True])
        self.assertIn("if (true)", body)
        self.assertIn("selectedBookIds.clear()", body)
        self.assertEqual(self.pending_markers(), {})

    def test_e_empty_results_still_run_clear_script_and_consume_marker(self):
        self.books = ()
        self.seed_clear_marker()
        body = self.get().get_data(as_text=True)
        self.assertNotIn('id="opdsQueueForm"', body)
        self.assertNotIn('id="selectedCount"', body)
        self.assertIn("const selectedBookIds = loadStoredSelection()", body)
        self.assertIn("if (true)", body)
        self.assertIn("selectedBookIds.clear()", body)
        self.assertEqual(self.pending_markers(), {})

    def test_f_flash_is_visible_without_clear_marker_for_stale_redirect(self):
        message = "Выбранные книги устарели. Обновите результаты поиска."
        with self.client.session_transaction() as flask_session:
            flask_session["_flashes"] = [("message", message)]
        body = self.get().get_data(as_text=True)
        self.assertIn(message, body)
        self.assertIn("if (false)", body)
        self.assertEqual(self.pending_markers(), {})

    def test_g_error_render_does_not_consume_pending_clear_marker(self):
        self.seed_clear_marker()
        UI.load_current_opds_search_page = lambda *args, **kwargs: (
            (_ for _ in ()).throw(ValueError("OPDS-источник не настроен"))
        )
        response = self.get()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.pending_markers(), {self.clear_token(): True})

    def test_h_multi_page_gets_share_the_same_storage_key(self):
        first = self.get(page=0).get_data(as_text=True)
        second = self.get(page=2).get_data(as_text=True)
        expected_key = UI.catalog_selection_storage_key(
            "source-a",
            "search",
            "query",
        )
        first_line = next(
            line for line in first.splitlines() if "const selectionStorageKey" in line
        )
        second_line = next(
            line for line in second.splitlines() if "const selectionStorageKey" in line
        )
        self.assertEqual(first_line, second_line)
        self.assertIn(expected_key, first_line)
        self.assertNotIn("page", first_line)

    def test_i_search_navigation_uses_separate_loading_classes(self):
        template = self.template_source()
        self.assertEqual(template.count('class="opds-page-link"'), 2)
        self.assertEqual(template.count('class="opds-catalog-link"'), 1)
        self.assertIn(
            '<a href="{{ url_for(\'queue_page\') }}">Очередь</a>',
            template,
        )
        self.assertNotIn(
            'class="opds-page-link" href="{{ url_for(\'queue_page\') }}"',
            template,
        )
        self.assertNotIn(
            'class="opds-catalog-link" href="{{ url_for(\'queue_page\') }}"',
            template,
        )
        self.assertIn(
            '<a class="opds-catalog-link" href="{{ url_for(\'registered_catalog_page\', token=related.token) }}">',
            template,
        )
        self.assertNotIn(
            'class="opds-page-link" href="{{ url_for(\'registered_catalog_page\'',
            template,
        )
        for required in (
            'id="opdsPageLoadingOverlay"',
            'aria-hidden="true"',
            'role="status"',
            'aria-live="polite"',
            "Загрузка страницы...",
            "Загрузка каталога...",
            "Получение данных из OPDS-каталога",
        ):
            with self.subTest(required=required):
                self.assertIn(required, template)

    def test_j_search_loading_allows_first_click_and_resets_on_pageshow(self):
        template = self.template_source()
        loading_script = template.split(
            '<script id="opdsPageLoadingScript">',
            1,
        )[1].split("</script>", 1)[0]
        handler_match = re.search(
            r"function beginOpdsLoading\(event, title, note\) \{(?P<body>.*?)^\}",
            loading_script,
            re.DOTALL | re.MULTILINE,
        )
        self.assertIsNotNone(handler_match)
        handler = handler_match.group("body")
        self.assertEqual(handler.count("event.preventDefault()"), 1)
        self.assertLess(
            handler.index("if (opdsPageLoading)"),
            handler.index("event.preventDefault()"),
        )
        self.assertLess(
            handler.index("event.preventDefault()"),
            handler.index("opdsPageLoading = true"),
        )
        self.assertIn(
            "link.addEventListener('click', showOpdsPageLoading)",
            loading_script,
        )
        self.assertIn(
            "link.addEventListener('click', showOpdsCatalogLoading)",
            loading_script,
        )
        self.assertIn("'Загрузка страницы...'", loading_script)
        self.assertIn("'Загрузка каталога...'", loading_script)
        self.assertIn(
            "window.addEventListener('pageshow', resetOpdsPageLoading)",
            loading_script,
        )
        self.assertIn("link.classList.remove('loading-disabled')", loading_script)
        self.assertIn("link.removeAttribute('aria-disabled')", loading_script)

    def test_k_page_loading_is_selection_independent_and_neutral(self):
        template = self.template_source()
        loading_script = template.split(
            '<script id="opdsPageLoadingScript">',
            1,
        )[1].split("</script>", 1)[0]
        for forbidden in (
            "selectedBookIds",
            "sessionStorage",
            "clear_selection",
            "book_id",
            "format_mode",
            "opdsQueueForm",
            "epub_url",
            "fb2_url",
            "acquisition_links",
            "source_id",
            "Flibusta",
            "Флибуста",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, loading_script)


if __name__ == "__main__":
    unittest.main()
