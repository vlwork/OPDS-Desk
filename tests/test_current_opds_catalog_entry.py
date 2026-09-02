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

from flask import Flask, redirect, render_template_string, url_for


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_entry_module():
    """Загружает OPDS entry route и registry без runtime приложения."""
    source = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    wanted_definitions = {
        "SourceConfig",
        "normalize_app_config",
        "source_config_from_app_config",
        "normalize_opds_url",
        "CatalogRef",
        "RegisteredCatalogRef",
        "make_catalog_ref_token",
        "register_catalog_ref",
        "get_catalog_ref",
        "get_current_catalog_ref",
        "clear_catalog_ref_registry",
        "current_source_config",
        "current_root_catalog_ref",
        "register_current_root_catalog",
        "has_configured_opds_source",
        "open_current_opds_catalog",
    }
    wanted_assignments = {
        "CONFIG_VERSION",
        "MAX_CATALOG_REF_REGISTRY",
        "catalog_ref_registry",
        "catalog_ref_registry_lock",
        "REGISTERED_CATALOG_HTML",
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

    module = types.ModuleType("isolated_current_opds_catalog_entry_test")
    sys.modules[module.__name__] = module
    app = Flask(module.__name__)

    @app.get("/")
    def index():
        return ""

    @app.get("/catalog/opds/<token>")
    def registered_catalog_page(token):
        return token

    module.__dict__.update(
        dataclass=dataclasses.dataclass,
        hashlib=hashlib,
        ipaddress=ipaddress,
        json=json,
        threading=threading,
        urlsplit=urlsplit,
        urlunsplit=urlunsplit,
        app=app,
        redirect=redirect,
        render_template_string=render_template_string,
        url_for=url_for,
        DEFAULT_DESTINATION="test-default-library",
        APP_CONFIG={},
    )
    exec(
        compile(ast.Module(body=body, type_ignores=[]), "app.py", "exec"),
        module.__dict__,
    )
    module.app.testing = True
    module.__source_text__ = source
    module.__source_tree__ = tree
    return module


ENTRY_MODULE = load_entry_module()


class CurrentOpdsCatalogEntryTests(unittest.TestCase):
    def setUp(self):
        ENTRY_MODULE.clear_catalog_ref_registry()
        ENTRY_MODULE.APP_CONFIG = {
            "config_version": ENTRY_MODULE.CONFIG_VERSION,
            "opds_url": "https://catalog.example.org/root.xml",
            "source_id": "sha256:source-a",
            "source_name": "Example OPDS",
            "library_path": "X:/Books",
            "setup_complete": True,
        }
        self.client = ENTRY_MODULE.app.test_client()

    def tearDown(self):
        ENTRY_MODULE.clear_catalog_ref_registry()

    def test_a_configured_source_redirects_to_registered_catalog(self):
        response = self.client.get("/catalog/opds")
        self.assertEqual(response.status_code, 302)
        self.assertRegex(
            response.headers["Location"],
            r"^/catalog/opds/catalog:[0-9a-f]{64}$",
        )

    def test_b_redirect_exposes_no_source_url_or_hostname(self):
        response = self.client.get("/catalog/opds")
        location = response.headers["Location"].lower()
        for forbidden in (
            ENTRY_MODULE.APP_CONFIG["opds_url"].lower(),
            "catalog.example.org",
            "http",
            "https",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, location)

    def test_c_root_registration_is_called_once(self):
        original = ENTRY_MODULE.register_current_root_catalog
        calls = []

        def counted_registration():
            calls.append(True)
            return original()

        ENTRY_MODULE.register_current_root_catalog = counted_registration
        try:
            response = self.client.get("/catalog/opds")
        finally:
            ENTRY_MODULE.register_current_root_catalog = original
        self.assertEqual(response.status_code, 302)
        self.assertEqual(calls, [True])

    def test_d_entry_route_does_not_call_network_or_view_builder(self):
        forbidden_names = (
            "load_current_opds_feed",
            "load_registered_catalog_page",
            "build_registered_catalog_view",
            "OPDSHTTPClient",
        )
        originals = {
            name: ENTRY_MODULE.__dict__.get(name)
            for name in forbidden_names
        }

        def forbidden_call(*args, **kwargs):
            raise AssertionError("entry route must not perform network or build a view")

        for name in forbidden_names:
            ENTRY_MODULE.__dict__[name] = forbidden_call
        try:
            response = self.client.get("/catalog/opds")
        finally:
            for name, original in originals.items():
                if original is None:
                    ENTRY_MODULE.__dict__.pop(name, None)
                else:
                    ENTRY_MODULE.__dict__[name] = original
        self.assertEqual(response.status_code, 302)

    def test_e_unconfigured_source_returns_neutral_response(self):
        ENTRY_MODULE.APP_CONFIG.update(
            opds_url="",
            source_id="",
            source_name="",
        )
        response = self.client.get("/catalog/opds")
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 409)
        self.assertIn("OPDS-источник не настроен.", body)
        self.assertNotIn("Location", response.headers)
        self.assertNotIn("flibusta", body.lower())

    def test_f_source_change_creates_new_token_and_rejects_old_one(self):
        response_a = self.client.get("/catalog/opds")
        token_a = response_a.headers["Location"].rsplit("/", 1)[-1]

        ENTRY_MODULE.APP_CONFIG.update(
            opds_url="https://books.example.net/catalog.xml",
            source_id="sha256:source-b",
            source_name="Other OPDS",
        )
        response_b = self.client.get("/catalog/opds")
        token_b = response_b.headers["Location"].rsplit("/", 1)[-1]

        self.assertNotEqual(token_a, token_b)
        self.assertIsNotNone(ENTRY_MODULE.get_catalog_ref(token_a))
        self.assertIsNone(ENTRY_MODULE.get_current_catalog_ref(token_a))
        self.assertIsNotNone(ENTRY_MODULE.get_current_catalog_ref(token_b))

    def test_g_opaque_token_is_not_converted_to_integer(self):
        original = ENTRY_MODULE.register_current_root_catalog
        token = "catalog:opaque-uuid-token"
        ENTRY_MODULE.register_current_root_catalog = lambda: token
        try:
            response = self.client.get("/catalog/opds")
        finally:
            ENTRY_MODULE.register_current_root_catalog = original
        self.assertEqual(response.headers["Location"], f"/catalog/opds/{token}")

    def test_h_route_is_get_only(self):
        response = self.client.post("/catalog/opds")
        self.assertEqual(response.status_code, 405)

    def test_i_get_does_not_mutate_app_config(self):
        snapshot = dict(ENTRY_MODULE.APP_CONFIG)
        identity = id(ENTRY_MODULE.APP_CONFIG)
        self.client.get("/catalog/opds")
        self.assertEqual(ENTRY_MODULE.APP_CONFIG, snapshot)
        self.assertEqual(id(ENTRY_MODULE.APP_CONFIG), identity)

    def test_j_invalid_source_error_is_sanitized(self):
        secret = "https://private.example.org/internal/path"
        original = ENTRY_MODULE.register_current_root_catalog
        ENTRY_MODULE.register_current_root_catalog = lambda: (
            (_ for _ in ()).throw(ValueError(secret))
        )
        try:
            response = self.client.get("/catalog/opds")
        finally:
            ENTRY_MODULE.register_current_root_catalog = original
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 409)
        self.assertIn("Не удалось открыть OPDS-каталог.", body)
        self.assertNotIn(secret, body)

    def test_k_entry_route_has_no_forbidden_dependencies(self):
        route_node = next(
            node
            for node in ENTRY_MODULE.__source_tree__.body
            if getattr(node, "name", None) == "open_current_opds_catalog"
        )
        route_source = (
            ast.get_source_segment(ENTRY_MODULE.__source_text__, route_node) or ""
        ).lower()
        for forbidden in (
            "opds_base",
            "flibusta",
            "requests",
            "opdshttpclient",
            "load_current_opds_feed",
            "queue",
            "downloader",
            "save_epub",
            "save_fb2",
            "acquisition",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, route_source)

    def test_l_legacy_routes_do_not_call_new_entry_route(self):
        protected_functions = {"author_catalog", "series_catalog"}
        found = set()
        for node in ENTRY_MODULE.__source_tree__.body:
            name = getattr(node, "name", None)
            if name not in protected_functions:
                continue
            found.add(name)
            called_names = {
                child.func.id
                for child in ast.walk(node)
                if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
            }
            self.assertNotIn("open_current_opds_catalog", called_names)
        self.assertEqual(found, protected_functions)


if __name__ == "__main__":
    unittest.main()
