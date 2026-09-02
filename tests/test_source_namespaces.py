import ast
import hashlib
import sys
import types
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_namespace_module():
    """Загружает только нейтральные helpers ключей без runtime приложения."""
    source = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    wanted = {
        "source_namespace",
        "_opaque_key_part",
        "catalog_cache_key",
        "catalog_selection_storage_key",
    }
    body = [node for node in tree.body if getattr(node, "name", None) in wanted]
    module = types.ModuleType("isolated_source_namespaces_test")
    sys.modules[module.__name__] = module
    module.__dict__["hashlib"] = hashlib
    exec(compile(ast.Module(body=body, type_ignores=[]), "app.py", "exec"), module.__dict__)
    module.__source_text__ = source
    module.__source_tree__ = tree
    return module


NAMESPACE_MODULE = load_namespace_module()


class SourceNamespaceTests(unittest.TestCase):
    def test_a_same_inputs_produce_same_keys(self):
        cache_first = NAMESPACE_MODULE.catalog_cache_key(
            "sha256:source-a",
            "author",
            "urn:catalog:item",
        )
        cache_second = NAMESPACE_MODULE.catalog_cache_key(
            "sha256:source-a",
            "author",
            "urn:catalog:item",
        )
        storage_first = NAMESPACE_MODULE.catalog_selection_storage_key(
            "sha256:source-a",
            "author",
            "urn:catalog:item",
        )
        storage_second = NAMESPACE_MODULE.catalog_selection_storage_key(
            "sha256:source-a",
            "author",
            "urn:catalog:item",
        )
        self.assertEqual(cache_first, cache_second)
        self.assertEqual(storage_first, storage_second)

    def test_b_different_sources_produce_different_keys(self):
        first = NAMESPACE_MODULE.catalog_cache_key(
            "sha256:source-a",
            "series",
            "same-catalog-id",
        )
        second = NAMESPACE_MODULE.catalog_cache_key(
            "sha256:source-b",
            "series",
            "same-catalog-id",
        )
        self.assertNotEqual(first, second)

    def test_c_opaque_non_numeric_catalog_ids_are_supported(self):
        catalog_ids = (
            "8a79d4d4-d228-4f9c-b1b0-3f12fd6787f7",
            "urn:isbn:9780000000001",
            "tag:catalog.example,2026:item/alpha?edition=2",
        )
        keys = {
            NAMESPACE_MODULE.catalog_cache_key("sha256:source", "custom-kind", value)
            for value in catalog_ids
        }
        self.assertEqual(len(keys), len(catalog_ids))

    def test_d_empty_source_uses_legacy_namespace(self):
        self.assertEqual(NAMESPACE_MODULE.source_namespace(""), "legacy")
        self.assertEqual(NAMESPACE_MODULE.source_namespace("   "), "legacy")
        self.assertIn(
            ":legacy:",
            NAMESPACE_MODULE.catalog_cache_key("", "author", "catalog"),
        )

    def test_e_storage_key_is_safe_and_does_not_expose_url(self):
        source_url = "https://user:secret@example.org/opds"
        source_id = "sha256:" + hashlib.sha256(source_url.encode("utf-8")).hexdigest()
        key = NAMESPACE_MODULE.catalog_selection_storage_key(
            source_id,
            "https://example.org/kind",
            "https://example.org/catalog?id=1",
        )
        self.assertNotIn(source_url, key)
        self.assertNotIn("https://", key)
        self.assertNotIn("user", key)
        self.assertNotIn("secret", key)
        self.assertTrue(all(char.isalnum() or char in ":-" for char in key))

    def test_f_namespace_does_not_expose_source_id_origin(self):
        source_id = "sha256:" + "a" * 64
        namespace = NAMESPACE_MODULE.source_namespace(source_id)
        self.assertTrue(namespace.startswith("source-"))
        self.assertNotEqual(namespace, source_id)
        self.assertNotIn(":", namespace)
        self.assertNotIn("http", namespace)
        self.assertNotIn("@", namespace)

    def test_g_helpers_have_no_runtime_source_dependency(self):
        wanted = {
            "source_namespace",
            "_opaque_key_part",
            "catalog_cache_key",
            "catalog_selection_storage_key",
        }
        helper_nodes = [
            node
            for node in NAMESPACE_MODULE.__source_tree__.body
            if getattr(node, "name", None) in wanted
        ]
        names = {
            child.id
            for node in helper_nodes
            for child in ast.walk(node)
            if isinstance(child, ast.Name)
        }
        source_text = "\n".join(
            ast.get_source_segment(NAMESPACE_MODULE.__source_text__, node) or ""
            for node in helper_nodes
        ).lower()
        self.assertNotIn("LEGACY_OPDS_BASE", names)
        self.assertNotIn("http://", source_text)
        self.assertNotIn("https://", source_text)


if __name__ == "__main__":
    unittest.main()
