import ast
import dataclasses
import re
import sys
import types
import unittest
from pathlib import Path

from flask import Flask, render_template_string


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_home_module():
    """Загружает только home route и templates без runtime приложения."""
    source = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    wanted_definitions = {"setup_page", "index"}
    wanted_assignments = {
        "NEUTRAL_HOME_HTML",
        "SETUP_HTML",
        "SETTINGS_HTML",
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

    module = types.ModuleType("isolated_neutral_home_test")
    sys.modules[module.__name__] = module
    app = Flask(module.__name__)

    @app.get("/settings")
    def settings_page():
        return "settings"

    @app.get("/settings/opds")
    def opds_settings_page():
        return "opds settings"

    @app.get("/catalog/opds")
    def open_current_opds_catalog():
        return "catalog"

    @app.get("/search/opds")
    def opds_search_page():
        return "search"

    module.__dict__.update(
        dataclass=dataclasses.dataclass,
        app=app,
        render_template_string=render_template_string,
        COMMON_CSS="",
        DESTINATION="X:/Books",
        APP_CONFIG={},
        current_source_config=None,
        has_configured_opds_source=None,
    )
    exec(
        compile(ast.Module(body=body, type_ignores=[]), "app.py", "exec"),
        module.__dict__,
    )
    module.app.testing = True
    module.__source_text__ = source
    module.__source_tree__ = tree
    return module


HOME_MODULE = load_home_module()


def function_node(name):
    return next(
        node
        for node in HOME_MODULE.__source_tree__.body
        if getattr(node, "name", None) == name
    )


def assignment_source(name):
    node = next(
        item
        for item in HOME_MODULE.__source_tree__.body
        if isinstance(item, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == name
            for target in item.targets
        )
    )
    return ast.get_source_segment(HOME_MODULE.__source_text__, node) or ""


class NeutralHomeTests(unittest.TestCase):
    def setUp(self):
        HOME_MODULE.APP_CONFIG.clear()
        HOME_MODULE.APP_CONFIG.update(setup_complete=True)
        self.source = types.SimpleNamespace(
            source_id="",
            root_url="",
            display_name="",
        )
        HOME_MODULE.current_source_config = lambda: self.source
        HOME_MODULE.has_configured_opds_source = lambda: bool(
            self.source.root_url
        )
        self.client = HOME_MODULE.app.test_client()

    def test_a_incomplete_setup_still_shows_setup_flow(self):
        HOME_MODULE.APP_CONFIG["setup_complete"] = False
        HOME_MODULE.current_source_config = lambda: (_ for _ in ()).throw(
            AssertionError("setup gate must run before source lookup")
        )
        body = self.client.get("/").get_data(as_text=True)
        self.assertIn("Первоначальная настройка", body)
        self.assertIn("chooseLibraryButton", body)
        self.assertNotIn("Локальный OPDS-клиент", body)

    def test_b_completed_setup_shows_neutral_home(self):
        body = self.client.get("/").get_data(as_text=True)
        self.assertIn("Локальный OPDS-клиент", body)
        self.assertNotIn("searchForm", body)

    def test_c_home_always_shows_settings_actions(self):
        body = self.client.get("/").get_data(as_text=True)
        self.assertIn("Настроить OPDS", body)
        self.assertIn('href="/settings/opds"', body)
        self.assertIn("Настройки библиотеки", body)
        self.assertIn('href="/settings"', body)

    def test_d_unconfigured_source_has_no_open_catalog_link(self):
        body = self.client.get("/").get_data(as_text=True)
        self.assertIn("OPDS-источник не настроен", body)
        self.assertNotIn("Открыть OPDS-каталог", body)
        self.assertNotIn('href="/catalog/opds"', body)

    def test_e_configured_source_has_open_catalog_link(self):
        self.source.root_url = "https://catalog.example.org/root.xml"
        body = self.client.get("/").get_data(as_text=True)
        self.assertIn("OPDS-источник настроен", body)
        self.assertIn("Открыть OPDS-каталог", body)
        self.assertIn('href="/catalog/opds"', body)

    def test_f_source_name_is_visible_when_present(self):
        self.source.root_url = "https://catalog.example.org/root.xml"
        self.source.display_name = "Example Catalog"
        body = self.client.get("/").get_data(as_text=True)
        self.assertIn("Example Catalog", body)

    def test_g_source_name_is_html_escaped(self):
        self.source.root_url = "https://catalog.example.org/root.xml"
        self.source.display_name = "<script>alert(1)</script>"
        body = self.client.get("/").get_data(as_text=True)
        self.assertNotIn("<script>alert(1)</script>", body)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", body)

    def test_h_source_url_and_id_are_not_rendered(self):
        self.source.root_url = "https://private.example.org/secret/catalog"
        self.source.source_id = "sha256:private-source-id"
        self.source.display_name = "Private Catalog"
        body = self.client.get("/").get_data(as_text=True)
        self.assertNotIn(self.source.root_url, body)
        self.assertNotIn("private.example.org", body)
        self.assertNotIn(self.source.source_id, body)

    def test_i_orphan_legacy_search_symbols_are_absent(self):
        assignments = {
            target.id
            for node in HOME_MODULE.__source_tree__.body
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        definitions = {
            node.name
            for node in HOME_MODULE.__source_tree__.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for name in (
            "MAIN_HTML",
            "OPDS_SEARCH",
            "search_cache",
            "search_cache_lock",
        ):
            with self.subTest(assignment=name):
                self.assertNotIn(name, assignments)
        for name in (
            "search_cache_key",
            "search_cache_get",
            "search_cache_put",
            "perform_search_cached",
            "fetch_search_page",
            "search_books",
            "parse_author_search_entry",
            "parse_feed_authors",
            "author_tokens",
            "author_search_terms",
            "author_match_score",
            "fetch_author_search_page",
            "search_authors",
        ):
            with self.subTest(definition=name):
                self.assertNotIn(name, definitions)

    def test_j_home_calls_no_legacy_network_queue_or_health_helpers(self):
        forbidden_names = (
            "legacy_opds_get",
            "health_snapshot",
            "queue_active_book_ids",
            "queue_pending_count",
        )
        originals = {
            name: HOME_MODULE.__dict__.get(name) for name in forbidden_names
        }

        def forbidden_call(*args, **kwargs):
            raise AssertionError("neutral home called a legacy helper")

        for name in forbidden_names:
            HOME_MODULE.__dict__[name] = forbidden_call
        try:
            response = self.client.get("/?q=test&mode=authors&page=9")
        finally:
            for name, original in originals.items():
                if original is None:
                    HOME_MODULE.__dict__.pop(name, None)
                else:
                    HOME_MODULE.__dict__[name] = original
        self.assertEqual(response.status_code, 200)

    def test_k_orphan_generic_loading_css_is_removed(self):
        css_source = assignment_source("COMMON_CSS")
        for marker in (
            ".loading-overlay",
            ".loading-box",
            ".spinner",
            ".loading-title",
            ".loading-note",
            "@keyframes spin",
        ):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, css_source)

    def test_l_live_legacy_and_neutral_symbols_still_exist(self):
        assignments = {
            target.id
            for node in HOME_MODULE.__source_tree__.body
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        definitions = {
            node.name
            for node in HOME_MODULE.__source_tree__.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for name in (
            "NEUTRAL_HOME_HTML",
            "SEARCH_CACHE_TTL",
            "LEGACY_OPDS_BASE",
            "LEGACY_QUEUE_SOURCE_ID",
        ):
            with self.subTest(assignment=name):
                self.assertIn(name, assignments)
        for name in (
            "normalize_author_text",
            "parse_feed_books",
            "parse_entry",
            "legacy_opds_get",
            "allowed_legacy_opds_url",
            "load_catalog_page",
            "collect_catalog",
            "render_catalog",
            "save_epub",
            "save_fb2",
            "download_error_info",
            "health_snapshot",
            "health_api",
        ):
            with self.subTest(definition=name):
                self.assertIn(name, definitions)

    def test_m_legacy_catalog_and_background_routes_still_exist(self):
        definitions = {
            getattr(node, "name", None) for node in HOME_MODULE.__source_tree__.body
        }
        for name in (
            "author_catalog",
            "series_catalog",
            "queue_page",
            "queue_history",
            "jobs_page",
        ):
            self.assertIn(name, definitions)

    def test_n_settings_home_link_still_targets_index(self):
        settings_source = assignment_source("SETTINGS_HTML")
        self.assertEqual(settings_source.count("url_for('index')"), 1)
        self.assertIn("← На главный экран", settings_source)
        self.assertNotIn("← На главную\n", settings_source)

    def test_o_index_uses_only_neutral_source_helpers_after_gate(self):
        node = function_node("index")
        called_names = {
            child.func.id
            for child in ast.walk(node)
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
        }
        self.assertIn("setup_page", called_names)
        self.assertIn("current_source_config", called_names)
        self.assertIn("has_configured_opds_source", called_names)
        self.assertIn("render_template_string", called_names)
        for forbidden in (
            "perform_search_cached",
            "queue_pending_count",
            "queue_active_book_ids",
            "health_snapshot",
            "legacy_opds_get",
        ):
            self.assertNotIn(forbidden, called_names)

    def test_p_neutral_template_has_no_forbidden_dependencies(self):
        template = HOME_MODULE.NEUTRAL_HOME_HTML.lower()
        for forbidden in (
            "flibusta",
            "opds_base",
            "/api/health",
            "queue",
            "download",
            "epub",
            "fb2",
            "proxy",
            "socks",
            "xray",
            "tor",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, template)
        self.assertNotIn("|safe", template)

    def test_q_desktop_startup_still_opens_flask_app(self):
        module_guard = next(
            node
            for node in HOME_MODULE.__source_tree__.body
            if isinstance(node, ast.If)
            and "__name__" in (ast.get_source_segment(HOME_MODULE.__source_text__, node.test) or "")
        )
        guard_source = (
            ast.get_source_segment(HOME_MODULE.__source_text__, module_guard) or ""
        )
        self.assertIn("webview.create_window(", guard_source)
        self.assertIn("url=app", guard_source)
        self.assertIn("js_api=desktop_api", guard_source)
        self.assertIn("webview.start(", guard_source)


class NeutralHomeSearchTests(unittest.TestCase):
    def setUp(self):
        HOME_MODULE.APP_CONFIG.clear()
        HOME_MODULE.APP_CONFIG.update(setup_complete=True)
        self.source = types.SimpleNamespace(
            source_id="source-a",
            root_url="https://catalog.example.org/root.xml",
            display_name="Example Catalog",
        )
        HOME_MODULE.current_source_config = lambda: self.source
        HOME_MODULE.has_configured_opds_source = lambda: bool(
            self.source.root_url
        )
        self.client = HOME_MODULE.app.test_client()

    def body(self):
        return self.client.get("/").get_data(as_text=True)

    def test_a_configured_source_shows_search_form(self):
        body = self.body()
        self.assertIn("Поиск по OPDS", body)
        self.assertIn('<form id="opdsSearchForm" class="search-form"', body)

    def test_b_search_form_uses_get(self):
        self.assertIn('method="get"', self.body())

    def test_c_action_uses_opds_search_endpoint(self):
        body = self.body()
        self.assertIn('action="/search/opds"', body)
        self.assertIn(
            "url_for('opds_search_page')",
            HOME_MODULE.NEUTRAL_HOME_HTML,
        )

    def test_d_query_input_uses_q_name(self):
        self.assertIn('name="q"', self.body())

    def test_e_query_input_uses_search_type(self):
        self.assertIn('type="search"', self.body())

    def test_f_form_has_no_hidden_page(self):
        template = HOME_MODULE.NEUTRAL_HOME_HTML.lower()
        self.assertNotIn('type="hidden"', template)
        self.assertNotIn('name="page"', template)

    def test_g_form_has_no_force_parameter(self):
        self.assertNotIn('name="force"', HOME_MODULE.NEUTRAL_HOME_HTML.lower())

    def test_h_home_search_has_neutral_loading_overlay(self):
        template = HOME_MODULE.NEUTRAL_HOME_HTML
        for required in (
            'id="opdsSearchForm"',
            'id="opdsSearchLoadingOverlay"',
            'aria-hidden="true"',
            'role="status"',
            'aria-live="polite"',
            'id="openOpdsCatalogLink"',
            "Поиск книг...",
            "Загрузка каталога...",
            "Получение данных из OPDS-каталога",
            "opdsSearchForm.addEventListener('submit', showOpdsSearchLoading)",
            "openOpdsCatalogLink.addEventListener('click', showOpenOpdsCatalogLoading)",
        ):
            with self.subTest(required=required):
                self.assertIn(required, template)
        lowered = template.lower()
        for forbidden in (
            "fetch(",
            "xmlhttprequest",
            "localstorage",
            "sessionstorage",
            "source_id",
            "acquisition_links",
            "epub_url",
            "fb2_url",
            "download_url",
            "flibusta",
            "флибуста",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, lowered)

    def test_h2_home_loading_allows_first_navigation_and_resets_on_pageshow(self):
        template = HOME_MODULE.NEUTRAL_HOME_HTML
        handler_match = re.search(
            r"function beginOpdsHomeLoading\(event, title, note\) \{(?P<body>.*?)^\}",
            template,
            re.DOTALL | re.MULTILINE,
        )
        self.assertIsNotNone(handler_match)
        handler = handler_match.group("body")
        self.assertEqual(handler.count("event.preventDefault()"), 1)
        self.assertLess(
            handler.index("if (opdsSearchLoading)"),
            handler.index("event.preventDefault()"),
        )
        self.assertLess(
            handler.index("event.preventDefault()"),
            handler.index("opdsSearchLoading = true"),
        )
        self.assertIn("opdsSearchSubmit.disabled = true", handler)
        self.assertIn("opdsSearchSubmit.disabled = false", template)
        self.assertIn("'Поиск книг...'", template)
        self.assertIn("'Загрузка каталога...'", template)
        self.assertIn(
            '<a href="{{ url_for(\'opds_settings_page\') }}">Настроить OPDS</a>',
            template,
        )
        self.assertIn(
            '<a href="{{ url_for(\'settings_page\') }}">Настройки библиотеки</a>',
            template,
        )
        self.assertNotIn('id="opdsSettingsLoadingLink"', template)
        self.assertIn(
            "window.addEventListener('pageshow', resetOpdsSearchLoading)",
            template,
        )

    def test_i_home_calls_no_search_discovery_or_http_helpers(self):
        forbidden_names = (
            "load_current_opds_feed",
            "resolve_current_opds_search_descriptor",
            "load_current_opds_search_page",
            "OPDSHTTPClient",
        )
        originals = {
            name: HOME_MODULE.__dict__.get(name) for name in forbidden_names
        }

        def forbidden_call(*args, **kwargs):
            raise AssertionError("home performed OPDS search discovery")

        for name in forbidden_names:
            HOME_MODULE.__dict__[name] = forbidden_call
        try:
            response = self.client.get("/")
        finally:
            for name, original in originals.items():
                if original is None:
                    HOME_MODULE.__dict__.pop(name, None)
                else:
                    HOME_MODULE.__dict__[name] = original
        self.assertEqual(response.status_code, 200)

    def test_j_unconfigured_source_has_no_active_search_form(self):
        self.source.root_url = ""
        body = self.body()
        self.assertNotIn("Поиск по OPDS", body)
        self.assertNotIn('<form class="search-form"', body)

    def test_k_opds_settings_action_remains(self):
        body = self.body()
        self.assertIn("Настроить OPDS", body)
        self.assertIn('href="/settings/opds"', body)

    def test_l_open_opds_action_remains_for_configured_source(self):
        body = self.body()
        self.assertIn("Открыть OPDS-каталог", body)
        self.assertIn('href="/catalog/opds"', body)

    def test_m_library_settings_action_remains(self):
        body = self.body()
        self.assertIn("Настройки библиотеки", body)
        self.assertIn('href="/settings"', body)

    def test_n_form_has_no_legacy_markers(self):
        template = HOME_MODULE.NEUTRAL_HOME_HTML.lower()
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
                self.assertNotIn(marker, template)

    def test_o_home_search_is_offline_and_has_no_backend_calls(self):
        node = function_node("index")
        called_names = {
            child.func.id
            for child in ast.walk(node)
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
        }
        for forbidden in (
            "load_current_opds_feed",
            "resolve_current_opds_search_descriptor",
            "load_current_opds_search_page",
            "OPDSHTTPClient",
            "requests",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, called_names)


if __name__ == "__main__":
    unittest.main()
