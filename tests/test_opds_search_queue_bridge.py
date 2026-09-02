import ast
import copy
import dataclasses
import hashlib
import json
import sys
import threading
import types
import unittest
from pathlib import Path

from flask import Flask, Response, flash, redirect, request, session, url_for


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_bridge_module():
    source = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    wanted = {
        "SourceConfig",
        "normalize_opds_search_query",
        "make_opds_search_book_token",
        "register_opds_search_book",
        "get_opds_search_book",
        "resolve_opds_search_book",
        "clear_opds_search_book_registry",
        "unique_opaque_ids",
        "resolve_opds_search_selection",
        "choose_catalog_book_format",
        "source_namespace",
        "_opaque_key_part",
        "catalog_selection_storage_key",
        "current_source_id",
        "catalog_selection_clear_token",
        "mark_catalog_selection_clear",
        "catalog_selection_clear_pending",
        "opds_search_queue_add",
    }
    assignments = {
        "MAX_OPDS_SEARCH_BOOK_REGISTRY",
        "opds_search_book_registry",
        "opds_search_book_registry_lock",
        "MAX_OPDS_SEARCH_QUEUE_SELECTION",
    }
    body = []
    for node in tree.body:
        if getattr(node, "name", None) in wanted:
            body.append(node)
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in assignments
            for target in node.targets
        ):
            body.append(node)

    module = types.ModuleType("isolated_opds_search_queue_bridge_test")
    sys.modules[module.__name__] = module
    module.__dict__.update(
        app=Flask(module.__name__),
        copy=copy,
        dataclass=dataclasses.dataclass,
        flash=flash,
        hashlib=hashlib,
        json=json,
        redirect=redirect,
        request=request,
        Response=Response,
        session=session,
        threading=threading,
        url_for=url_for,
    )
    exec(
        compile(ast.Module(body=body, type_ignores=[]), "app.py", "exec"),
        module.__dict__,
    )
    module.app.add_url_rule(
        "/search/opds",
        endpoint="opds_search_page",
        view_func=lambda: "search",
    )
    module.app.secret_key = "test-secret"
    module.app.testing = True
    module.__source_text__ = source
    module.__source_tree__ = tree
    return module


BRIDGE = load_bridge_module()


def server_book(
    book_id,
    *,
    title=None,
    epub_url="https://server.example/book.epub",
    fb2_url="https://server.example/book.fb2",
):
    return {
        "source_id": "wrong-source-in-book",
        "id": book_id,
        "title": title or f"Server title {book_id}",
        "author": "Server author",
        "authors": ["Server author"],
        "epub": bool(epub_url),
        "fb2": bool(fb2_url),
        "epub_url": epub_url,
        "fb2_url": fb2_url,
        "epub_mime_type": "application/epub+zip",
        "fb2_mime_type": "application/x-fictionbook+xml",
        "acquisition_links": [{"href": epub_url or fb2_url}],
        "metadata": {"server": True},
    }


class OPDSSearchQueueBridgeTests(unittest.TestCase):
    def setUp(self):
        BRIDGE.clear_opds_search_book_registry()
        self.current_source = BRIDGE.SourceConfig(
            source_id="source-a",
            root_url="https://catalog.example/opds",
            display_name="Catalog",
        )
        self.queue_calls = []
        self.existing_ids = set()
        self.queue_results = {}
        BRIDGE.current_source_config = lambda: self.current_source

        def apply_local_status(book):
            book["exists_any"] = book["id"] in self.existing_ids
            return book

        def queue_add_book(book, format_mode, download_duplicates):
            self.queue_calls.append(
                (copy.deepcopy(book), format_mode, download_duplicates)
            )
            return self.queue_results.get(book["id"], True)

        BRIDGE.apply_local_status = apply_local_status
        BRIDGE.queue_add_book = queue_add_book
        self.client = BRIDGE.app.test_client()

    def tearDown(self):
        BRIDGE.clear_opds_search_book_registry()

    def register(self, book_id, query="query", source_id="source-a", **kwargs):
        return BRIDGE.register_opds_search_book(
            source_id,
            query,
            server_book(book_id, **kwargs),
        )

    def flashes(self):
        with self.client.session_transaction() as session:
            return [message for _, message in session.get("_flashes", ())]

    def clear_markers(self):
        with self.client.session_transaction() as session:
            return dict(session.get("catalog_selections_to_clear", {}))

    def test_a_success_uses_two_server_snapshots_and_ignores_client_injection(self):
        self.register("id-a", title="Server A")
        self.register("id-b", title="Server B")

        def forbidden(*args, **kwargs):
            raise AssertionError("POST bridge attempted network access")

        BRIDGE.OPDSHTTPClient = forbidden
        BRIDGE.load_current_opds_search_page = forbidden
        BRIDGE.requests = types.SimpleNamespace(get=forbidden, post=forbidden)
        response = self.client.post(
            "/search/opds/queue",
            data={
                "q": "query",
                "book_id": ["id-a", "id-b"],
                "format_mode": "hostile-mode",
                "source_id": "legacy-v1",
                "title": "Fake title",
                "author": "Fake author",
                "epub_url": "https://attacker.example/a.epub",
                "fb2_url": "https://attacker.example/a.fb2",
                "epub": "0",
                "fb2": "0",
            },
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["Location"], "/search/opds?q=query")
        self.assertEqual(len(self.queue_calls), 2)
        for index, expected_id in enumerate(("id-a", "id-b")):
            queued, format_mode, download_duplicates = self.queue_calls[index]
            self.assertEqual(queued["source_id"], "source-a")
            self.assertEqual(queued["id"], expected_id)
            self.assertEqual(queued["title"], f"Server {expected_id[-1].upper()}")
            self.assertEqual(
                queued["epub_url"],
                "https://server.example/book.epub",
            )
            self.assertEqual(format_mode, "auto")
            self.assertFalse(download_duplicates)
        self.assertIn("В очередь добавлено: 2", self.flashes())
        expected_clear_token = "clear:" + BRIDGE.catalog_selection_storage_key(
            "source-a",
            "search",
            "query",
        )
        self.assertEqual(self.clear_markers(), {expected_clear_token: True})
        stored = BRIDGE.resolve_opds_search_book("source-a", "query", "id-a")
        self.assertNotIn("exists_any", stored)

    def test_b_registry_miss_is_atomic_and_uses_stale_selection_redirect(self):
        self.register("id-a")
        response = self.client.post(
            "/search/opds/queue",
            data={"q": "query", "book_id": ["id-a", "missing-id"]},
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.queue_calls, [])
        self.assertIn(
            "Выбранные книги устарели. Обновите результаты поиска и повторите выбор.",
            self.flashes(),
        )
        self.assertEqual(self.clear_markers(), {})

    def test_c_current_source_change_cannot_resolve_old_source_snapshot(self):
        self.register("same-id", source_id="source-a")
        self.current_source = BRIDGE.SourceConfig(
            source_id="source-b",
            root_url="https://other.example/opds",
            display_name="Other",
        )
        response = self.client.post(
            "/search/opds/queue",
            data={"q": "query", "book_id": "same-id"},
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.queue_calls, [])
        self.assertIn("устарели", self.flashes()[0])

    def test_d_query_context_mismatch_cannot_resolve_snapshot(self):
        self.register("same-id", query="query-one")
        response = self.client.post(
            "/search/opds/queue",
            data={"q": "query-two", "book_id": "same-id"},
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.queue_calls, [])
        self.assertIn("устарели", self.flashes()[0])

    def test_e_duplicate_opaque_ids_are_processed_once_in_first_seen_order(self):
        item_ids = (
            "urn:uuid:0d508a30-073f-4028-b522-592a2acbdb98",
            "tag:catalog.example,2026:item",
            "book?id=10&edition=2",
        )
        for item_id in item_ids:
            self.register(item_id)
        response = self.client.post(
            "/search/opds/queue",
            data={
                "q": "query",
                "book_id": [
                    item_ids[0],
                    item_ids[0],
                    item_ids[1],
                    item_ids[2],
                    item_ids[1],
                ],
            },
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            [call[0]["id"] for call in self.queue_calls],
            list(item_ids),
        )

    def test_f_local_unsupported_already_queued_and_added_counts_are_separate(self):
        self.register("local")
        self.register("unsupported", epub_url="", fb2_url="https://server/fb2")
        self.register("queued")
        self.register("added")
        self.existing_ids.add("local")
        self.queue_results["queued"] = False
        response = self.client.post(
            "/search/opds/queue",
            data={
                "q": "query",
                "book_id": ["local", "unsupported", "queued", "added"],
                "format_mode": "epub",
            },
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            [call[0]["id"] for call in self.queue_calls],
            ["queued", "added"],
        )
        summary = self.flashes()[0]
        self.assertIn("В очередь добавлено: 1", summary)
        self.assertIn("уже локально: 1", summary)
        self.assertIn("уже в очереди: 1", summary)
        self.assertIn("неподдерживаемый формат: 1", summary)

    def test_g_invalid_query_empty_selection_and_limit_return_400(self):
        responses = [
            self.client.post(
                "/search/opds/queue",
                data={"q": "", "book_id": "id-a"},
            ),
            self.client.post(
                "/search/opds/queue",
                data={"q": "query"},
            ),
        ]
        original_limit = BRIDGE.MAX_OPDS_SEARCH_QUEUE_SELECTION
        BRIDGE.MAX_OPDS_SEARCH_QUEUE_SELECTION = 2
        try:
            responses.append(
                self.client.post(
                    "/search/opds/queue",
                    data={"q": "query", "book_id": ["a", "b", "c"]},
                )
            )
        finally:
            BRIDGE.MAX_OPDS_SEARCH_QUEUE_SELECTION = original_limit
        self.assertEqual([response.status_code for response in responses], [400, 400, 400])
        self.assertIn("Не выбрана ни одна книга", responses[1].get_data(as_text=True))
        self.assertIn("слишком много", responses[2].get_data(as_text=True))
        self.assertEqual(self.queue_calls, [])
        self.assertEqual(self.clear_markers(), {})

    def test_h_unconfigured_source_returns_409_without_resolution_or_queue(self):
        self.current_source = BRIDGE.SourceConfig("", "", "")
        response = self.client.post(
            "/search/opds/queue",
            data={"q": "query", "book_id": "id-a"},
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("OPDS-источник не настроен", response.get_data(as_text=True))
        self.assertEqual(self.queue_calls, [])
        self.assertEqual(self.clear_markers(), {})

    def test_i_pure_resolver_deduplicates_and_returns_independent_snapshots(self):
        self.register("id-a")
        self.register("id-b")
        resolved = BRIDGE.resolve_opds_search_selection(
            "source-a",
            "  query  ",
            ["id-a", "id-a", "id-b"],
        )
        self.assertEqual([book["id"] for book in resolved], ["id-a", "id-b"])
        resolved[0]["title"] = "Changed"
        self.assertNotEqual(
            BRIDGE.resolve_opds_search_book(
                "source-a", "query", "id-a"
            )["title"],
            "Changed",
        )

    def test_j_pure_resolver_has_no_network_config_cache_or_queue_dependencies(self):
        node = next(
            node
            for node in BRIDGE.__source_tree__.body
            if getattr(node, "name", None) == "resolve_opds_search_selection"
        )
        source = ast.get_source_segment(BRIDGE.__source_text__, node) or ""
        for forbidden in (
            "current_source_config",
            "current_source_id",
            "APP_CONFIG",
            "OPDSHTTPClient",
            "requests",
            "load_current_opds_search_page",
            "load_cached_opds_search_page",
            "opds_search_page_cache",
            "queue_add_book",
            "apply_local_status",
            "int(",
            "isdigit",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

    def test_k_route_is_post_only_and_reads_only_trusted_form_fields(self):
        self.assertEqual(self.client.get("/search/opds/queue").status_code, 405)
        node = next(
            node
            for node in BRIDGE.__source_tree__.body
            if getattr(node, "name", None) == "opds_search_queue_add"
        )
        trusted = set()
        for child in ast.walk(node):
            if not isinstance(child, ast.Call) or not isinstance(child.func, ast.Attribute):
                continue
            if child.func.attr not in {"get", "getlist"} or not child.args:
                continue
            form = child.func.value
            if not (
                isinstance(form, ast.Attribute)
                and form.attr == "form"
                and isinstance(form.value, ast.Name)
                and form.value.id == "request"
            ):
                continue
            if isinstance(child.args[0], ast.Constant):
                trusted.add(child.args[0].value)
        self.assertEqual(trusted, {"q", "book_id", "format_mode"})

    def test_l_zero_added_still_marks_selection_for_clear(self):
        self.register("already-queued")
        self.queue_results["already-queued"] = False
        response = self.client.post(
            "/search/opds/queue",
            data={"q": "query", "book_id": "already-queued"},
        )
        self.assertEqual(response.status_code, 303)
        self.assertIn("В очередь добавлено: 0", self.flashes()[0])
        self.assertEqual(len(self.clear_markers()), 1)


if __name__ == "__main__":
    unittest.main()
