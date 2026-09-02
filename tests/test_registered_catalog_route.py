import ast
import dataclasses
import re
import sys
import types
import unittest
from pathlib import Path

import requests
from flask import Flask, redirect, render_template_string, request, url_for


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_route_module():
    """Загружает только read-only route и template без runtime приложения."""
    source = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    wanted_definitions = {
        "RegisteredCatalogRef",
        "RegisteredCatalogBookView",
        "RegisteredCatalogView",
        "registered_catalog_page",
    }
    body = []
    for node in tree.body:
        if getattr(node, "name", None) in wanted_definitions:
            body.append(node)
            continue
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name)
            and target.id == "REGISTERED_CATALOG_HTML"
            for target in node.targets
        ):
            body.append(node)

    module = types.ModuleType("isolated_registered_catalog_route_test")
    sys.modules[module.__name__] = module
    module.__dict__.update(
        dataclass=dataclasses.dataclass,
        app=Flask(module.__name__),
        request=request,
        redirect=redirect,
        render_template_string=render_template_string,
        requests=requests,
        build_registered_catalog_view=None,
        resolve_preferred_registered_catalog_token=None,
        url_for=url_for,
    )
    exec(
        compile(ast.Module(body=body, type_ignores=[]), "app.py", "exec"),
        module.__dict__,
    )
    module.app.add_url_rule("/", endpoint="index", view_func=lambda: "")
    module.app.testing = True
    module.__source_text__ = source
    module.__source_tree__ = tree
    return module


ROUTE_MODULE = load_route_module()


def book_view(title="Example Book", author="Example Author"):
    return ROUTE_MODULE.RegisteredCatalogBookView(
        id="urn:uuid:550e8400-e29b-41d4-a716-446655440000",
        title=title,
        author=author,
        authors=(author,),
        language="en",
        genres=("fiction", "adventure"),
        formats=("EPUB", "FB2"),
        translator="Translator",
        size="1.2 MB",
        has_cover=True,
    )


def catalog_view(
    token,
    page=0,
    pages=1,
    has_previous=False,
    has_next=False,
    view_all=False,
    title="Example Catalog",
    navigation=(),
):
    return ROUTE_MODULE.RegisteredCatalogView(
        token=token,
        title=title,
        books=(book_view(),),
        page=page,
        pages=pages,
        has_previous=has_previous,
        has_next=has_next,
        view_all=view_all,
        navigation=navigation,
    )


class RegisteredCatalogRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = ROUTE_MODULE.app.test_client()
        self.calls = []

        def builder(token, page=0, view_all=False):
            self.calls.append(
                {"token": token, "page": page, "view_all": view_all}
            )
            return catalog_view(
                token,
                page=page,
                pages=page + 1,
                has_previous=page > 0,
                has_next=True,
                view_all=view_all,
            )

        ROUTE_MODULE.build_registered_catalog_view = builder
        ROUTE_MODULE.resolve_preferred_registered_catalog_token = (
            lambda token: token
        )

    def get(self, token="catalog:" + "a" * 64, query=""):
        suffix = f"?{query}" if query else ""
        return self.client.get(f"/catalog/opds/{token}{suffix}")

    def test_a_get_returns_success(self):
        response = self.get()
        self.assertEqual(response.status_code, 200)

    def test_b_route_passes_exact_opaque_token_to_builder(self):
        token = "catalog:" + "b" * 64
        self.get(token)
        self.assertEqual(self.calls[0]["token"], token)

    def test_c_page_query_is_passed_as_integer(self):
        self.get(query="page=2")
        self.assertEqual(self.calls[0]["page"], 2)

    def test_d_negative_and_invalid_pages_become_zero(self):
        self.get(query="page=-7")
        self.assertEqual(self.calls[-1]["page"], 0)
        self.get(query="page=not-a-number")
        self.assertEqual(self.calls[-1]["page"], 0)

    def test_e_view_all_query_is_passed_to_builder(self):
        self.get(query="view=all")
        self.assertTrue(self.calls[0]["view_all"])

    def test_f_token_is_never_converted_to_integer(self):
        tokens = (
            "catalog:" + "c" * 64,
            "550e8400-e29b-41d4-a716-446655440000",
            "sha256:" + "d" * 64,
        )
        for token in tokens:
            with self.subTest(token=token):
                self.get(token)
                self.assertEqual(self.calls[-1]["token"], token)

    def test_g_html_shows_readonly_metadata(self):
        body = self.get().get_data(as_text=True)
        self.assertIn("Example Catalog", body)
        self.assertIn("Example Book", body)
        self.assertIn("Example Author", body)
        self.assertIn("EPUB, FB2", body)
        self.assertIn("fiction, adventure", body)
        self.assertIn("Translator", body)
        self.assertIn("1.2 MB", body)

    def test_h_html_has_no_backend_or_external_url_data(self):
        body = self.get().get_data(as_text=True).lower()
        for forbidden in (
            "epub_url",
            "fb2_url",
            "acquisition_links",
            "opds_url",
            "cover_url",
            "thumbnail_url",
            "web_url",
            "files.example.org",
            "catalog.example.org/root.xml",
            "download",
            "queue",
            "sessionstorage",
            "checkbox",
            "save_epub",
            "save_fb2",
            "<form",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, body)

    def test_i_navigation_uses_only_registered_token(self):
        token = "catalog:" + "a" * 64
        navigation_token = "catalog:" + "e" * 64
        navigation = ROUTE_MODULE.RegisteredCatalogRef(
            token=navigation_token,
            title="Nested catalog",
            kind="navigation",
        )
        ROUTE_MODULE.build_registered_catalog_view = (
            lambda *args, **kwargs: catalog_view(
                token,
                navigation=(navigation,),
            )
        )
        body = self.get(token).get_data(as_text=True)
        self.assertIn(f'/catalog/opds/{navigation_token}', body)
        self.assertIn("Nested catalog", body)
        self.assertIn(
            f'class="registered-catalog-loading-link" href="/catalog/opds/{navigation_token}"',
            body,
        )
        self.assertNotIn("https://source.example.org", body)

    def test_j_previous_next_and_view_all_links_are_correct(self):
        token = "catalog:" + "f" * 64
        ROUTE_MODULE.build_registered_catalog_view = (
            lambda *args, **kwargs: catalog_view(
                token,
                page=2,
                pages=3,
                has_previous=True,
                has_next=True,
            )
        )
        body = self.get(token, "page=2").get_data(as_text=True)
        self.assertIn(f'/catalog/opds/{token}?page=1', body)
        self.assertIn(f'/catalog/opds/{token}?page=3', body)
        self.assertIn(f'/catalog/opds/{token}?view=all', body)
        self.assertEqual(body.count('class="registered-catalog-loading-link"'), 3)

    def test_k_view_all_links_back_to_page_zero(self):
        token = "catalog:" + "1" * 64
        ROUTE_MODULE.build_registered_catalog_view = (
            lambda *args, **kwargs: catalog_view(
                token,
                pages=4,
                view_all=True,
            )
        )
        body = self.get(token, "view=all").get_data(as_text=True)
        self.assertIn(f'/catalog/opds/{token}?page=0', body)
        self.assertEqual(body.count('class="registered-catalog-loading-link"'), 1)

    def test_l_unknown_or_stale_token_returns_sanitized_404(self):
        ROUTE_MODULE.build_registered_catalog_view = (
            lambda *args, **kwargs: (_ for _ in ()).throw(
                ValueError("OPDS-каталог недоступен или устарел")
            )
        )
        response = self.get()
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 404)
        self.assertIn("OPDS-каталог недоступен или устарел.", body)
        self.assertIn('id="registeredCatalogLoadingOverlay"', body)
        self.assertNotIn("Traceback", body)

    def test_m_expected_load_error_returns_sanitized_502(self):
        secret = "https://private.example.org/internal-cache-key"
        ROUTE_MODULE.build_registered_catalog_view = (
            lambda *args, **kwargs: (_ for _ in ()).throw(
                RuntimeError(f"failure at {secret}")
            )
        )
        response = self.get()
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 502)
        self.assertIn("Не удалось загрузить OPDS-каталог.", body)
        self.assertNotIn(secret, body)
        self.assertNotIn("Traceback", body)

    def test_n_jinja_escapes_catalog_and_book_text(self):
        token = "catalog:" + "2" * 64
        unsafe = "<script>alert(1)</script>"
        ROUTE_MODULE.build_registered_catalog_view = (
            lambda *args, **kwargs: ROUTE_MODULE.RegisteredCatalogView(
                token=token,
                title=unsafe,
                books=(book_view(title=unsafe, author=unsafe),),
                page=0,
                pages=1,
                has_previous=False,
                has_next=False,
                view_all=False,
                navigation=(),
            )
        )
        body = self.get(token).get_data(as_text=True)
        self.assertNotIn(unsafe, body)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", body)

    def test_o_route_is_get_only(self):
        response = self.client.post("/catalog/opds/catalog:" + "3" * 64)
        self.assertEqual(response.status_code, 405)
        self.assertEqual(self.calls, [])

    def test_p_legacy_routes_renderer_and_template_are_not_connected(self):
        protected_functions = {
            "render_catalog",
            "author_catalog",
            "series_catalog",
        }
        found = set()
        for node in ROUTE_MODULE.__source_tree__.body:
            name = getattr(node, "name", None)
            if name not in protected_functions:
                continue
            found.add(name)
            called_names = {
                child.func.id
                for child in ast.walk(node)
                if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
            }
            self.assertNotIn("build_registered_catalog_view", called_names)
        self.assertEqual(found, protected_functions)
        catalog_template = next(
            node
            for node in ROUTE_MODULE.__source_tree__.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "CATALOG_HTML"
                for target in node.targets
            )
        )
        template_source = (
            ast.get_source_segment(ROUTE_MODULE.__source_text__, catalog_template)
            or ""
        )
        self.assertNotIn("registered_catalog_page", template_source)

    def test_q_route_calls_no_lower_level_catalog_components(self):
        route_node = next(
            node
            for node in ROUTE_MODULE.__source_tree__.body
            if getattr(node, "name", None) == "registered_catalog_page"
        )
        called_names = {
            child.func.id
            for child in ast.walk(route_node)
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
        }
        self.assertIn("build_registered_catalog_view", called_names)
        self.assertIn(
            "resolve_preferred_registered_catalog_token",
            called_names,
        )
        for forbidden in (
            "get_current_catalog_ref",
            "load_registered_catalog_page",
            "collect_registered_catalog",
            "OPDSHTTPClient",
        ):
            self.assertNotIn(forbidden, called_names)

    def test_r_template_marks_every_catalog_navigation_link(self):
        template = ROUTE_MODULE.REGISTERED_CATALOG_HTML
        self.assertEqual(
            template.count('class="registered-catalog-loading-link"'),
            5,
        )
        for label in (
            "Вернуться к первой странице",
            "← Назад",
            "Далее →",
            "Показать всё",
            "{{ item.title }}",
        ):
            with self.subTest(label=label):
                tagged_link = next(
                    line
                    for line in template.splitlines()
                    if label in line
                )
                self.assertIn('class="registered-catalog-loading-link"', tagged_link)
        for required in (
            'id="registeredCatalogLoadingOverlay"',
            'aria-hidden="true"',
            'role="status"',
            'aria-live="polite"',
            'class="registered-catalog-loading-spinner" aria-hidden="true"',
            "Загрузка каталога...",
            "Получение данных из OPDS-каталога",
        ):
            with self.subTest(required=required):
                self.assertIn(required, template)

    def test_s_catalog_loading_allows_first_click_and_resets_on_pageshow(self):
        template = ROUTE_MODULE.REGISTERED_CATALOG_HTML
        loading_script = template.split(
            '<script id="registeredCatalogLoadingScript">',
            1,
        )[1].split("</script>", 1)[0]
        handler_match = re.search(
            r"function showRegisteredCatalogLoading\(event\) \{(?P<body>.*?)^\}",
            loading_script,
            re.DOTALL | re.MULTILINE,
        )
        self.assertIsNotNone(handler_match)
        handler = handler_match.group("body")
        self.assertEqual(handler.count("event.preventDefault()"), 1)
        self.assertLess(
            handler.index("if (registeredCatalogLoading)"),
            handler.index("event.preventDefault()"),
        )
        self.assertLess(
            handler.index("event.preventDefault()"),
            handler.index("registeredCatalogLoading = true"),
        )
        self.assertIn("link.classList.add('loading-disabled')", loading_script)
        self.assertIn("link.classList.remove('loading-disabled')", loading_script)
        self.assertIn("link.removeAttribute('aria-disabled')", loading_script)
        self.assertIn(
            "window.addEventListener('pageshow', resetRegisteredCatalogLoading)",
            loading_script,
        )

    def test_t_catalog_loading_is_neutral_and_has_no_transport_data(self):
        template = ROUTE_MODULE.REGISTERED_CATALOG_HTML
        loading_script = template.split(
            '<script id="registeredCatalogLoadingScript">',
            1,
        )[1].split("</script>", 1)[0]
        for forbidden in (
            "Flibusta",
            "Флибуста",
            "source_id",
            "acquisition_links",
            "epub_url",
            "fb2_url",
            "download_url",
            "requested_url",
            "final_url",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, loading_script)

    def test_u_home_link_is_local_and_skips_loading_on_success_and_error(self):
        home_link = '<a href="/">← На главный экран</a>'
        success_body = self.get().get_data(as_text=True)
        self.assertIn(home_link, success_body)

        ROUTE_MODULE.build_registered_catalog_view = (
            lambda *args, **kwargs: (_ for _ in ()).throw(
                RuntimeError("catalog load failed")
            )
        )
        error_body = self.get().get_data(as_text=True)
        self.assertIn(home_link, error_body)

        for body in (success_body, error_body):
            with self.subTest(page="success" if body is success_body else "error"):
                home_link_match = re.search(
                    r'<a(?P<attributes>[^>]*)>← На главный экран</a>',
                    body,
                )
                self.assertIsNotNone(home_link_match)
                self.assertNotIn(
                    "registered-catalog-loading-link",
                    home_link_match.group("attributes"),
                )

    def test_v_root_page_redirects_to_local_resolved_child_token(self):
        root_token = "catalog:" + "4" * 64
        child_token = "catalog:" + "5" * 64
        ROUTE_MODULE.resolve_preferred_registered_catalog_token = (
            lambda token: child_token
        )

        response = self.get(root_token)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], f"/catalog/opds/{child_token}")
        self.assertNotIn("http", response.headers["Location"])
        self.assertEqual(self.calls, [])

    def test_w_child_token_renders_books_without_another_redirect(self):
        child_token = "catalog:" + "6" * 64
        ROUTE_MODULE.resolve_preferred_registered_catalog_token = (
            lambda token: token
        )

        response = self.get(child_token)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.calls[0]["token"], child_token)

    def test_x_child_pagination_keeps_child_token_and_skips_resolver(self):
        child_token = "catalog:" + "7" * 64
        resolver_calls = []
        ROUTE_MODULE.resolve_preferred_registered_catalog_token = (
            lambda token: resolver_calls.append(token) or token
        )

        response = self.get(child_token, "page=1")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(resolver_calls, [])
        self.assertEqual(
            self.calls[0],
            {"token": child_token, "page": 1, "view_all": False},
        )

    def test_y_view_all_redirect_preserves_view_without_root_page(self):
        root_token = "catalog:" + "8" * 64
        child_token = "catalog:" + "9" * 64
        ROUTE_MODULE.resolve_preferred_registered_catalog_token = (
            lambda token: child_token
        )

        response = self.get(root_token, "page=0&view=all")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.headers["Location"],
            f"/catalog/opds/{child_token}?view=all",
        )

    def test_z_explicit_root_page_one_is_not_auto_followed(self):
        root_token = "catalog:" + "a" * 64
        resolver_calls = []
        ROUTE_MODULE.resolve_preferred_registered_catalog_token = (
            lambda token: resolver_calls.append(token) or "catalog:" + "b" * 64
        )

        response = self.get(root_token, "page=1")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(resolver_calls, [])
        self.assertEqual(self.calls[0]["token"], root_token)
        self.assertEqual(self.calls[0]["page"], 1)

    def test_z2_ambiguous_root_stays_on_navigation_page(self):
        root_token = "catalog:" + "c" * 64
        ROUTE_MODULE.resolve_preferred_registered_catalog_token = (
            lambda token: token
        )
        navigation = (
            ROUTE_MODULE.RegisteredCatalogRef(
                token="catalog:" + "d" * 64,
                title="Catalog A",
                kind="acquisition",
            ),
            ROUTE_MODULE.RegisteredCatalogRef(
                token="catalog:" + "e" * 64,
                title="Catalog B",
                kind="acquisition",
            ),
        )
        ROUTE_MODULE.build_registered_catalog_view = (
            lambda *args, **kwargs: catalog_view(
                root_token,
                navigation=navigation,
            )
        )

        response = self.get(root_token)

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Catalog A", body)
        self.assertIn("Catalog B", body)


if __name__ == "__main__":
    unittest.main()
