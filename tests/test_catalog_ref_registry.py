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


def load_registry_module():
    """Загружает только нейтральный registry без runtime приложения."""
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
        "register_catalog_refs",
        "register_catalog_navigation",
        "current_source_config",
        "current_root_catalog_ref",
        "register_current_root_catalog",
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

    module = types.ModuleType("isolated_catalog_ref_registry_test")
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
    )
    exec(compile(ast.Module(body=body, type_ignores=[]), "app.py", "exec"), module.__dict__)
    module.__source_text__ = source
    module.__source_tree__ = tree
    return module


REGISTRY_MODULE = load_registry_module()


class CatalogRefRegistryTests(unittest.TestCase):
    def setUp(self):
        REGISTRY_MODULE.MAX_CATALOG_REF_REGISTRY = 4096
        REGISTRY_MODULE.clear_catalog_ref_registry()
        REGISTRY_MODULE.APP_CONFIG = {
            "config_version": REGISTRY_MODULE.CONFIG_VERSION,
            "opds_url": "",
            "source_id": "",
            "source_name": "",
            "library_path": "X:/Books",
            "setup_complete": True,
        }

    def tearDown(self):
        REGISTRY_MODULE.MAX_CATALOG_REF_REGISTRY = 4096
        REGISTRY_MODULE.clear_catalog_ref_registry()

    def make_ref(
        self,
        source_id="source-a",
        url="https://example.org/catalog",
        title="Catalog",
        kind="navigation",
    ):
        return REGISTRY_MODULE.CatalogRef(
            source_id=source_id,
            url=url,
            title=title,
            kind=kind,
        )

    def test_a_same_source_and_url_produce_same_token(self):
        first = REGISTRY_MODULE.make_catalog_ref_token(
            "source-a", "https://example.org/catalog"
        )
        second = REGISTRY_MODULE.make_catalog_ref_token(
            "source-a", "https://example.org/catalog"
        )
        self.assertEqual(first, second)
        self.assertRegex(first, r"^catalog:[0-9a-f]{64}$")

    def test_b_sources_are_isolated_for_the_same_url(self):
        first = REGISTRY_MODULE.make_catalog_ref_token(
            "source-a", "https://example.org/catalog"
        )
        second = REGISTRY_MODULE.make_catalog_ref_token(
            "source-b", "https://example.org/catalog"
        )
        self.assertNotEqual(first, second)

    def test_c_token_does_not_expose_url_or_hostname(self):
        original_url = "https://example.org/private/catalog"
        token = REGISTRY_MODULE.make_catalog_ref_token("source-a", original_url)
        self.assertNotIn("example.org", token)
        self.assertNotIn("https", token)
        self.assertNotIn(original_url, token)

    def test_d_register_and_get_return_the_catalog_ref(self):
        ref = self.make_ref()
        token = REGISTRY_MODULE.register_catalog_ref(ref)
        self.assertEqual(REGISTRY_MODULE.get_catalog_ref(token), ref)

    def test_e_registered_url_is_normalized(self):
        ref = self.make_ref(url="HTTPS://Example.org/catalog#fragment")
        token = REGISTRY_MODULE.register_catalog_ref(ref)
        stored = REGISTRY_MODULE.get_catalog_ref(token)
        self.assertEqual(stored.url, "https://example.org/catalog")

    def test_f_unknown_token_returns_none(self):
        self.assertIsNone(REGISTRY_MODULE.get_catalog_ref("catalog:unknown"))

    def test_g_registration_rejects_invalid_scheme(self):
        with self.assertRaises(ValueError):
            REGISTRY_MODULE.register_catalog_ref(self.make_ref(url="file:///catalog"))

    def test_h_current_root_catalog_ref_uses_only_configured_source(self):
        self.assertIsNone(REGISTRY_MODULE.current_root_catalog_ref())
        REGISTRY_MODULE.APP_CONFIG.update(
            opds_url="https://catalog.example.org/root.xml",
            source_id="sha256:source-test",
            source_name="Example OPDS",
        )
        ref = REGISTRY_MODULE.current_root_catalog_ref()
        self.assertEqual(
            ref,
            REGISTRY_MODULE.CatalogRef(
                source_id="sha256:source-test",
                url="https://catalog.example.org/root.xml",
                title="Example OPDS",
                kind="navigation",
            ),
        )
        token = REGISTRY_MODULE.register_current_root_catalog()
        self.assertEqual(REGISTRY_MODULE.get_catalog_ref(token), ref)

    def test_i_navigation_result_contains_no_url(self):
        result = REGISTRY_MODULE.register_catalog_navigation(
            (self.make_ref(), self.make_ref(url="https://example.org/other"))
        )
        self.assertEqual(len(result), 2)
        self.assertTrue(
            all(isinstance(item, REGISTRY_MODULE.RegisteredCatalogRef) for item in result)
        )
        self.assertEqual(
            {field.name for field in dataclasses.fields(REGISTRY_MODULE.RegisteredCatalogRef)},
            {"token", "title", "kind"},
        )
        self.assertTrue(all(not hasattr(item, "url") for item in result))

    def test_j_registry_limit_evicts_oldest_entries(self):
        REGISTRY_MODULE.MAX_CATALOG_REF_REGISTRY = 3
        tokens = [
            REGISTRY_MODULE.register_catalog_ref(
                self.make_ref(url=f"https://example.org/catalog/{index}")
            )
            for index in range(5)
        ]
        self.assertEqual(len(REGISTRY_MODULE.catalog_ref_registry), 3)
        self.assertIsNone(REGISTRY_MODULE.get_catalog_ref(tokens[0]))
        self.assertIsNone(REGISTRY_MODULE.get_catalog_ref(tokens[1]))
        self.assertIsNotNone(REGISTRY_MODULE.get_catalog_ref(tokens[-1]))

    def test_k_clear_registry_does_not_change_app_config(self):
        REGISTRY_MODULE.APP_CONFIG["custom_test"] = 123
        snapshot = dict(REGISTRY_MODULE.APP_CONFIG)
        REGISTRY_MODULE.register_catalog_ref(self.make_ref())
        REGISTRY_MODULE.clear_catalog_ref_registry()
        self.assertEqual(REGISTRY_MODULE.APP_CONFIG, snapshot)
        self.assertEqual(REGISTRY_MODULE.catalog_ref_registry, {})

    def test_l_uuid_and_string_source_ids_need_no_integer_conversion(self):
        url = "https://example.org/catalog"
        source_ids = (
            "550e8400-e29b-41d4-a716-446655440000",
            "sha256:opaque-source",
            "source/name:value",
        )
        tokens = [
            REGISTRY_MODULE.make_catalog_ref_token(source_id, url)
            for source_id in source_ids
        ]
        self.assertEqual(len(tokens), len(set(tokens)))
        self.assertTrue(all(token.startswith("catalog:") for token in tokens))

    def test_m_current_source_can_resolve_its_registered_ref(self):
        ref = self.make_ref(source_id="source-a")
        token = REGISTRY_MODULE.register_catalog_ref(ref)
        REGISTRY_MODULE.APP_CONFIG.update(
            opds_url="https://source-a.example.org/root.xml",
            source_id="source-a",
        )
        self.assertEqual(REGISTRY_MODULE.get_current_catalog_ref(token), ref)

    def test_n_source_switch_hides_old_ref_but_keeps_low_level_entry(self):
        ref = self.make_ref(source_id="source-a")
        token = REGISTRY_MODULE.register_catalog_ref(ref)
        REGISTRY_MODULE.APP_CONFIG.update(
            opds_url="https://source-a.example.org/root.xml",
            source_id="source-a",
        )
        self.assertEqual(REGISTRY_MODULE.get_current_catalog_ref(token), ref)

        REGISTRY_MODULE.APP_CONFIG.update(
            opds_url="https://source-b.example.org/root.xml",
            source_id="source-b",
        )
        self.assertEqual(REGISTRY_MODULE.get_catalog_ref(token), ref)
        self.assertIsNone(REGISTRY_MODULE.get_current_catalog_ref(token))

    def test_o_empty_current_source_hides_registered_ref(self):
        token = REGISTRY_MODULE.register_catalog_ref(self.make_ref())
        self.assertIsNone(REGISTRY_MODULE.get_current_catalog_ref(token))

    def test_p_unknown_current_catalog_token_returns_none(self):
        REGISTRY_MODULE.APP_CONFIG.update(
            opds_url="https://source-a.example.org/root.xml",
            source_id="source-a",
        )
        self.assertIsNone(
            REGISTRY_MODULE.get_current_catalog_ref("catalog:unknown")
        )

    def test_q_current_lookup_has_no_numeric_id_assumptions(self):
        source_id = "550e8400-e29b-41d4-a716-446655440000/source"
        ref = self.make_ref(source_id=source_id)
        token = REGISTRY_MODULE.register_catalog_ref(ref)
        REGISTRY_MODULE.APP_CONFIG.update(
            opds_url="https://uuid-source.example.org/root.xml",
            source_id=source_id,
        )
        self.assertEqual(REGISTRY_MODULE.get_current_catalog_ref(token), ref)

    def test_r_generic_registration_returns_safe_refs_in_input_order(self):
        refs = (
            self.make_ref(
                url="HTTPS://Example.org/first#fragment",
                title="First related catalog",
                kind="related",
            ),
            self.make_ref(
                url="https://example.org/second",
                title="Second related catalog",
                kind="related",
            ),
        )
        registered = REGISTRY_MODULE.register_catalog_refs(refs)
        self.assertIsInstance(registered, tuple)
        self.assertTrue(
            all(
                isinstance(item, REGISTRY_MODULE.RegisteredCatalogRef)
                for item in registered
            )
        )
        self.assertEqual(
            [(item.title, item.kind) for item in registered],
            [
                ("First related catalog", "related"),
                ("Second related catalog", "related"),
            ],
        )
        for ref, item in zip(refs, registered):
            self.assertTrue(item.token)
            self.assertNotEqual(item.token, ref.url)
            self.assertNotIn(ref.url, item.token)
            stored = REGISTRY_MODULE.get_catalog_ref(item.token)
            self.assertEqual(stored.source_id, ref.source_id)
            self.assertEqual(stored.title, ref.title)
            self.assertEqual(stored.kind, ref.kind)
        self.assertEqual(
            REGISTRY_MODULE.get_catalog_ref(registered[0].token).url,
            "https://example.org/first",
        )
        self.assertEqual(
            {field.name for field in dataclasses.fields(registered[0])},
            {"token", "title", "kind"},
        )

    def test_s_generic_registration_empty_input_returns_empty_tuple(self):
        self.assertEqual(REGISTRY_MODULE.register_catalog_refs(()), ())

    def test_t_generic_registration_rejects_non_catalog_ref(self):
        with self.assertRaises(TypeError):
            REGISTRY_MODULE.register_catalog_refs(
                ({"url": "https://example.org/not-a-ref"},)
            )

    def test_u_navigation_registration_uses_generic_helper(self):
        refs = (self.make_ref(),)
        self.assertEqual(
            REGISTRY_MODULE.register_catalog_navigation(refs),
            REGISTRY_MODULE.register_catalog_refs(refs),
        )
        navigation_node = next(
            node
            for node in REGISTRY_MODULE.__source_tree__.body
            if getattr(node, "name", None) == "register_catalog_navigation"
        )
        called_names = {
            child.func.id
            for child in ast.walk(navigation_node)
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
        }
        self.assertEqual(called_names, {"register_catalog_refs"})

    def test_v_generic_helper_has_no_network_or_config_dependencies(self):
        helper_node = next(
            node
            for node in REGISTRY_MODULE.__source_tree__.body
            if getattr(node, "name", None) == "register_catalog_refs"
        )
        source = (
            ast.get_source_segment(REGISTRY_MODULE.__source_text__, helper_node) or ""
        ).lower()
        self.assertIn("register_catalog_ref(", source)
        for forbidden in (
            "current_source_config",
            "requests",
            "fetch(",
            "load_opds_catalog_page",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
