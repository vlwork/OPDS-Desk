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


def load_source_config_module():
    """Загружает только нейтральный config-слой без runtime приложения."""
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
    module = types.ModuleType("isolated_source_config_test")
    sys.modules[module.__name__] = module
    module.__dict__.update(
        dataclass=dataclasses.dataclass,
        hashlib=hashlib,
        ipaddress=ipaddress,
        DEFAULT_DESTINATION="test-default-library",
        urlsplit=urlsplit,
        urlunsplit=urlunsplit,
    )
    exec(compile(ast.Module(body=body, type_ignores=[]), "app.py", "exec"), module.__dict__)
    return module


SOURCE_MODULE = load_source_config_module()


def successful_validation(
    normalized_url="https://example.org/opds",
    final_url="https://catalog.example.org/feed.xml",
    title="Example catalog",
):
    return SOURCE_MODULE.SourceValidationResult(
        valid=True,
        normalized_url=normalized_url,
        final_url=final_url,
        title=title,
        error="",
    )


class SourceConfigTests(unittest.TestCase):
    def test_a_equivalent_normalized_urls_have_same_source_id(self):
        first = SOURCE_MODULE.make_source_id("HTTPS://Example.org/opds#fragment")
        second = SOURCE_MODULE.make_source_id("https://example.org/opds")
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("sha256:"))
        self.assertEqual(len(first), len("sha256:") + 64)

    def test_b_different_normalized_urls_have_different_source_ids(self):
        first = SOURCE_MODULE.make_source_id("https://example.org/opds")
        second = SOURCE_MODULE.make_source_id("https://example.org/catalog")
        self.assertNotEqual(first, second)

    def test_c_build_source_config_normalizes_and_trims(self):
        source = SOURCE_MODULE.build_source_config(
            " HTTPS://Example.org/opds#fragment ",
            "  Example catalog  ",
        )
        self.assertEqual(source.root_url, "https://example.org/opds")
        self.assertEqual(
            source.source_id,
            SOURCE_MODULE.make_source_id("https://example.org/opds"),
        )
        self.assertEqual(source.display_name, "Example catalog")

    def test_d_apply_validated_source_uses_final_url_and_preserves_config(self):
        original = {
            "library_path": "X:/Books",
            "setup_complete": True,
            "custom_test": 123,
        }
        result = SOURCE_MODULE.apply_validated_source(
            original,
            successful_validation(),
        )
        final_url = "https://catalog.example.org/feed.xml"
        self.assertEqual(result["config_version"], SOURCE_MODULE.CONFIG_VERSION)
        self.assertEqual(result["opds_url"], final_url)
        self.assertEqual(result["source_id"], SOURCE_MODULE.make_source_id(final_url))
        self.assertEqual(result["source_name"], "Example catalog")
        self.assertEqual(result["library_path"], "X:/Books")
        self.assertIs(result["setup_complete"], True)
        self.assertEqual(result["custom_test"], 123)

    def test_e_invalid_validation_is_rejected_without_mutation(self):
        original = {"library_path": "X:/Books", "custom_test": 123}
        snapshot = dict(original)
        validation = SOURCE_MODULE.SourceValidationResult(
            valid=False,
            normalized_url="https://example.org/opds",
            final_url="",
            title="",
            error="Invalid feed",
        )
        with self.assertRaises(ValueError):
            SOURCE_MODULE.apply_validated_source(original, validation)
        self.assertEqual(original, snapshot)

    def test_f_empty_final_url_uses_normalized_url(self):
        validation = successful_validation(
            normalized_url="HTTPS://Example.org/opds#fragment",
            final_url="",
        )
        result = SOURCE_MODULE.apply_validated_source({}, validation)
        self.assertEqual(result["opds_url"], "https://example.org/opds")
        self.assertEqual(
            result["source_id"],
            SOURCE_MODULE.make_source_id("https://example.org/opds"),
        )

    def test_g_legacy_config_stays_empty_without_builtin_source(self):
        result = SOURCE_MODULE.normalize_app_config(
            {"library_path": "X:/Books", "setup_complete": True}
        )
        self.assertEqual(result["opds_url"], "")
        self.assertEqual(result["source_id"], "")
        self.assertEqual(result["source_name"], "")

    def test_h_apply_validated_source_returns_new_dict(self):
        original = {
            "config_version": 7,
            "library_path": "X:/Books",
            "setup_complete": False,
            "custom_test": {"nested": "unchanged"},
        }
        snapshot = dict(original)
        result = SOURCE_MODULE.apply_validated_source(
            original,
            successful_validation(title="  Trimmed title  "),
        )
        self.assertIsNot(result, original)
        self.assertEqual(original, snapshot)
        self.assertEqual(result["config_version"], 7)
        self.assertEqual(result["library_path"], original["library_path"])
        self.assertEqual(result["setup_complete"], original["setup_complete"])
        self.assertEqual(result["custom_test"], original["custom_test"])
        self.assertEqual(result["source_name"], "Trimmed title")


if __name__ == "__main__":
    unittest.main()
