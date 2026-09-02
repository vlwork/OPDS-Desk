import ast
import dataclasses
import re
import sys
import types
import unittest
from pathlib import Path

from flask import Flask, render_template_string


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_setup_module():
    """Загружает только setup presentation без runtime приложения."""
    source = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    wanted_definitions = {
        "set_library_path",
        "DesktopApi",
        "setup_page",
        "index",
        "health_api",
    }
    wanted_assignments = {"SETUP_HTML"}
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

    module = types.ModuleType("isolated_neutral_setup_test")
    sys.modules[module.__name__] = module
    module.__dict__.update(
        dataclass=dataclasses.dataclass,
        app=Flask(module.__name__),
        render_template_string=render_template_string,
        COMMON_CSS="",
        DESTINATION="X:/Books",
    )
    exec(
        compile(ast.Module(body=body, type_ignores=[]), "app.py", "exec"),
        module.__dict__,
    )
    module.app.testing = True
    module.__source_text__ = source
    module.__source_tree__ = tree
    return module


SETUP_MODULE = load_setup_module()


def definition_source(name):
    node = next(
        item
        for item in SETUP_MODULE.__source_tree__.body
        if getattr(item, "name", None) == name
    )
    return ast.get_source_segment(SETUP_MODULE.__source_text__, node) or ""


def desktop_method_source(name):
    class_node = next(
        item
        for item in SETUP_MODULE.__source_tree__.body
        if getattr(item, "name", None) == "DesktopApi"
    )
    method = next(
        item for item in class_node.body if getattr(item, "name", None) == name
    )
    return ast.get_source_segment(SETUP_MODULE.__source_text__, method) or ""


class NeutralSetupTests(unittest.TestCase):
    def test_a_setup_route_returns_success(self):
        response = SETUP_MODULE.app.test_client().get("/setup")
        self.assertEqual(response.status_code, 200)

    def test_b_setup_has_no_legacy_health_or_transport_terms(self):
        template = SETUP_MODULE.SETUP_HTML.lower()
        for forbidden in (
            "flibusta",
            "/api/health",
            "data.flibusta",
            "opds_base",
            "proxy",
            "socks",
            "xray",
            "tor",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, template)

    def test_c_health_button_and_javascript_are_removed(self):
        template = SETUP_MODULE.SETUP_HTML
        for forbidden in (
            "checkFlibustaButton",
            "flibustaStatus",
            "flibustaAvailable",
            "checkButton",
            "fetch(",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, template)

    def test_d_library_picker_call_remains(self):
        self.assertIn(
            "window.pywebview.api.choose_library_folder()",
            SETUP_MODULE.SETUP_HTML,
        )
        self.assertIn("{{ destination }}", SETUP_MODULE.SETUP_HTML)

    def test_e_complete_setup_call_remains(self):
        self.assertIn(
            "window.pywebview.api.complete_setup()",
            SETUP_MODULE.SETUP_HTML,
        )

    def test_f_finish_button_is_initially_disabled(self):
        self.assertRegex(
            SETUP_MODULE.SETUP_HTML,
            r'id="finishSetupButton"\s+disabled',
        )

    def test_g_successful_folder_choice_enables_finish(self):
        template = SETUP_MODULE.SETUP_HTML
        success_start = template.index("if (result && result.ok)")
        cancel_start = template.index("else if (result && result.cancelled)")
        success_branch = template[success_start:cancel_start]
        self.assertIn("librarySelected = true;", success_branch)
        self.assertIn("finishButton.disabled = false;", success_branch)

    def test_h_cancel_before_success_keeps_finish_disabled(self):
        template = SETUP_MODULE.SETUP_HTML
        cancel_start = template.index("else if (result && result.cancelled)")
        error_start = template.index("} else {", cancel_start)
        cancel_branch = template[cancel_start:error_start]
        self.assertIn("finishButton.disabled = !librarySelected;", cancel_branch)
        self.assertNotIn("librarySelected = true", cancel_branch)

    def test_i_repeated_cancel_does_not_reset_prior_success(self):
        template = SETUP_MODULE.SETUP_HTML
        self.assertEqual(template.count("librarySelected = false"), 1)
        self.assertEqual(template.count("librarySelected = true"), 1)
        self.assertNotIn("librarySelected = false;", template.split("let librarySelected = false;", 1)[1])

    def test_j_success_redirects_to_opds_settings_not_root(self):
        template = SETUP_MODULE.SETUP_HTML
        self.assertIn("window.location.href = '/settings/opds';", template)
        self.assertNotIn("window.location.href = '/';", template)

    def test_k_first_run_contains_no_opds_url_input_or_source_save(self):
        template = SETUP_MODULE.SETUP_HTML.lower()
        self.assertNotIn('name="opds_url"', template)
        self.assertNotIn("configure_opds_source", template)
        self.assertNotIn("clear_configured_opds_source", template)
        self.assertNotIn('app_config["opds_url"]', template)

    def test_l_complete_setup_semantics_remain_protected(self):
        source = desktop_method_source("complete_setup")
        self.assertIn('APP_CONFIG["setup_complete"] = True', source)
        self.assertIn("save_app_config(APP_CONFIG)", source)
        self.assertNotIn("opds_url", source)

    def test_m_library_picker_backend_remains_protected(self):
        source = desktop_method_source("choose_library_folder")
        self.assertIn("webview.active_window()", source)
        self.assertIn("webview.FileDialog.FOLDER", source)
        self.assertIn("set_library_path(selected_path)", source)

    def test_n_library_path_backend_remains_protected(self):
        source = definition_source("set_library_path")
        self.assertIn("os.path.isdir(path)", source)
        self.assertIn('APP_CONFIG["library_path"] = path', source)
        self.assertIn("save_app_config(APP_CONFIG)", source)
        self.assertIn("DESTINATION = path", source)

    def test_o_index_still_gates_on_setup_complete(self):
        source = definition_source("index")
        self.assertIn('APP_CONFIG.get("setup_complete", False)', source)
        self.assertIn("return setup_page()", source)
        self.assertIn("NEUTRAL_HOME_HTML", source)
        self.assertNotIn("perform_search_cached", source)

    def test_p_health_route_remains_available(self):
        health_source = definition_source("health_api")
        health_node = next(
            item
            for item in SETUP_MODULE.__source_tree__.body
            if getattr(item, "name", None) == "health_api"
        )
        route_paths = {
            decorator.args[0].value
            for decorator in health_node.decorator_list
            if isinstance(decorator, ast.Call)
            and decorator.args
            and isinstance(decorator.args[0], ast.Constant)
        }
        self.assertIn("/api/health", route_paths)
        self.assertIn("health_snapshot(force=force)", health_source)

    def test_q_wizard_has_three_neutral_steps(self):
        template = SETUP_MODULE.SETUP_HTML
        self.assertIn("1. О приложении", template)
        self.assertIn("2. Папка библиотеки", template)
        self.assertIn("3. Завершение настройки", template)
        self.assertIn("OPDS-источник необязателен на этом этапе", template)
        self.assertEqual(len(re.findall(r"<strong>[123]\.", template)), 3)


if __name__ == "__main__":
    unittest.main()
