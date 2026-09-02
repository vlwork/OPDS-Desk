import ast
import re
import unittest
from pathlib import Path

from flask import Flask


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)


def top_level_function_names():
    return {
        node.name
        for node in TREE.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def flask_route_decorators():
    routes = []
    for node in TREE.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and isinstance(decorator.func.value, ast.Name)
                and decorator.func.value.id == "app"
                and decorator.func.attr in {"get", "post", "route"}
                and decorator.args
                and isinstance(decorator.args[0], ast.Constant)
                and isinstance(decorator.args[0].value, str)
            ):
                continue
            routes.append((node.name, decorator))
    return routes


def route_methods(decorator):
    if decorator.func.attr == "get":
        return ("GET",)
    if decorator.func.attr == "post":
        return ("POST",)
    for keyword in decorator.keywords:
        if keyword.arg == "methods":
            return tuple(ast.literal_eval(keyword.value))
    return ("GET",)


def isolated_route_app():
    app = Flask("isolated_orphan_legacy_routes_test")
    for index, (_, decorator) in enumerate(flask_route_decorators()):
        app.add_url_rule(
            decorator.args[0].value,
            endpoint=f"isolated_route_{index}",
            view_func=lambda: "",
            methods=route_methods(decorator),
        )
    app.testing = True
    return app


def production_template_source():
    templates = []
    for node in TREE.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id.endswith("_HTML")
            for target in node.targets
        ):
            continue
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            templates.append(node.value.value)
    return "\n".join(templates)


class OrphanLegacyRoutesRemovedTests(unittest.TestCase):
    def test_a_orphan_handler_functions_are_absent(self):
        functions = top_level_function_names()
        self.assertNotIn("download_book", functions)
        self.assertNotIn("queue_add_one", functions)

    def test_b_orphan_route_decorators_are_absent(self):
        paths = {
            decorator.args[0].value
            for _, decorator in flask_route_decorators()
        }
        self.assertNotIn("/download", paths)
        self.assertNotIn("/queue/add-one", paths)

    def test_c_production_templates_have_no_orphan_route_callers(self):
        templates = production_template_source()
        for forbidden in (
            'url_for("download_book")',
            "url_for('download_book')",
            "queue_add_one",
            "/queue/add-one",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, templates)
        self.assertIsNone(
            re.search(
                r"<form\b[^>]*\baction\s*=\s*(['\"])/download\1",
                templates,
                flags=re.IGNORECASE,
            )
        )

    def test_d_removed_post_paths_return_404_in_isolated_route_map(self):
        client = isolated_route_app().test_client()
        self.assertEqual(client.post("/download").status_code, 404)
        self.assertEqual(client.post("/queue/add-one").status_code, 404)


if __name__ == "__main__":
    unittest.main()
