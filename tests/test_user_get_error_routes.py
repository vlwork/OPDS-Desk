import ast
import re
import sys
import types
import unittest
from pathlib import Path

from flask import Flask, flash, redirect, render_template_string, request, url_for


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_error_route_module():
    """Загружает только общий error template/helper и проверяемые GET routes."""
    source = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    wanted_definitions = {
        "render_error_page",
        "author_catalog",
        "series_catalog",
        "queue_run_detail",
        "job_page",
    }
    wanted_assignments = {"COMMON_CSS", "ERROR_HTML"}
    body = []
    for node in tree.body:
        if getattr(node, "name", None) in wanted_definitions:
            body.append(node)
            continue
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in wanted_assignments
            for target in node.targets
        ):
            body.append(node)

    module = types.ModuleType("isolated_user_get_error_routes_test")
    sys.modules[module.__name__] = module
    module.__dict__.update(
        app=Flask(module.__name__),
        flash=flash,
        redirect=redirect,
        re=re,
        render_template_string=render_template_string,
        request=request,
        url_for=url_for,
        render_catalog=lambda *args, **kwargs: "catalog",
        queue_run_summary=lambda run_id: None,
        queue_run_items=lambda run_id: (),
        job_snapshot=lambda job_id: None,
        DOWNLOAD_RETRY_ATTEMPTS=1,
        DOWNLOAD_CONNECT_TIMEOUT=1,
        DOWNLOAD_READ_TIMEOUT=1,
        JOB_HTML="",
        RUN_DETAIL_HTML="",
    )
    exec(
        compile(ast.Module(body=body, type_ignores=[]), "app.py", "exec"),
        module.__dict__,
    )
    module.app.add_url_rule("/", endpoint="index", view_func=lambda: "index")
    module.app.testing = True
    module.app.secret_key = "user-get-error-routes-test"
    module.__source_text__ = source
    module.__source_tree__ = tree
    return module


ROUTE_MODULE = load_error_route_module()


class UserGetErrorRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = ROUTE_MODULE.app.test_client()
        ROUTE_MODULE.queue_run_summary = lambda run_id: None
        ROUTE_MODULE.job_snapshot = lambda job_id: None

    def assert_html_error(self, response, status, title, message):
        self.assertEqual(response.status_code, status)
        self.assertTrue(response.content_type.startswith("text/html"))
        body = response.get_data(as_text=True)
        self.assertIn(title, body)
        self.assertIn(message, body)
        self.assertIn(f"HTTP {status}", body)
        self.assertIn('<a href="/">← На главный экран</a>', body)

    def test_a_invalid_author_id_returns_html_404(self):
        response = self.client.get("/author/not-a-number")
        self.assert_html_error(
            response,
            404,
            "Автор не найден",
            "Некорректный идентификатор автора.",
        )

    def test_b_invalid_series_id_returns_html_404(self):
        response = self.client.get("/series/not-a-number")
        self.assert_html_error(
            response,
            404,
            "Серия не найдена",
            "Некорректный идентификатор серии.",
        )

    def test_c_invalid_run_id_returns_html_400(self):
        response = self.client.get("/runs/not-valid")
        self.assert_html_error(
            response,
            400,
            "Некорректный запуск",
            "Некорректный run_id.",
        )

    def test_d_missing_run_returns_html_404_without_database_dependency(self):
        calls = []
        ROUTE_MODULE.queue_run_summary = lambda run_id: calls.append(run_id)
        run_id = "0123456789abcdef0123456789abcdef"

        response = self.client.get(f"/runs/{run_id}")

        self.assertEqual(calls, [run_id])
        self.assert_html_error(
            response,
            404,
            "Запуск не найден",
            "Запуск не найден.",
        )

    def test_e_missing_job_returns_html_404_without_runtime_jobs(self):
        calls = []
        ROUTE_MODULE.job_snapshot = lambda job_id: calls.append(job_id)

        response = self.client.get("/job/missing-job-id")

        self.assertEqual(calls, ["missing-job-id"])
        self.assert_html_error(
            response,
            404,
            "Задание не найдено",
            "Задание не найдено.",
        )

    def test_f_error_template_has_only_direct_local_home_navigation(self):
        template = ROUTE_MODULE.ERROR_HTML
        self.assertIn("url_for('index')", template)
        self.assertIn("← На главный экран", template)
        for forbidden in (
            "request.referrer",
            "history.back",
            "return_url",
            "search_return",
            "context_return",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, template)

    def test_g_title_and_message_keep_default_jinja_escaping(self):
        template = ROUTE_MODULE.ERROR_HTML
        self.assertIn("{{ title }}", template)
        self.assertIn("{{ message }}", template)
        self.assertNotIn("title|safe", template)
        self.assertNotIn("message|safe", template)

    def test_h_no_global_400_or_404_errorhandler_was_added(self):
        handlers = []
        for node in ROUTE_MODULE.__source_tree__.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue
                function = decorator.func
                if (
                    isinstance(function, ast.Attribute)
                    and function.attr == "errorhandler"
                    and decorator.args
                    and isinstance(decorator.args[0], ast.Constant)
                    and decorator.args[0].value in {400, 404}
                ):
                    handlers.append(decorator.args[0].value)
        self.assertEqual(handlers, [])


if __name__ == "__main__":
    unittest.main()
