import ast
import dataclasses
import hashlib
import sys
import threading
import time
import types
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_stage1_namespace_module():
    """Загружает только source-aware ключи Stage 1 без runtime приложения."""
    source = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    wanted = {
        "SourceConfig",
        "normalize_app_config",
        "source_config_from_app_config",
        "source_namespace",
        "_opaque_key_part",
        "catalog_cache_key",
        "catalog_selection_storage_key",
        "current_source_config",
        "current_source_id",
        "catalog_page_cache_key",
        "catalog_selection_clear_token",
        "get_cached_catalog",
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
    module = types.ModuleType("isolated_stage1_namespace_test")
    sys.modules[module.__name__] = module
    module.__dict__.update(
        dataclass=dataclasses.dataclass,
        hashlib=hashlib,
        DEFAULT_DESTINATION="test-default-library",
        APP_CONFIG={},
    )
    exec(compile(ast.Module(body=body, type_ignores=[]), "app.py", "exec"), module.__dict__)
    module.__source_text__ = source
    module.__source_tree__ = tree
    return module


STAGE1_MODULE = load_stage1_namespace_module()


class Stage1SourceNamespaceTests(unittest.TestCase):
    def setUp(self):
        STAGE1_MODULE.APP_CONFIG = {
            "config_version": STAGE1_MODULE.CONFIG_VERSION,
            "opds_url": "",
            "source_id": "",
            "source_name": "",
            "library_path": "X:/Books",
            "setup_complete": True,
        }

    def test_a_legacy_page_cache_key_is_stable(self):
        first = STAGE1_MODULE.catalog_page_cache_key(
            "author",
            "urn:catalog:legacy",
            3,
        )
        second = STAGE1_MODULE.catalog_page_cache_key(
            "author",
            "urn:catalog:legacy",
            3,
        )
        self.assertEqual(first, second)
        self.assertIn(":legacy:", first[0])
        self.assertEqual(first[1], 3)

    def test_b_page_cache_keys_differ_between_sources(self):
        STAGE1_MODULE.APP_CONFIG["opds_url"] = "https://example.org/opds"
        STAGE1_MODULE.APP_CONFIG["source_id"] = "sha256:source-a"
        first = STAGE1_MODULE.catalog_page_cache_key("series", "same-id", 2)
        STAGE1_MODULE.APP_CONFIG["source_id"] = "sha256:source-b"
        second = STAGE1_MODULE.catalog_page_cache_key("series", "same-id", 2)
        self.assertNotEqual(first, second)
        self.assertEqual(first[1], second[1])

    def test_c_full_catalog_cache_keys_do_not_overlap(self):
        first = STAGE1_MODULE.catalog_cache_key(
            "sha256:source-a",
            "author",
            "same-id",
        )
        second = STAGE1_MODULE.catalog_cache_key(
            "sha256:source-b",
            "author",
            "same-id",
        )
        self.assertNotEqual(first, second)
        get_cached_catalog = next(
            node
            for node in STAGE1_MODULE.__source_tree__.body
            if getattr(node, "name", None) == "get_cached_catalog"
        )
        called_names = {
            child.func.id
            for child in ast.walk(get_cached_catalog)
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
        }
        self.assertIn("catalog_cache_key", called_names)
        self.assertIn("current_source_id", called_names)

    def test_d_selection_storage_key_is_source_aware_and_neutral(self):
        source_url = "https://user:secret@example.org/opds"
        first = STAGE1_MODULE.catalog_selection_storage_key(
            "sha256:source-a",
            "author",
            "same-id",
        )
        second = STAGE1_MODULE.catalog_selection_storage_key(
            "sha256:source-b",
            "author",
            "same-id",
        )
        self.assertNotEqual(first, second)
        self.assertNotIn(source_url, first)
        self.assertNotIn("flibusta", first.lower())

    def test_e_clear_selection_tokens_differ_between_sources(self):
        STAGE1_MODULE.APP_CONFIG["opds_url"] = "https://example.org/opds"
        STAGE1_MODULE.APP_CONFIG["source_id"] = "sha256:source-a"
        first = STAGE1_MODULE.catalog_selection_clear_token("author", "same-id")
        STAGE1_MODULE.APP_CONFIG["source_id"] = "sha256:source-b"
        second = STAGE1_MODULE.catalog_selection_clear_token("author", "same-id")
        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("clear:opds-selection:"))

    def test_f_kind_and_catalog_id_remain_opaque_strings(self):
        catalog_ids = (
            "8a79d4d4-d228-4f9c-b1b0-3f12fd6787f7",
            "urn:catalog:item/alpha",
            "tag:catalog.example,2026:item?edition=2",
        )
        keys = {
            STAGE1_MODULE.catalog_page_cache_key("custom-kind", catalog_id, 0)
            for catalog_id in catalog_ids
        }
        self.assertEqual(len(keys), len(catalog_ids))

    def test_g_template_receives_python_storage_key(self):
        source = STAGE1_MODULE.__source_text__
        self.assertIn(
            "const selectionStorageKey={{ selection_storage_key|tojson }};",
            source,
        )
        self.assertNotIn("'flibusta-selection:'", source)
        render_catalog = next(
            node
            for node in STAGE1_MODULE.__source_tree__.body
            if getattr(node, "name", None) == "render_catalog"
        )
        rendered_source = ast.get_source_segment(source, render_catalog) or ""
        self.assertIn("catalog_selection_storage_key", rendered_source)
        self.assertIn("selection_storage_key=selection_storage_key", rendered_source)

    def test_h_protected_stage1_elements_remain_present(self):
        source = STAGE1_MODULE.__source_text__
        for marker in (
            "catalog_page_cache",
            "catalog_page_cache_lock",
            "load_catalog_page",
            "get_cached_catalog_page",
            "collect_catalog",
            "get_cached_catalog",
            "sessionStorage",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, source)
        self.assertIn("view_all", source)
        self.assertTrue("view='all'" in source or 'view="all"' in source)

    def test_i_numeric_legacy_catalog_id_is_supported_at_runtime_boundaries(self):
        STAGE1_MODULE.APP_CONFIG["opds_url"] = "https://example.org/opds"
        STAGE1_MODULE.APP_CONFIG["source_id"] = "sha256:source-a"
        page_first = STAGE1_MODULE.catalog_page_cache_key("author", 123, 2)
        page_second = STAGE1_MODULE.catalog_page_cache_key("author", 123, 2)
        selection_first = STAGE1_MODULE.catalog_selection_storage_key(
            STAGE1_MODULE.current_source_id(),
            str("author"),
            str(123),
        )
        selection_second = STAGE1_MODULE.catalog_selection_storage_key(
            STAGE1_MODULE.current_source_id(),
            str("author"),
            str(123),
        )
        clear_first = STAGE1_MODULE.catalog_selection_clear_token("author", 123)
        clear_second = STAGE1_MODULE.catalog_selection_clear_token("author", 123)

        full_key_first = STAGE1_MODULE.catalog_cache_key(
            STAGE1_MODULE.current_source_id(),
            "author",
            "123",
        )
        cached_result = {"title": "Cached", "books": []}
        STAGE1_MODULE.catalog_cache = {
            full_key_first: {"time": time.time(), "result": cached_result}
        }
        STAGE1_MODULE.catalog_lock = threading.Lock()
        STAGE1_MODULE.CATALOG_CACHE_TTL = 900
        STAGE1_MODULE.time = time
        STAGE1_MODULE.apply_local_status = lambda book: book
        STAGE1_MODULE.annotate_duplicates = lambda books: (0, 0)
        STAGE1_MODULE.collect_catalog = lambda kind, catalog_id: self.fail(
            "full catalog cache unexpectedly missed"
        )
        self.assertIs(
            STAGE1_MODULE.get_cached_catalog("author", 123),
            cached_result,
        )

        self.assertEqual(page_first, page_second)
        self.assertEqual(selection_first, selection_second)
        self.assertEqual(clear_first, clear_second)

        STAGE1_MODULE.APP_CONFIG["source_id"] = "sha256:source-b"
        page_other_source = STAGE1_MODULE.catalog_page_cache_key("author", 123, 2)
        selection_other_source = STAGE1_MODULE.catalog_selection_storage_key(
            STAGE1_MODULE.current_source_id(),
            str("author"),
            str(123),
        )
        clear_other_source = STAGE1_MODULE.catalog_selection_clear_token("author", 123)
        full_key_other_source = STAGE1_MODULE.catalog_cache_key(
            STAGE1_MODULE.current_source_id(),
            "author",
            "123",
        )
        self.assertNotEqual(page_first, page_other_source)
        self.assertNotEqual(selection_first, selection_other_source)
        self.assertNotEqual(clear_first, clear_other_source)
        self.assertNotEqual(full_key_first, full_key_other_source)

    def test_j_all_legacy_integration_calls_stringify_opaque_values(self):
        expected = {
            "catalog_page_cache_key": "catalog_cache_key",
            "get_cached_catalog": "catalog_cache_key",
            "catalog_selection_clear_token": "catalog_selection_storage_key",
            "render_catalog": "catalog_selection_storage_key",
        }
        for function_name, helper_name in expected.items():
            with self.subTest(function=function_name):
                function_node = next(
                    node
                    for node in STAGE1_MODULE.__source_tree__.body
                    if getattr(node, "name", None) == function_name
                )
                helper_call = next(
                    child
                    for child in ast.walk(function_node)
                    if isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Name)
                    and child.func.id == helper_name
                )
                for argument in helper_call.args[1:3]:
                    self.assertIsInstance(argument, ast.Call)
                    self.assertIsInstance(argument.func, ast.Name)
                    self.assertEqual(argument.func.id, "str")


if __name__ == "__main__":
    unittest.main()
