import ast
import re
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)
HOME_TEXT = "← На главный экран"
HOME_TEMPLATES = (
    "ERROR_HTML",
    "SETTINGS_HTML",
    "OPDS_SETTINGS_HTML",
    "CATALOG_HTML",
    "REGISTERED_CATALOG_HTML",
    "OPDS_SEARCH_HTML",
    "JOB_HTML",
    "QUEUE_HTML",
    "JOBS_HTML",
    "HISTORY_HTML",
    "RUNS_HTML",
    "RUN_DETAIL_HTML",
    "NOTIFICATIONS_HTML",
)
EXCLUDED_TEMPLATES = (
    "NEUTRAL_HOME_HTML",
    "SETUP_HTML",
)
LOADING_CLASSES = (
    "opds-page-link",
    "opds-catalog-link",
    "registered-catalog-loading-link",
    "catalog-page-link",
    "catalog-full-view-link",
    "loading-disabled",
)
HOME_LINK_PATTERN = re.compile(
    r'<a(?P<before>[^>]*)href="\{\{\s*url_for\(\'index\'\)\s*\}\}"'
    r'(?P<after>[^>]*)>\s*← На главный экран\s*</a>',
    re.DOTALL,
)


def html_templates():
    templates = {}
    for node in TREE.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name) or not target.id.endswith("_HTML"):
            continue
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            templates[target.id] = node.value.value
    return templates


TEMPLATES = html_templates()


class HomeNavigationUITests(unittest.TestCase):
    def test_a_each_working_template_has_direct_local_home_link(self):
        for template_name in HOME_TEMPLATES:
            with self.subTest(template=template_name):
                template = TEMPLATES[template_name]
                matches = tuple(HOME_LINK_PATTERN.finditer(template))
                self.assertTrue(matches)
                for match in matches:
                    link = match.group(0)
                    self.assertIn("url_for('index')", link)
                    for loading_class in LOADING_CLASSES:
                        self.assertNotIn(loading_class, link)

    def test_b_main_screen_and_first_run_setup_are_excluded(self):
        for template_name in EXCLUDED_TEMPLATES:
            with self.subTest(template=template_name):
                self.assertNotIn(HOME_TEXT, TEMPLATES[template_name])

    def test_c_orphan_main_template_is_removed_and_neutral_home_is_rendered(self):
        rendered_templates = {
            call.args[0].id
            for call in ast.walk(TREE)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "render_template_string"
            and call.args
            and isinstance(call.args[0], ast.Name)
        }
        self.assertNotIn("MAIN_HTML", TEMPLATES)
        self.assertIn("NEUTRAL_HOME_HTML", rendered_templates)


if __name__ == "__main__":
    unittest.main()
