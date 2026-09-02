import ast
import dataclasses
import hashlib
import html
import sys
import types
import unittest
from pathlib import Path

import requests
from flask import Flask, redirect, render_template_string, request, session, url_for


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_search_route_module():
    """Загружает только neutral OPDS search route и его safe модели."""
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
        "search_return",
        "context_return",
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

    module = types.ModuleType("isolated_opds_search_route_test")
    sys.modules[module.__name__] = module
    module.__dict__.update(
        RequestException=requests.RequestException,
        app=Flask(module.__name__),
        dataclass=dataclasses.dataclass,
        hashlib=hashlib,
        redirect=redirect,
        request=request,
        render_template_string=render_template_string,
        session=session,
        MAX_CATALOG_PAGES=5,
        load_current_opds_search_page=None,
        build_opds_search_view=None,
        catalog_selection_clear_pending=lambda *args, **kwargs: False,
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
        "/queue",
        endpoint="queue_page",
        view_func=lambda: "queue",
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
        "/search/opds/queue",
        endpoint="opds_search_queue_add",
        view_func=lambda: "queue",
        methods=["POST"],
    )
    module.app.testing = True
    module.app.secret_key = "opds-search-route-test"
    module.__source_text__ = source
    module.__source_tree__ = tree
    return module


ROUTE_MODULE = load_search_route_module()


def book_view(
    book_id="tag:catalog.example.test,2026:opaque-book",
    title="Example Book",
    author="Example Author",
    related=(),
):
    return ROUTE_MODULE.RegisteredCatalogBookView(
        id=book_id,
        title=title,
        author=author,
        authors=(author,),
        language="en",
        genres=("fiction", "adventure"),
        formats=("EPUB", "FB2"),
        translator="Translator",
        size="1.2 MB",
        has_cover=True,
        related=related,
    )


def search_view(
    query="Dune",
    page=0,
    books=None,
    has_previous=False,
    has_next=False,
    title="Search results",
    total_results=None,
):
    if books is None:
        books = (book_view(),)
    return ROUTE_MODULE.OPDSSearchView(
        query=query,
        books=books,
        page=page,
        has_previous=has_previous,
        has_next=has_next,
        title=title,
        total_results=total_results,
    )


def backend_page(page=0, source_id="private-source-id"):
    return ROUTE_MODULE.OPDSCatalogPage(
        source_id=source_id,
        requested_url="https://private.example.org/requested",
        final_url="https://private.example.org/final",
        title="Backend results",
        books=(
            {
                "title": "Raw backend book",
                "epub_url": "https://files.example.org/book.epub",
                "download_url": "https://files.example.org/download",
                "acquisition_links": [
                    {"href": "https://files.example.org/book.epub"}
                ],
                "related": (
                    ROUTE_MODULE.CatalogRef(
                        source_id=source_id,
                        url="https://private.example.org/related/catalog.xml",
                        title="Related catalog",
                        kind="related",
                    ),
                ),
            },
        ),
        navigation=(),
        next_url="https://private.example.org/next",
    )


class OPDSSearchRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = ROUTE_MODULE.app.test_client()
        self.loader_calls = []
        self.builder_calls = []

        def loader(query, page=0, force=False):
            self.loader_calls.append(
                {"query": query, "page": page, "force": force}
            )
            return backend_page(page)

        def builder(search_page, query, page=0):
            self.builder_calls.append(
                {"search_page": search_page, "query": query, "page": page}
            )
            return search_view(
                query=query,
                page=page,
                has_previous=page > 0,
            )

        ROUTE_MODULE.load_current_opds_search_page = loader
        ROUTE_MODULE.build_opds_search_view = builder

    def get(self, query="Dune", page=None):
        query_string = {"q": query}
        if page is not None:
            query_string["page"] = page
        return self.client.get("/search/opds", query_string=query_string)

    def get_render_context(self, query="Dune", page=None):
        contexts = []
        original = ROUTE_MODULE.render_template_string

        def capture(template, **context):
            contexts.append(context)
            return original(template, **context)

        ROUTE_MODULE.render_template_string = capture
        try:
            response = self.get(query, page=page)
        finally:
            ROUTE_MODULE.render_template_string = original
        return response, contexts[-1]

    def test_a_route_is_registered_for_get_only(self):
        self.assertEqual(self.get().status_code, 200)
        call_count = len(self.loader_calls)
        self.assertEqual(self.client.post("/search/opds").status_code, 405)
        self.assertEqual(len(self.loader_calls), call_count)

    def test_b_missing_query_returns_400(self):
        response = self.client.get("/search/opds")
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 400)
        self.assertIn("Укажите поисковый запрос.", body)
        self.assertIn('<a href="/">← На главный экран</a>', body)
        self.assertEqual(self.loader_calls, [])

    def test_c_whitespace_query_returns_400(self):
        response = self.get("   ")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.loader_calls, [])

    def test_d_valid_query_calls_current_search_loader(self):
        response = self.get("  Dune  ")
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            '<a href="/">← На главный экран</a>',
            response.get_data(as_text=True),
        )
        self.assertEqual(self.loader_calls[0]["query"], "Dune")

    def test_e_missing_page_uses_zero(self):
        self.get()
        self.assertEqual(self.loader_calls[0]["page"], 0)

    def test_f_page_one_reaches_backend_as_one(self):
        self.get(page="1")
        self.assertEqual(self.loader_calls[0]["page"], 1)

    def test_g_non_integer_page_returns_400(self):
        response = self.get(page="abc")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.loader_calls, [])

    def test_h_negative_page_returns_400(self):
        response = self.get(page="-1")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.loader_calls, [])

    def test_i_page_at_limit_returns_400(self):
        response = self.get(page=str(ROUTE_MODULE.MAX_CATALOG_PAGES))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.loader_calls, [])

    def test_j_force_is_always_false(self):
        self.get(page="1")
        self.assertIs(self.loader_calls[0]["force"], False)

    def test_j2_successful_page_zero_saves_search_and_context_urls(self):
        response = self.get("query")
        self.assertEqual(response.status_code, 200)
        with self.client.session_transaction() as flask_session:
            self.assertEqual(
                flask_session["last_context_url"],
                "/search/opds?q=query",
            )
            self.assertEqual(
                flask_session["last_search_url"],
                "/search/opds?q=query",
            )

    def test_j3_successful_page_two_preserves_page_in_saved_urls(self):
        response = self.get("query", page="2")
        self.assertEqual(response.status_code, 200)
        with self.client.session_transaction() as flask_session:
            expected = "/search/opds?q=query&page=2"
            self.assertEqual(flask_session["last_context_url"], expected)
            self.assertEqual(flask_session["last_search_url"], expected)

    def test_j4_invalid_query_and_page_do_not_save_context(self):
        for path in ("/search/opds", "/search/opds?q=query&page=invalid"):
            with self.subTest(path=path):
                client = ROUTE_MODULE.app.test_client()
                response = client.get(path)
                self.assertEqual(response.status_code, 400)
                with client.session_transaction() as flask_session:
                    self.assertNotIn("last_context_url", flask_session)
                    self.assertNotIn("last_search_url", flask_session)

    def test_j5_search_load_error_does_not_save_context(self):
        ROUTE_MODULE.load_current_opds_search_page = (
            lambda *args, **kwargs: (_ for _ in ()).throw(
                requests.ConnectionError("network failure")
            )
        )
        response = self.get("query")
        self.assertEqual(response.status_code, 502)
        with self.client.session_transaction() as flask_session:
            self.assertNotIn("last_context_url", flask_session)
            self.assertNotIn("last_search_url", flask_session)

    def test_j6_return_routes_use_last_neutral_search_page(self):
        self.get("query", page="2")
        expected = "/search/opds?q=query&page=2"
        context_response = self.client.get("/context-return")
        search_response = self.client.get("/search-return")
        self.assertEqual(context_response.status_code, 302)
        self.assertEqual(context_response.headers["Location"], expected)
        self.assertEqual(search_response.status_code, 302)
        self.assertEqual(search_response.headers["Location"], expected)

    def test_k_backend_result_passes_through_view_builder(self):
        self.get(page="1")
        self.assertIsInstance(
            self.builder_calls[0]["search_page"],
            ROUTE_MODULE.OPDSCatalogPage,
        )
        self.assertEqual(self.builder_calls[0]["query"], "Dune")
        self.assertEqual(self.builder_calls[0]["page"], 1)

    def test_k2_builder_value_error_is_not_masked_as_remote_502(self):
        original = ROUTE_MODULE.build_opds_search_view
        ROUTE_MODULE.build_opds_search_view = (
            lambda *args, **kwargs: (_ for _ in ()).throw(
                ValueError("presentation contract violation")
            )
        )
        try:
            with self.assertRaisesRegex(
                ValueError,
                "presentation contract violation",
            ):
                self.get()
        finally:
            ROUTE_MODULE.build_opds_search_view = original

    def test_l_template_receives_view_not_backend_page(self):
        contexts = []
        original = ROUTE_MODULE.render_template_string

        def capture(template, **context):
            contexts.append(context)
            return original(template, **context)

        ROUTE_MODULE.render_template_string = capture
        try:
            self.get()
        finally:
            ROUTE_MODULE.render_template_string = original
        self.assertIsInstance(contexts[0]["view"], ROUTE_MODULE.OPDSSearchView)
        self.assertFalse(
            any(
                isinstance(value, ROUTE_MODULE.OPDSCatalogPage)
                for value in contexts[0].values()
            )
        )

    def test_m_query_is_html_escaped(self):
        unsafe = "<script>alert(1)</script>"
        body = self.get(unsafe).get_data(as_text=True)
        self.assertNotIn(unsafe, body)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", body)

    def test_n_source_title_and_author_are_html_escaped(self):
        unsafe_title = "<img src=x onerror=alert(1)>"
        unsafe_author = "<b>Author</b>"
        ROUTE_MODULE.build_opds_search_view = lambda *args, **kwargs: search_view(
            title=unsafe_title,
            books=(book_view(title=unsafe_title, author=unsafe_author),),
        )
        body = self.get().get_data(as_text=True)
        self.assertNotIn(unsafe_title, body)
        self.assertNotIn(unsafe_author, body)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", body)
        self.assertIn("&lt;b&gt;Author&lt;/b&gt;", body)

    def test_o_template_has_no_safe_filter_for_untrusted_text(self):
        self.assertNotIn("|safe", self.template_source().lower())

    def test_p_empty_results_show_neutral_message(self):
        ROUTE_MODULE.build_opds_search_view = lambda *args, **kwargs: search_view(
            books=(),
        )
        body = self.get().get_data(as_text=True)
        self.assertIn("По вашему запросу ничего не найдено.", body)
        self.assertIn('href="/queue"', body)
        self.assertIn("Очередь", body)

    def test_p1_queue_link_is_a_plain_get_link(self):
        template = self.template_source()
        self.assertIn("url_for('queue_page')", template)
        self.assertIn(">Очередь</a>", template)
        self.assertNotIn('action="{{ url_for(\'queue_page\') }}"', template)

    def test_p2_total_results_is_rendered_when_present(self):
        ROUTE_MODULE.build_opds_search_view = lambda *args, **kwargs: search_view(
            total_results=57,
        )
        body = self.get().get_data(as_text=True)
        self.assertIn("Всего найдено: 57", body)

    def test_p3_zero_total_results_is_rendered(self):
        ROUTE_MODULE.build_opds_search_view = lambda *args, **kwargs: search_view(
            books=(),
            total_results=0,
        )
        body = self.get().get_data(as_text=True)
        self.assertIn("Всего найдено: 0", body)

    def test_p4_missing_total_results_is_not_rendered(self):
        body = self.get().get_data(as_text=True)
        self.assertNotIn("Всего найдено:", body)

    def test_p5_page_book_count_still_uses_only_rendered_books(self):
        ROUTE_MODULE.build_opds_search_view = lambda *args, **kwargs: search_view(
            books=(book_view(),),
            total_results=57,
        )
        body = self.get().get_data(as_text=True)
        self.assertIn("Книг на странице: 1", body)

    def test_q_has_previous_links_to_page_minus_one(self):
        ROUTE_MODULE.build_opds_search_view = lambda *args, **kwargs: search_view(
            page=2,
            has_previous=True,
        )
        body = html.unescape(self.get(page="2").get_data(as_text=True))
        self.assertIn("/search/opds?q=Dune&page=1", body)

    def test_r_has_next_links_to_page_plus_one(self):
        ROUTE_MODULE.build_opds_search_view = lambda *args, **kwargs: search_view(
            page=2,
            has_next=True,
        )
        body = html.unescape(self.get(page="2").get_data(as_text=True))
        self.assertIn("/search/opds?q=Dune&page=3", body)

    def test_s_pagination_links_preserve_query(self):
        ROUTE_MODULE.build_opds_search_view = lambda *args, **kwargs: search_view(
            query="Dune Messiah",
            has_next=True,
        )
        body = html.unescape(self.get("Dune Messiah").get_data(as_text=True))
        self.assertIn("/search/opds?q=Dune+Messiah&page=1", body)

    def test_t_next_url_is_not_exposed(self):
        body = self.get().get_data(as_text=True)
        self.assertNotIn("https://private.example.org/next", body)
        self.assertNotIn("next_url", self.template_source())

    def test_u_requested_and_final_urls_are_not_exposed(self):
        body = self.get().get_data(as_text=True)
        self.assertNotIn("https://private.example.org/requested", body)
        self.assertNotIn("https://private.example.org/final", body)
        self.assertNotIn("requested_url", self.template_source())
        self.assertNotIn("final_url", self.template_source())

    def test_v_acquisition_and_download_urls_are_not_exposed(self):
        body = self.get().get_data(as_text=True)
        for forbidden in (
            "files.example.org",
            "epub_url",
            "download_url",
            "acquisition_links",
            '"href"',
            "cover_url",
            "thumbnail_url",
            "web_url",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, body)

    def test_v2_related_catalog_url_is_not_exposed(self):
        remote_url = "https://private.example.org/related/catalog.xml"
        related = ROUTE_MODULE.RegisteredCatalogRef(
            token="catalog:" + "c" * 64,
            title="Related catalog",
            kind="related",
        )
        ROUTE_MODULE.build_opds_search_view = lambda *args, **kwargs: search_view(
            books=(book_view(related=(related,)),),
        )
        body = html.unescape(self.get().get_data(as_text=True))
        with ROUTE_MODULE.app.test_request_context():
            expected_href = url_for(
                "registered_catalog_page",
                token=related.token,
            )
        self.assertIn("Related catalog", body)
        self.assertIn(f'href="{expected_href}"', body)
        self.assertIn(related.token, body)
        self.assertNotEqual(related.token, remote_url)
        self.assertNotIn(remote_url, body)
        self.assertNotIn("private-source-id", body)

    def test_v3_book_without_related_has_no_related_actions(self):
        ROUTE_MODULE.build_opds_search_view = lambda *args, **kwargs: search_view(
            books=(book_view(related=()),),
        )
        body = self.get().get_data(as_text=True)
        self.assertNotIn('class="related-actions"', body)

    def test_v4_multiple_related_catalogs_keep_provider_order(self):
        related = (
            ROUTE_MODULE.RegisteredCatalogRef(
                token="catalog:" + "1" * 64,
                title="First Collection",
                kind="related",
            ),
            ROUTE_MODULE.RegisteredCatalogRef(
                token="catalog:" + "2" * 64,
                title="Second Collection",
                kind="related",
            ),
        )
        ROUTE_MODULE.build_opds_search_view = lambda *args, **kwargs: search_view(
            books=(book_view(related=related),),
        )
        body = html.unescape(self.get().get_data(as_text=True))
        self.assertLess(body.index(related[0].title), body.index(related[1].title))
        with ROUTE_MODULE.app.test_request_context():
            expected_hrefs = [
                url_for("registered_catalog_page", token=item.token)
                for item in related
            ]
        for item, expected_href in zip(related, expected_hrefs):
            with self.subTest(title=item.title):
                self.assertIn(item.title, body)
                self.assertIn(f'href="{expected_href}"', body)

    def test_v5_related_title_is_html_escaped(self):
        unsafe_title = 'Related <Catalog> & "Example"'
        related = ROUTE_MODULE.RegisteredCatalogRef(
            token="catalog:" + "e" * 64,
            title=unsafe_title,
            kind="related",
        )
        ROUTE_MODULE.build_opds_search_view = lambda *args, **kwargs: search_view(
            books=(book_view(related=(related,)),),
        )
        body = self.get().get_data(as_text=True)
        self.assertNotIn(unsafe_title, body)
        self.assertIn("Related &lt;Catalog&gt; &amp; &#34;Example&#34;", body)

    def test_v6_related_links_use_only_existing_registered_endpoint(self):
        template = self.template_source()
        self.assertIn(
            "url_for('registered_catalog_page', token=related.token)",
            template,
        )
        self.assertNotIn("/related/", template)
        self.assertNotIn("?url=", template)
        self.assertNotIn("?href=", template)
        self.assertNotIn("?source=", template)
        for forbidden in ("fetch(", "onclick", "window.location", "localStorage"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, template)

    def test_v7_success_passes_opaque_selection_storage_key(self):
        response, context = self.get_render_context("  Dune  ")
        key = context["selection_storage_key"]
        storage_key_line = next(
            line
            for line in response.get_data(as_text=True).splitlines()
            if "const selectionStorageKey" in line
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            key,
            ROUTE_MODULE.catalog_selection_storage_key(
                "private-source-id",
                "search",
                "Dune",
            ),
        )
        self.assertNotIn("private-source-id", key)
        self.assertNotIn("Dune", key)
        self.assertIn(key, storage_key_line)
        self.assertNotIn("private-source-id", storage_key_line)
        self.assertNotIn("Dune", storage_key_line)

    def test_v8_selection_storage_key_ignores_page_but_changes_with_query(self):
        _, first_context = self.get_render_context("Dune", page="0")
        _, next_context = self.get_render_context("Dune", page="2")
        _, other_context = self.get_render_context("Foundation", page="0")
        self.assertEqual(
            first_context["selection_storage_key"],
            next_context["selection_storage_key"],
        )
        self.assertNotEqual(
            first_context["selection_storage_key"],
            other_context["selection_storage_key"],
        )

    def test_v9_selection_storage_key_changes_with_source(self):
        _, first_context = self.get_render_context()
        ROUTE_MODULE.load_current_opds_search_page = (
            lambda *args, **kwargs: backend_page(source_id="another-source-id")
        )
        _, other_context = self.get_render_context()
        self.assertNotEqual(
            first_context["selection_storage_key"],
            other_context["selection_storage_key"],
        )

    def test_v10_each_book_has_checkbox_with_opaque_id(self):
        opaque_ids = (
            "urn:uuid:0d508a30-073f-4028-b522-592a2acbdb98",
            "tag:catalog.example.test,2026:item?edition=2&format=epub",
        )
        ROUTE_MODULE.build_opds_search_view = lambda *args, **kwargs: search_view(
            books=tuple(book_view(book_id=book_id) for book_id in opaque_ids),
        )
        body = html.unescape(self.get().get_data(as_text=True))
        self.assertEqual(body.count('class="book-check"'), len(opaque_ids))
        for book_id in opaque_ids:
            with self.subTest(book_id=book_id):
                self.assertIn(f'value="{book_id}"', body)

    def test_v11_selection_ui_uses_only_neutral_queue_submit_contract(self):
        template = self.template_source()
        self.assertNotIn('name="book_id"', template)
        self.assertIn('id="opdsQueueForm"', template)
        self.assertIn('method="post"', template)
        self.assertIn("url_for('opds_search_queue_add')", template)
        for forbidden in (
            "/download",
            "bulk_start",
            "queue_add_bulk",
            "queue_add_one",
            "download_book",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, template)

    def test_v12_selection_js_restores_and_persists_global_opaque_set(self):
        template = self.template_source()
        for required in (
            "const selectionStorageKey = {{ selection_storage_key|tojson }};",
            "sessionStorage.getItem",
            "sessionStorage.setItem",
            "new Set",
            "selectedBookIds",
            "selectedBookIds.size",
            "checkbox.checked = selectedBookIds.has(id)",
            "String(checkbox.value)",
            "selectedCount.textContent",
            "try {",
            "catch (error)",
        ):
            with self.subTest(required=required):
                self.assertIn(required, template)
        for forbidden in (
            "localStorage",
            "fetch(",
            "XMLHttpRequest",
            "parseInt(",
            "Number(",
            "innerHTML",
            "|safe",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, template)

    def test_v13_empty_results_hide_selection_toolbar_without_clearing_storage(self):
        ROUTE_MODULE.build_opds_search_view = lambda *args, **kwargs: search_view(
            books=(),
        )
        body = self.get().get_data(as_text=True)
        self.assertNotIn('class="selection-toolbar"', body)
        self.assertNotIn("sessionStorage.removeItem", self.template_source())

    def test_w_source_without_search_returns_409(self):
        ROUTE_MODULE.load_current_opds_search_page = (
            lambda *args, **kwargs: (_ for _ in ()).throw(
                ValueError("Этот OPDS-источник не предоставляет поиск")
            )
        )
        response = self.get()
        self.assertEqual(response.status_code, 409)
        self.assertIn(
            "Этот OPDS-источник не предоставляет поиск.",
            response.get_data(as_text=True),
        )

    def test_x_unconfigured_source_returns_409_and_settings_link(self):
        ROUTE_MODULE.load_current_opds_search_page = (
            lambda *args, **kwargs: (_ for _ in ()).throw(
                ValueError("OPDS-источник не настроен")
            )
        )
        response = self.get()
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 409)
        self.assertIn("OPDS-источник не настроен.", body)
        self.assertIn('/settings/opds', body)

    def test_y_expected_remote_failures_return_sanitized_502(self):
        secret = "https://private.example.org/descriptor"
        for failure in (
            ValueError(f"invalid response at {secret}"),
            RuntimeError(f"runtime failure at {secret}"),
            requests.ConnectionError(f"network failure at {secret}"),
        ):
            with self.subTest(failure=type(failure).__name__):
                ROUTE_MODULE.load_current_opds_search_page = (
                    lambda *args, failure=failure, **kwargs: (_ for _ in ()).throw(
                        failure
                    )
                )
                response = self.get()
                body = response.get_data(as_text=True)
                self.assertEqual(response.status_code, 502)
                self.assertIn("Не удалось загрузить результаты OPDS-поиска.", body)
                self.assertNotIn(secret, body)
                self.assertNotIn("Traceback", body)

    def test_z_route_does_not_call_legacy_search(self):
        called_names = self.route_called_names()
        for forbidden in (
            "search_books",
            "search_authors",
            "search_all",
            "legacy_opds_get",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, called_names)
        self.assertIn("load_current_opds_search_page", called_names)

    def test_aa_route_has_no_direct_requests_calls(self):
        source = self.route_source().lower()
        self.assertNotIn("requests.", source)
        self.assertNotIn("requests.get", source)

    def test_ab_route_and_template_have_no_legacy_markers(self):
        source = "\n".join((self.route_source(), self.template_source())).lower()
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

    def route_node(self):
        return next(
            node
            for node in ROUTE_MODULE.__source_tree__.body
            if getattr(node, "name", None) == "opds_search_page"
        )

    def route_source(self):
        return (
            ast.get_source_segment(ROUTE_MODULE.__source_text__, self.route_node())
            or ""
        )

    def route_called_names(self):
        return {
            child.func.id
            for child in ast.walk(self.route_node())
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
        }

    def template_source(self):
        node = next(
            node
            for node in ROUTE_MODULE.__source_tree__.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "OPDS_SEARCH_HTML"
                for target in node.targets
            )
        )
        return ast.get_source_segment(ROUTE_MODULE.__source_text__, node) or ""


if __name__ == "__main__":
    unittest.main()
