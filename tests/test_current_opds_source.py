import ast
import dataclasses
import sys
import types
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_current_source_module():
    """Загружает только нейтральный слой текущего OPDS-источника."""
    source = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    wanted = {
        "SourceConfig",
        "AcquisitionLink",
        "BookRecord",
        "CatalogRef",
        "OPDSFeed",
        "normalize_app_config",
        "source_config_from_app_config",
        "current_source_config",
        "current_source_id",
        "has_configured_opds_source",
        "load_current_opds_feed",
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
    module = types.ModuleType("isolated_current_opds_source_test")
    sys.modules[module.__name__] = module
    module.__dict__.update(
        dataclass=dataclasses.dataclass,
        DEFAULT_DESTINATION="test-default-library",
        APP_CONFIG={},
    )
    exec(compile(ast.Module(body=body, type_ignores=[]), "app.py", "exec"), module.__dict__)
    module.__source_text__ = source
    module.__source_tree__ = tree
    return module


SOURCE_MODULE = load_current_source_module()


class FakeClient:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def fetch_feed(self, url, source_id=""):
        self.calls.append((url, source_id))
        return self.result


class CurrentOPDSSourceTests(unittest.TestCase):
    def setUp(self):
        SOURCE_MODULE.APP_CONFIG = {
            "config_version": SOURCE_MODULE.CONFIG_VERSION,
            "opds_url": "",
            "source_id": "",
            "source_name": "",
            "library_path": "X:/Books",
            "setup_complete": True,
        }

    def test_a_empty_source_is_not_configured_and_cannot_load(self):
        source = SOURCE_MODULE.current_source_config()
        self.assertEqual(source.root_url, "")
        self.assertEqual(source.source_id, "")
        self.assertEqual(source.display_name, "")
        self.assertFalse(SOURCE_MODULE.has_configured_opds_source())
        with self.assertRaisesRegex(ValueError, "не настроен"):
            SOURCE_MODULE.load_current_opds_feed(FakeClient(object()))

    def test_b_configured_source_is_returned_without_changes(self):
        SOURCE_MODULE.APP_CONFIG.update(
            opds_url="https://example.org/opds",
            source_id="sha256:source-id",
            source_name="Example catalog",
        )
        source = SOURCE_MODULE.current_source_config()
        self.assertEqual(source.root_url, "https://example.org/opds")
        self.assertEqual(source.source_id, "sha256:source-id")
        self.assertEqual(source.display_name, "Example catalog")
        self.assertTrue(SOURCE_MODULE.has_configured_opds_source())

    def test_c_load_passes_exact_url_and_source_id_to_client(self):
        SOURCE_MODULE.APP_CONFIG.update(
            opds_url="https://example.org/opds",
            source_id="sha256:source-id",
            source_name="Example catalog",
        )
        feed = SOURCE_MODULE.OPDSFeed(
            title="Example feed",
            publications=(),
            navigation=(),
            next_url="",
        )
        client = FakeClient(feed)
        result = SOURCE_MODULE.load_current_opds_feed(client)
        self.assertIs(result, feed)
        self.assertEqual(
            client.calls,
            [("https://example.org/opds", "sha256:source-id")],
        )

    def test_d_reading_and_loading_do_not_mutate_app_config(self):
        SOURCE_MODULE.APP_CONFIG.update(
            opds_url="https://example.org/opds",
            source_id="sha256:source-id",
            source_name="Example catalog",
            custom_test=123,
        )
        snapshot = dict(SOURCE_MODULE.APP_CONFIG)
        SOURCE_MODULE.current_source_config()
        SOURCE_MODULE.has_configured_opds_source()
        SOURCE_MODULE.load_current_opds_feed(FakeClient(object()))
        self.assertEqual(SOURCE_MODULE.APP_CONFIG, snapshot)

    def test_e_empty_legacy_config_does_not_gain_a_url(self):
        SOURCE_MODULE.APP_CONFIG = {
            "library_path": "X:/Books",
            "setup_complete": True,
        }
        snapshot = dict(SOURCE_MODULE.APP_CONFIG)
        source = SOURCE_MODULE.current_source_config()
        self.assertEqual(source.root_url, "")
        self.assertEqual(source.source_id, "")
        self.assertNotIn("http", source.root_url)
        self.assertEqual(SOURCE_MODULE.APP_CONFIG, snapshot)

    def test_f_current_source_id_delegates_to_current_source_config(self):
        SOURCE_MODULE.APP_CONFIG.update(
            opds_url="https://example.org/opds",
            source_id="sha256:source-id",
        )
        self.assertEqual(
            SOURCE_MODULE.current_source_id(),
            SOURCE_MODULE.current_source_config().source_id,
        )
        function_node = next(
            node
            for node in SOURCE_MODULE.__source_tree__.body
            if getattr(node, "name", None) == "current_source_id"
        )
        called_names = {
            child.func.id
            for child in ast.walk(function_node)
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
        }
        self.assertIn("current_source_config", called_names)

    def test_g_new_runtime_helpers_are_source_neutral(self):
        wanted = {
            "current_source_config",
            "current_source_id",
            "has_configured_opds_source",
            "load_current_opds_feed",
        }
        helper_source = "\n".join(
            ast.get_source_segment(SOURCE_MODULE.__source_text__, node) or ""
            for node in SOURCE_MODULE.__source_tree__.body
            if getattr(node, "name", None) in wanted
        ).lower()
        for marker in (
            "opds_base",
            "flibusta.is",
            "/opds/search",
            "proxy",
            "socks",
            "xray",
        ):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, helper_source)

    def test_h_legacy_runtime_functions_do_not_use_new_feed_loader(self):
        protected = {
            "catalog_start_url",
            "load_catalog_page",
            "collect_catalog",
        }
        found = set()
        for node in SOURCE_MODULE.__source_tree__.body:
            if getattr(node, "name", None) not in protected:
                continue
            found.add(node.name)
            called_names = {
                child.func.id
                for child in ast.walk(node)
                if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
            }
            self.assertNotIn("load_current_opds_feed", called_names)
        self.assertEqual(found, protected)


if __name__ == "__main__":
    unittest.main()
