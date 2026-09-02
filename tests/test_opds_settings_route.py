import ast
import dataclasses
import sys
import types
import unittest
from pathlib import Path

import requests
from flask import Flask, redirect, render_template_string, request, url_for


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_settings_module():
    """Загружает только OPDS settings workflow без runtime приложения."""
    source = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    wanted_definitions = {
        "SourceConfig",
        "SourceValidationResult",
        "normalize_app_config",
        "source_config_from_app_config",
        "current_source_config",
        "has_configured_opds_source",
        "_save_and_replace_app_config",
        "clear_configured_opds_source",
        "settings_page",
        "opds_settings_page",
    }
    wanted_assignments = {
        "CONFIG_VERSION",
        "SETUP_HTML",
        "SETTINGS_HTML",
        "OPDS_SETTINGS_HTML",
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

    module = types.ModuleType("isolated_opds_settings_route_test")
    sys.modules[module.__name__] = module
    app = Flask(module.__name__)

    @app.get("/")
    def index():
        return "index"

    @app.get("/catalog/opds")
    def open_current_opds_catalog():
        return "catalog"

    module.__dict__.update(
        dataclass=dataclasses.dataclass,
        app=app,
        request=request,
        redirect=redirect,
        render_template_string=render_template_string,
        url_for=url_for,
        requests=requests,
        DEFAULT_DESTINATION="test-default-library",
        COMMON_CSS="",
        DESTINATION="X:/Books",
        APP_CONFIG={},
        save_app_config=lambda config: None,
        configure_opds_source=None,
    )
    exec(
        compile(ast.Module(body=body, type_ignores=[]), "app.py", "exec"),
        module.__dict__,
    )
    module.app.testing = True
    module.__source_text__ = source
    module.__source_tree__ = tree
    return module


SETTINGS_MODULE = load_settings_module()


class OpdsSettingsRouteTests(unittest.TestCase):
    def setUp(self):
        SETTINGS_MODULE.APP_CONFIG.clear()
        SETTINGS_MODULE.APP_CONFIG.update(
            config_version=SETTINGS_MODULE.CONFIG_VERSION,
            opds_url="",
            source_id="",
            source_name="",
            library_path="X:/Books",
            setup_complete=True,
            custom_test=123,
        )
        SETTINGS_MODULE.DESTINATION = "X:/Books"
        SETTINGS_MODULE.save_app_config = lambda config: None
        self.configure_calls = []

        def configure(url):
            self.configure_calls.append(url)
            if url == "https://invalid.example/catalog":
                return SETTINGS_MODULE.SourceValidationResult(
                    valid=False,
                    normalized_url=url,
                    final_url="",
                    title="",
                    error="internal validation detail",
                )
            SETTINGS_MODULE.APP_CONFIG.update(
                opds_url=url,
                source_id="sha256:configured-source",
                source_name="Example OPDS",
            )
            return SETTINGS_MODULE.SourceValidationResult(
                valid=True,
                normalized_url=url,
                final_url=url,
                title="Example OPDS",
                error="",
            )

        SETTINGS_MODULE.configure_opds_source = configure
        self.client = SETTINGS_MODULE.app.test_client()

    def test_a_get_returns_success(self):
        response = self.client.get("/settings/opds")
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            '<a class="button-link" href="/">← На главный экран</a>',
            response.get_data(as_text=True),
        )

    def test_b_get_performs_no_validation_or_network(self):
        def forbidden_call(*args, **kwargs):
            raise AssertionError("GET settings must not access the network")

        originals = {
            name: SETTINGS_MODULE.__dict__.get(name)
            for name in (
                "configure_opds_source",
                "OPDSHTTPClient",
                "load_current_opds_feed",
            )
        }
        for name in originals:
            SETTINGS_MODULE.__dict__[name] = forbidden_call
        try:
            response = self.client.get("/settings/opds")
        finally:
            for name, original in originals.items():
                if original is None:
                    SETTINGS_MODULE.__dict__.pop(name, None)
                else:
                    SETTINGS_MODULE.__dict__[name] = original
        self.assertEqual(response.status_code, 200)

    def test_c_unconfigured_source_has_empty_input(self):
        body = self.client.get("/settings/opds").get_data(as_text=True)
        self.assertIn('name="opds_url" value=""', body)

    def test_d_html_has_no_default_or_legacy_source(self):
        body = self.client.get("/settings/opds").get_data(as_text=True).lower()
        for forbidden in (
            "flibusta",
            "flibusta.is",
            "opds_base",
            "https://",
            "http://",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, body)

    def test_e_save_calls_configure_once_with_trimmed_url(self):
        response = self.client.post(
            "/settings/opds",
            data={
                "action": "save",
                "opds_url": "  https://catalog.example.org/root.xml  ",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            self.configure_calls,
            ["https://catalog.example.org/root.xml"],
        )

    def test_f_success_redirects_to_settings_get(self):
        response = self.client.post(
            "/settings/opds",
            data={"action": "save", "opds_url": "https://example.org/opds"},
        )
        self.assertEqual(response.headers["Location"], "/settings/opds?saved=1")

    def test_g_success_message_appears_after_redirect(self):
        response = self.client.post(
            "/settings/opds",
            data={"action": "save", "opds_url": "https://example.org/opds"},
            follow_redirects=True,
        )
        self.assertIn("OPDS-источник сохранён.", response.get_data(as_text=True))

    def test_h_empty_url_does_not_validate_or_change_config(self):
        snapshot = dict(SETTINGS_MODULE.APP_CONFIG)
        response = self.client.post(
            "/settings/opds",
            data={"action": "save", "opds_url": "   "},
        )
        self.assertEqual(self.configure_calls, [])
        self.assertEqual(SETTINGS_MODULE.APP_CONFIG, snapshot)
        self.assertIn(
            "Укажите адрес OPDS-каталога.",
            response.get_data(as_text=True),
        )
        self.assertIn(
            '<a class="button-link" href="/">← На главный экран</a>',
            response.get_data(as_text=True),
        )

    def test_i_validation_failure_preserves_existing_config(self):
        SETTINGS_MODULE.APP_CONFIG.update(
            opds_url="https://old.example/catalog",
            source_id="sha256:old-source",
            source_name="Old OPDS",
        )
        snapshot = dict(SETTINGS_MODULE.APP_CONFIG)
        response = self.client.post(
            "/settings/opds",
            data={
                "action": "save",
                "opds_url": "https://invalid.example/catalog",
            },
        )
        self.assertEqual(SETTINGS_MODULE.APP_CONFIG, snapshot)
        body = response.get_data(as_text=True)
        self.assertIn("Не удалось проверить OPDS-источник.", body)
        self.assertNotIn("internal validation detail", body)

    def test_j_clear_calls_helper_once_and_redirects(self):
        SETTINGS_MODULE.APP_CONFIG.update(
            opds_url="https://example.org/opds",
            source_id="sha256:source",
            source_name="Example OPDS",
        )
        original = SETTINGS_MODULE.clear_configured_opds_source
        calls = []

        def counted_clear():
            calls.append(True)
            return original()

        SETTINGS_MODULE.clear_configured_opds_source = counted_clear
        try:
            response = self.client.post(
                "/settings/opds",
                data={"action": "clear"},
            )
        finally:
            SETTINGS_MODULE.clear_configured_opds_source = original
        self.assertEqual(calls, [True])
        self.assertEqual(response.headers["Location"], "/settings/opds?cleared=1")
        self.assertEqual(self.configure_calls, [])

    def test_k_clear_preserves_non_source_config(self):
        SETTINGS_MODULE.APP_CONFIG.update(
            opds_url="https://example.org/opds",
            source_id="sha256:source",
            source_name="Example OPDS",
        )
        self.client.post("/settings/opds", data={"action": "clear"})
        self.assertEqual(SETTINGS_MODULE.APP_CONFIG["opds_url"], "")
        self.assertEqual(SETTINGS_MODULE.APP_CONFIG["source_id"], "")
        self.assertEqual(SETTINGS_MODULE.APP_CONFIG["source_name"], "")
        self.assertEqual(SETTINGS_MODULE.APP_CONFIG["library_path"], "X:/Books")
        self.assertIs(SETTINGS_MODULE.APP_CONFIG["setup_complete"], True)
        self.assertEqual(SETTINGS_MODULE.APP_CONFIG["custom_test"], 123)

    def test_l_configured_source_metadata_and_catalog_link_are_visible(self):
        SETTINGS_MODULE.APP_CONFIG.update(
            opds_url="https://catalog.example.org/root.xml",
            source_id="sha256:secret-source-id",
            source_name="Example Catalog",
        )
        body = self.client.get("/settings/opds").get_data(as_text=True)
        self.assertIn("Example Catalog", body)
        self.assertIn("https://catalog.example.org/root.xml", body)
        self.assertIn('href="/catalog/opds"', body)
        self.assertNotIn("sha256:secret-source-id", body)

    def test_m_source_fields_are_html_escaped(self):
        unsafe_name = "<script>alert(1)</script>"
        unsafe_url = "https://example.org/?q=<img src=x onerror=alert(2)>"
        SETTINGS_MODULE.APP_CONFIG.update(
            opds_url=unsafe_url,
            source_id="sha256:source",
            source_name=unsafe_name,
        )
        body = self.client.get("/settings/opds").get_data(as_text=True)
        self.assertNotIn(unsafe_name, body)
        self.assertNotIn("<img src=x onerror=alert(2)>", body)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", body)
        self.assertIn("&lt;img src=x onerror=alert(2)&gt;", body)

    def test_n_existing_settings_page_keeps_library_ui_and_adds_opds_link(self):
        body = self.client.get("/settings").get_data(as_text=True)
        self.assertIn("X:/Books", body)
        self.assertIn("chooseLibraryButton", body)
        self.assertIn('href="/settings/opds"', body)
        self.assertIn("Настройка OPDS", body)

    def test_o_setup_and_legacy_ui_are_not_connected_to_new_settings(self):
        self.assertNotIn("opds_settings_page", SETTINGS_MODULE.SETUP_HTML)
        protected_functions = {"index", "author_catalog", "series_catalog"}
        found = set()
        for node in SETTINGS_MODULE.__source_tree__.body:
            name = getattr(node, "name", None)
            if name not in protected_functions:
                continue
            found.add(name)
            called_names = {
                child.func.id
                for child in ast.walk(node)
                if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
            }
            self.assertNotIn("configure_opds_source", called_names)
        self.assertEqual(found, protected_functions)

    def test_p_new_template_and_route_have_no_forbidden_dependencies(self):
        route_node = next(
            node
            for node in SETTINGS_MODULE.__source_tree__.body
            if getattr(node, "name", None) == "opds_settings_page"
        )
        route_source = (
            ast.get_source_segment(SETTINGS_MODULE.__source_text__, route_node) or ""
        ).lower()
        template_source = SETTINGS_MODULE.OPDS_SETTINGS_HTML.lower()
        for forbidden in (
            "opds_base",
            "flibusta",
            "/b/",
            "/opds/search",
            "proxy",
            "socks",
            "xray",
            "tor",
            "queue",
            "save_epub",
            "save_fb2",
            "acquisition_links",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, route_source)
                self.assertNotIn(forbidden, template_source)
        self.assertNotIn("|safe", template_source)


if __name__ == "__main__":
    unittest.main()
