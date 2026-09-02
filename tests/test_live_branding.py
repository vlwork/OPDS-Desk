import ast
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)


def assignment_source(name):
    for node in TREE.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(
            isinstance(target, ast.Name) and target.id == name
            for target in node.targets
        ):
            return ast.get_source_segment(SOURCE, node) or ""
    raise AssertionError(f"Assignment not found: {name}")


def assignment_value(name):
    for node in TREE.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(
            isinstance(target, ast.Name) and target.id == name
            for target in node.targets
        ):
            if isinstance(node.value, ast.Constant):
                return node.value.value
    raise AssertionError(f"Constant assignment not found: {name}")


def function_source(name):
    for node in TREE.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(SOURCE, node) or ""
    raise AssertionError(f"Function not found: {name}")


def main_guard_source():
    for node in TREE.body:
        if not isinstance(node, ast.If):
            continue
        test_source = ast.get_source_segment(SOURCE, node.test) or ""
        if "__name__" in test_source and "__main__" in test_source:
            return ast.get_source_segment(SOURCE, node) or ""
    raise AssertionError("__main__ guard not found")


class LiveBrandingTests(unittest.TestCase):
    def test_a_settings_template_uses_opds_desk_branding(self):
        template = assignment_value("SETTINGS_HTML")
        self.assertIn("Настройки — OPDS Desk", template)
        self.assertIn("OPDS Desk {{ app_version }}", template)
        self.assertIn("после следующих запусков OPDS Desk", template)
        self.assertIn("desktop-версии OPDS Desk", template)
        self.assertNotIn("Flibusta Bridge", template)

    def test_b_catalog_template_uses_source_neutral_labels(self):
        template = assignment_value("CATALOG_HTML")
        self.assertIn("ID записи источника", template)
        self.assertIn("Получение данных из OPDS-каталога", template)
        self.assertNotIn("ID Флибусты", template)
        self.assertNotIn("Получение данных с Flibusta", template)

    def test_c_queue_and_jobs_templates_use_opds_desk_branding(self):
        queue_template = assignment_value("QUEUE_HTML")
        jobs_template = assignment_value("JOBS_HTML")
        self.assertIn("пока OPDS Desk запущен", queue_template)
        self.assertNotIn("Flibusta Bridge", queue_template)
        self.assertIn("OPDS Desk", jobs_template)
        self.assertNotIn("Flibusta Bridge", jobs_template)

    def test_d_orphan_main_template_is_removed(self):
        assignments = {
            target.id
            for node in TREE.body
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        self.assertNotIn("MAIN_HTML", assignments)

    def test_e_runtime_messages_are_source_neutral(self):
        error_source = function_source("download_error_info")
        epub_source = function_source("save_epub")
        fb2_source = function_source("save_fb2")

        self.assertIn("Ошибка OPDS-источника (HTTP {status})", error_source)
        self.assertNotIn("Ошибка Flibusta", error_source)

        self.assertIn("ожидание ответа OPDS-источника…", epub_source)
        self.assertNotIn("ожидание ответа Флибусты", epub_source)

        self.assertIn("ожидание ответа OPDS-источника…", fb2_source)
        self.assertIn("OPDS-источник вернул некорректный FB2 ZIP", fb2_source)
        self.assertNotIn("ожидание ответа Флибусты", fb2_source)
        self.assertNotIn("Флибуста вернула некорректный FB2 ZIP", fb2_source)

    def test_f_native_window_title_uses_opds_desk(self):
        source = main_guard_source()
        self.assertIn('title=f"OPDS Desk {APP_VERSION}"', source)
        self.assertNotIn('title=f"Flibusta Bridge {APP_VERSION}"', source)

    def test_g_internal_compatibility_markers_remain_unchanged(self):
        self.assertIn('"OPDSDesk"', assignment_source("NEUTRAL_APP_DATA_DIR"))
        self.assertIn('"FlibustaBridge"', assignment_source("LEGACY_APP_DATA_DIR"))
        self.assertIn("resolve_app_data_dir", assignment_source("APP_DATA_DIR"))
        self.assertIn("OPDS_DESK_SECRET", SOURCE)
        self.assertIn("FLIBUSTA_BRIDGE_SECRET", SOURCE)
        self.assertIn("booklore-flibusta-local-v20", SOURCE)
        self.assertEqual(assignment_value("LEGACY_QUEUE_SOURCE_ID"), "legacy-v1")

        queue_template = assignment_value("QUEUE_HTML")
        self.assertIn("opdsDeskLastNotificationId", queue_template)
        self.assertIn("flibustaLastNotificationId", queue_template)
        self.assertIn("[opds-", function_source("download_filename_identity_marker"))
        self.assertIn("[flibusta-", function_source("legacy_duplicate_storage_title"))
        candidate_source = function_source("duplicate_storage_title_candidates")
        self.assertIn("duplicate_storage_title", candidate_source)
        self.assertIn("legacy_duplicate_storage_title", candidate_source)
        transport_source = function_source("legacy_opds_get")
        self.assertIn("OPDS-Desk/1.0", transport_source)
        self.assertNotIn("BookLore-Flibusta-Bridge/12.0", transport_source)
        self.assertIn('data["legacy_opds"]', function_source("health_snapshot"))
        self.assertIn('data["flibusta"]', function_source("health_snapshot"))

        db_source = function_source("init_queue_db")
        self.assertIn("flibusta_id TEXT NOT NULL", db_source)
        self.assertIn("uq_queue_active_flibusta", db_source)
        self.assertIn("source_item_id=flibusta_id", db_source)

        assignments = {
            target.id
            for node in TREE.body
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        definitions = {
            getattr(node, "name", None)
            for node in TREE.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        self.assertIn("LEGACY_OPDS_BASE", assignments)
        self.assertNotIn("OPDS_BASE", assignments)
        self.assertIn("allowed_legacy_opds_url", definitions)
        self.assertIn("legacy_opds_get", definitions)
        self.assertNotIn("allowed_flibusta_url", definitions)
        self.assertNotIn("flibusta_get", definitions)


if __name__ == "__main__":
    unittest.main()
