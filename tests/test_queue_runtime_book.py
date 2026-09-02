import ast
import contextlib
import json
import os
import sys
import threading
import types
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FakeTime:
    @staticmethod
    def time():
        return 1000.0

    @staticmethod
    def sleep(delay):
        return None


def load_worker_module():
    source = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    wanted = {"queue_runtime_book", "run_queue_worker"}
    constants = {"LEGACY_QUEUE_SOURCE_ID", "BULK_DELAY"}
    body = []
    for node in tree.body:
        if getattr(node, "name", None) in wanted:
            body.append(node)
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in constants
            for target in node.targets
        ):
            body.append(node)

    module = types.ModuleType("isolated_queue_runtime_book_test")
    sys.modules[module.__name__] = module
    module.__dict__.update(json=json, os=os, time=FakeTime())
    exec(
        compile(ast.Module(body=body, type_ignores=[]), "app.py", "exec"),
        module.__dict__,
    )
    module.__source_text__ = source
    module.__source_tree__ = tree
    return module


WORKER = load_worker_module()


class FakeConnection:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, sql):
        row = self.rows.pop(0) if self.rows else None
        return types.SimpleNamespace(fetchone=lambda: row)


class QueueRuntimeBookTests(unittest.TestCase):
    def test_a_db_neutral_identity_overrides_legacy_json_without_mutation(self):
        raw_book = {
            "source_id": WORKER.LEGACY_QUEUE_SOURCE_ID,
            "id": "123",
            "title": "Example",
            "author": "Writer",
            "epub_url": "https://files.example/book.epub",
            "fb2_url": "https://files.example/book.fb2",
            "epub_mime_type": "application/epub+zip",
            "fb2_mime_type": "application/x-fictionbook+xml",
            "unknown": {"preserved": True},
        }
        original = dict(raw_book)
        runtime_book = WORKER.queue_runtime_book(
            {
                "source_id": "source-a",
                "source_item_id": "real-id",
                "flibusta_id": "compatibility-id",
            },
            raw_book,
        )
        self.assertEqual(runtime_book["source_id"], "source-a")
        self.assertEqual(runtime_book["id"], "real-id")
        for key in (
            "title",
            "author",
            "epub_url",
            "fb2_url",
            "epub_mime_type",
            "fb2_mime_type",
            "unknown",
        ):
            self.assertEqual(runtime_book[key], raw_book[key])
        self.assertEqual(raw_book, original)
        self.assertIsNot(runtime_book, raw_book)

    def test_b_db_legacy_identity_overrides_neutral_json(self):
        runtime_book = WORKER.queue_runtime_book(
            {
                "source_id": WORKER.LEGACY_QUEUE_SOURCE_ID,
                "source_item_id": "123",
                "flibusta_id": "123",
            },
            {
                "source_id": "source-a",
                "id": "opaque",
                "epub_url": "https://files.example/book.epub",
            },
        )
        self.assertEqual(
            (runtime_book["source_id"], runtime_book["id"]),
            (WORKER.LEGACY_QUEUE_SOURCE_ID, "123"),
        )
        self.assertEqual(
            runtime_book["epub_url"],
            "https://files.example/book.epub",
        )

    def test_c_opaque_source_item_ids_are_preserved_as_strings(self):
        item_ids = (
            "urn:uuid:0d508a30-073f-4028-b522-592a2acbdb98",
            "tag:catalog.example,2026:item",
            "book?id=10&edition=2",
        )
        for item_id in item_ids:
            with self.subTest(item_id=item_id):
                runtime_book = WORKER.queue_runtime_book(
                    {
                        "source_id": "source-a",
                        "source_item_id": item_id,
                        "flibusta_id": "ignored",
                    },
                    {"id": "wrong"},
                )
                self.assertEqual(runtime_book["id"], item_id)

    def test_d_missing_identity_columns_use_defensive_legacy_fallbacks(self):
        runtime_book = WORKER.queue_runtime_book(
            {"flibusta_id": 123},
            {"source_id": "wrong-source", "id": "wrong-id"},
        )
        self.assertEqual(
            (runtime_book["source_id"], runtime_book["id"]),
            (WORKER.LEGACY_QUEUE_SOURCE_ID, "123"),
        )

    def test_e_non_dict_book_values_are_rejected(self):
        for value in ([], "string", 123, None):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    ValueError,
                    "Некорректные данные книги в очереди",
                ):
                    WORKER.queue_runtime_book(
                        {"source_id": "source-a", "source_item_id": "item"},
                        value,
                    )

    def test_f_helper_has_only_persisted_identity_dependencies(self):
        node = next(
            node
            for node in WORKER.__source_tree__.body
            if getattr(node, "name", None) == "queue_runtime_book"
        )
        source = ast.get_source_segment(WORKER.__source_text__, node) or ""
        for forbidden in (
            "current_source_id",
            "current_source_config",
            "APP_CONFIG",
            "LEGACY_OPDS_BASE",
            "normalize_opds_url",
            "save_epub",
            "save_fb2",
            "save_opds_acquisition",
            "int(",
            "isdigit",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

    def run_worker_with_row(self, row):
        rows = [row, None]
        finishes = []
        downloads = []
        settings = {"paused": "0", "current_run_id": "run-1"}
        WORKER.queue_db_lock = threading.Lock()
        WORKER.queue_worker_lock = threading.Lock()
        WORKER.download_serial_lock = threading.Lock()
        WORKER.queue_worker_thread = object()
        WORKER.queue_connect = lambda: contextlib.nullcontext(FakeConnection(rows))
        WORKER.queue_setting_get = lambda key, default="": settings.get(key, default)
        WORKER.queue_setting_set = lambda key, value: settings.__setitem__(key, value)
        WORKER.queue_disk_guard = lambda: (True, "")
        WORKER.queue_run_sync_total = lambda: None
        WORKER.queue_update_item = lambda *args, **kwargs: None
        WORKER.queue_finish_item = (
            lambda *args, **kwargs: finishes.append((args, kwargs))
        )
        WORKER.apply_local_status = lambda value: value.update(exists_any=False)
        WORKER.apply_duplicate_local_status = (
            lambda value: value.update(duplicate_exists_any=False)
        )
        WORKER.choose_bulk_format = lambda value, mode: "epub"

        def download_one_book(value, file_format, **kwargs):
            downloads.append((dict(value), file_format, kwargs))
            return "downloaded", "missing-test-file", 1.0

        WORKER.download_one_book = download_one_book
        WORKER.download_error_info = lambda exc: {
            "label": "Ошибка загрузки",
            "retryable": False,
        }
        WORKER.compact_error = lambda exc, limit=None: str(exc)
        WORKER.queue_pending_count = lambda: 0
        WORKER.queue_run_finalize = lambda *args: None
        WORKER.queue_create_completion_notification = lambda: None
        WORKER.queue_run_mark_paused = lambda *args: None
        WORKER.run_queue_worker()
        return finishes, downloads, settings

    def test_g_worker_passes_db_identity_to_downloader_before_transport_choice(self):
        persisted_json = json.dumps(
            {
                "source_id": WORKER.LEGACY_QUEUE_SOURCE_ID,
                "id": "123",
                "title": "Example",
                "author": "Writer",
                "epub": True,
                "epub_url": "https://files.example/book.epub",
            }
        )
        row = {
            "id": 1,
            "source_id": "source-a",
            "source_item_id": "real-id",
            "flibusta_id": "real-id",
            "book_json": persisted_json,
            "download_duplicates": 0,
            "format_mode": "auto",
        }
        finishes, downloads, settings = self.run_worker_with_row(row)
        self.assertEqual(len(downloads), 1)
        runtime_book = downloads[0][0]
        self.assertEqual(
            (runtime_book["source_id"], runtime_book["id"]),
            ("source-a", "real-id"),
        )
        self.assertEqual(
            runtime_book["epub_url"],
            "https://files.example/book.epub",
        )
        self.assertEqual(row["book_json"], persisted_json)
        self.assertEqual(finishes[0][0][1], "done")
        self.assertEqual(settings["run_active"], "0")

    def test_h_worker_treats_non_dict_json_as_data_error_without_download(self):
        row = {
            "id": 2,
            "source_id": "source-a",
            "source_item_id": "real-id",
            "flibusta_id": "real-id",
            "book_json": "[]",
            "download_duplicates": 0,
            "format_mode": "auto",
        }
        finishes, downloads, settings = self.run_worker_with_row(row)
        self.assertEqual(downloads, [])
        self.assertEqual(len(finishes), 1)
        args, kwargs = finishes[0]
        self.assertEqual(args[:3], (2, "error", "Ошибка данных"))
        self.assertEqual(kwargs["error_category"], "Данные очереди")
        self.assertIn("Некорректные данные книги в очереди", kwargs["error"])
        self.assertEqual(settings["run_active"], "0")


if __name__ == "__main__":
    unittest.main()
