import ast
import dataclasses
import hashlib
import ipaddress
import sys
import types
import unittest
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_configuration_module():
    """Загружает backend настройки источника без runtime приложения."""
    source = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    wanted = {
        "SourceConfig",
        "SourceValidationResult",
        "normalize_app_config",
        "source_config_from_app_config",
        "normalize_opds_url",
        "make_source_id",
        "build_source_config",
        "apply_validated_source",
        "current_source_config",
        "validate_user_opds_url",
        "_save_and_replace_app_config",
        "save_validated_opds_source",
        "configure_opds_source",
        "clear_configured_opds_source",
    }
    body = []
    for node in tree.body:
        if getattr(node, "name", None) in wanted:
            body.append(node)
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "CONFIG_VERSION"
            for target in node.targets
        ):
            body.append(node)
    module = types.ModuleType("isolated_opds_source_configuration_test")
    sys.modules[module.__name__] = module
    module.__dict__.update(
        dataclass=dataclasses.dataclass,
        hashlib=hashlib,
        ipaddress=ipaddress,
        DEFAULT_DESTINATION="test-default-library",
        APP_CONFIG={},
        urlsplit=urlsplit,
        urlunsplit=urlunsplit,
    )
    exec(compile(ast.Module(body=body, type_ignores=[]), "app.py", "exec"), module.__dict__)
    module.__source_text__ = source
    module.__source_tree__ = tree
    return module


CONFIG_MODULE = load_configuration_module()


def validation_result(valid=True):
    return CONFIG_MODULE.SourceValidationResult(
        valid=valid,
        normalized_url="https://example.org/opds",
        final_url="https://catalog.example.org/feed.xml" if valid else "",
        title="Example catalog" if valid else "",
        error="" if valid else "Invalid source",
    )


class OPDSSourceConfigurationTests(unittest.TestCase):
    def setUp(self):
        CONFIG_MODULE.APP_CONFIG = {
            "config_version": CONFIG_MODULE.CONFIG_VERSION,
            "opds_url": "",
            "source_id": "",
            "source_name": "",
            "library_path": "X:/Books",
            "setup_complete": True,
            "custom_test": 123,
        }
        self.saved_configs = []

        def fake_save(config):
            self.saved_configs.append(dict(config))

        CONFIG_MODULE.save_app_config = fake_save

    def test_a_validation_does_not_change_or_save_config(self):
        expected = validation_result(valid=False)
        client = object()
        calls = []

        def fake_validate(url, client=None):
            calls.append((url, client))
            return expected

        CONFIG_MODULE.validate_opds_source = fake_validate
        snapshot = dict(CONFIG_MODULE.APP_CONFIG)
        result = CONFIG_MODULE.validate_user_opds_url(
            "https://example.org/opds",
            client,
        )
        self.assertIs(result, expected)
        self.assertEqual(calls, [("https://example.org/opds", client)])
        self.assertEqual(CONFIG_MODULE.APP_CONFIG, snapshot)
        self.assertEqual(self.saved_configs, [])

    def test_b_invalid_configuration_is_not_saved(self):
        expected = validation_result(valid=False)
        CONFIG_MODULE.validate_opds_source = lambda url, client=None: expected
        snapshot = dict(CONFIG_MODULE.APP_CONFIG)
        result = CONFIG_MODULE.configure_opds_source(
            "https://example.org/opds",
            object(),
        )
        self.assertIs(result, expected)
        self.assertEqual(self.saved_configs, [])
        self.assertEqual(CONFIG_MODULE.APP_CONFIG, snapshot)

    def test_c_valid_configuration_saves_final_url_and_preserves_fields(self):
        expected = validation_result(valid=True)
        CONFIG_MODULE.validate_opds_source = lambda url, client=None: expected
        identity = CONFIG_MODULE.APP_CONFIG
        result = CONFIG_MODULE.configure_opds_source(
            "https://example.org/opds",
            object(),
        )
        final_url = "https://catalog.example.org/feed.xml"
        self.assertIs(result, expected)
        self.assertEqual(len(self.saved_configs), 1)
        self.assertEqual(CONFIG_MODULE.APP_CONFIG["opds_url"], final_url)
        self.assertEqual(
            CONFIG_MODULE.APP_CONFIG["source_id"],
            CONFIG_MODULE.make_source_id(final_url),
        )
        self.assertEqual(CONFIG_MODULE.APP_CONFIG["source_name"], "Example catalog")
        self.assertEqual(CONFIG_MODULE.APP_CONFIG["library_path"], "X:/Books")
        self.assertIs(CONFIG_MODULE.APP_CONFIG["setup_complete"], True)
        self.assertEqual(CONFIG_MODULE.APP_CONFIG["custom_test"], 123)
        self.assertIs(CONFIG_MODULE.APP_CONFIG, identity)

    def test_d_invalid_result_cannot_be_saved(self):
        invalid = validation_result(valid=False)
        snapshot = dict(CONFIG_MODULE.APP_CONFIG)
        with self.assertRaises(ValueError):
            CONFIG_MODULE.save_validated_opds_source(invalid)
        self.assertEqual(self.saved_configs, [])
        self.assertEqual(CONFIG_MODULE.APP_CONFIG, snapshot)

    def test_e_save_failure_leaves_app_config_unchanged(self):
        def failing_save(config):
            raise OSError("disk error")

        CONFIG_MODULE.save_app_config = failing_save
        identity = CONFIG_MODULE.APP_CONFIG
        snapshot = dict(CONFIG_MODULE.APP_CONFIG)
        with self.assertRaises(OSError):
            CONFIG_MODULE.save_validated_opds_source(validation_result(valid=True))
        self.assertIs(CONFIG_MODULE.APP_CONFIG, identity)
        self.assertEqual(CONFIG_MODULE.APP_CONFIG, snapshot)

    def test_f_clear_source_preserves_unrelated_config(self):
        CONFIG_MODULE.APP_CONFIG.update(
            opds_url="https://catalog.example.org/feed.xml",
            source_id="sha256:source-id",
            source_name="Example catalog",
        )
        identity = CONFIG_MODULE.APP_CONFIG
        result = CONFIG_MODULE.clear_configured_opds_source()
        self.assertEqual(result["opds_url"], "")
        self.assertEqual(result["source_id"], "")
        self.assertEqual(result["source_name"], "")
        self.assertEqual(CONFIG_MODULE.APP_CONFIG["library_path"], "X:/Books")
        self.assertIs(CONFIG_MODULE.APP_CONFIG["setup_complete"], True)
        self.assertEqual(CONFIG_MODULE.APP_CONFIG["custom_test"], 123)
        self.assertIs(CONFIG_MODULE.APP_CONFIG, identity)
        self.assertEqual(self.saved_configs, [result])

    def test_g_failed_clear_leaves_app_config_unchanged(self):
        CONFIG_MODULE.APP_CONFIG.update(
            opds_url="https://catalog.example.org/feed.xml",
            source_id="sha256:source-id",
            source_name="Example catalog",
        )

        def failing_save(config):
            raise OSError("disk error")

        CONFIG_MODULE.save_app_config = failing_save
        identity = CONFIG_MODULE.APP_CONFIG
        snapshot = dict(CONFIG_MODULE.APP_CONFIG)
        with self.assertRaises(OSError):
            CONFIG_MODULE.clear_configured_opds_source()
        self.assertIs(CONFIG_MODULE.APP_CONFIG, identity)
        self.assertEqual(CONFIG_MODULE.APP_CONFIG, snapshot)

    def test_h_helpers_have_no_builtin_source_fallback(self):
        CONFIG_MODULE.clear_configured_opds_source()
        source = CONFIG_MODULE.current_source_config()
        self.assertEqual(source.root_url, "")
        self.assertEqual(source.source_id, "")
        wanted = {
            "validate_user_opds_url",
            "_save_and_replace_app_config",
            "save_validated_opds_source",
            "configure_opds_source",
            "clear_configured_opds_source",
        }
        helper_source = "\n".join(
            ast.get_source_segment(CONFIG_MODULE.__source_text__, node) or ""
            for node in CONFIG_MODULE.__source_tree__.body
            if getattr(node, "name", None) in wanted
        ).lower()
        self.assertNotIn("opds_base", helper_source)
        self.assertNotIn("flibusta", helper_source)
        self.assertNotIn("http://", helper_source)
        self.assertNotIn("https://", helper_source)

    def test_i_app_config_identity_is_preserved_on_save_and_clear(self):
        identity = CONFIG_MODULE.APP_CONFIG
        CONFIG_MODULE.save_validated_opds_source(validation_result(valid=True))
        self.assertIs(CONFIG_MODULE.APP_CONFIG, identity)
        CONFIG_MODULE.clear_configured_opds_source()
        self.assertIs(CONFIG_MODULE.APP_CONFIG, identity)

    def test_j_protected_legacy_functions_remain_present(self):
        protected = {
            "catalog_start_url",
            "load_catalog_page",
            "collect_catalog",
            "save_epub",
            "save_fb2",
        }
        found = {
            node.name
            for node in CONFIG_MODULE.__source_tree__.body
            if getattr(node, "name", None) in protected
        }
        self.assertEqual(found, protected)

    def test_k_commit_normalizes_partial_config_and_preserves_identity(self):
        identity = CONFIG_MODULE.APP_CONFIG
        result = CONFIG_MODULE._save_and_replace_app_config({"custom_test": 456})
        self.assertIs(CONFIG_MODULE.APP_CONFIG, identity)
        self.assertEqual(CONFIG_MODULE.APP_CONFIG, result)
        self.assertEqual(self.saved_configs, [result])
        self.assertEqual(result["config_version"], CONFIG_MODULE.CONFIG_VERSION)
        self.assertEqual(result["opds_url"], "")
        self.assertEqual(result["source_id"], "")
        self.assertEqual(result["source_name"], "")
        self.assertEqual(result["library_path"], "test-default-library")
        self.assertIs(result["setup_complete"], False)
        self.assertEqual(result["custom_test"], 456)


if __name__ == "__main__":
    unittest.main()
