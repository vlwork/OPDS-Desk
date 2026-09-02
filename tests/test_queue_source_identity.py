import ast
import copy
import contextlib
import dataclasses
import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_queue_module():
    source = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    wanted = {
        "display_text",
        "queue_connect",
        "init_queue_db",
        "CatalogRef",
        "queue_book_identity",
        "queue_book_json_snapshot",
        "queue_active_source_item_ids",
        "queue_active_book_ids",
        "queue_active_exists",
        "queue_add_book",
        "queue_retry_error_copies",
    }
    constants = {
        "LEGACY_QUEUE_SOURCE_ID",
        "QUEUE_DEFAULT_TIME",
        "QUEUE_DEFAULT_TZ_OFFSET",
        "QUEUE_DEFAULT_MIN_FREE_GB",
    }
    body = []
    for node in tree.body:
        if getattr(node, "name", None) in wanted:
            body.append(node)
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in constants
            for target in node.targets
        ):
            body.append(node)

    module = types.ModuleType("isolated_queue_source_identity_test")
    sys.modules[module.__name__] = module
    module.__dict__.update(
        copy=copy,
        dataclass=dataclasses.dataclass,
        json=json,
        os=os,
        queue_db_lock=threading.Lock(),
        re=re,
        sqlite3=sqlite3,
        time=time,
    )
    exec(
        compile(ast.Module(body=body, type_ignores=[]), "app.py", "exec"),
        module.__dict__,
    )
    production_queue_connect = module.queue_connect
    module.queue_connect = lambda: contextlib.closing(production_queue_connect())
    module.__source_text__ = source
    module.__source_tree__ = tree
    return module


QUEUE = load_queue_module()


def create_pre_identity_schema(path, source_aware=False):
    identity_columns = """
            source_id TEXT NOT NULL DEFAULT '',
            source_item_id TEXT NOT NULL DEFAULT '',
    """ if source_aware else ""
    with contextlib.closing(sqlite3.connect(path)) as conn:
        conn.executescript(
            f"""
            CREATE TABLE queue_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                flibusta_id TEXT NOT NULL,
                {identity_columns}
                title TEXT NOT NULL,
                author TEXT NOT NULL,
                book_json TEXT NOT NULL,
                format_mode TEXT NOT NULL DEFAULT 'auto',
                download_duplicates INTEGER NOT NULL DEFAULT 0,
                priority INTEGER NOT NULL DEFAULT 0,
                run_id TEXT NOT NULL DEFAULT '',
                downloaded_bytes INTEGER NOT NULL DEFAULT 0,
                error_category TEXT NOT NULL DEFAULT '',
                retry_queued INTEGER NOT NULL DEFAULT 0,
                retry_of_id INTEGER,
                status TEXT NOT NULL DEFAULT 'pending',
                added_at REAL NOT NULL,
                started_at REAL,
                finished_at REAL,
                attempts INTEGER NOT NULL DEFAULT 0,
                detail TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                origin_kind TEXT NOT NULL DEFAULT '',
                origin_id TEXT NOT NULL DEFAULT '',
                origin_name TEXT NOT NULL DEFAULT ''
            );
            """
        )
        conn.commit()


def queue_book(source_id=None, item_id="opaque-id", title="Example"):
    book = {
        "id": item_id,
        "title": title,
        "author": "Writer",
    }
    if source_id is not None:
        book["source_id"] = source_id
    return book


class QueueSourceIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        QUEUE.QUEUE_DB_FILE = str(Path(self.temp_dir.name) / "queue.db")

    def tearDown(self):
        self.temp_dir.cleanup()

    def rows(self, sql, params=()):
        with QUEUE.queue_connect() as conn:
            return conn.execute(sql, params).fetchall()

    def test_a_fresh_schema_has_source_identity_and_composite_active_index(self):
        QUEUE.init_queue_db()
        with QUEUE.queue_connect() as conn:
            columns = {
                row["name"]: row
                for row in conn.execute("PRAGMA table_info(queue_items)")
            }
            indexes = {
                row["name"]: row
                for row in conn.execute("PRAGMA index_list(queue_items)")
            }
            index_columns = [
                row["name"]
                for row in conn.execute(
                    "PRAGMA index_info(uq_queue_active_source_item)"
                )
            ]
        self.assertTrue({"flibusta_id", "source_id", "source_item_id"} <= columns.keys())
        self.assertEqual(columns["source_id"]["notnull"], 1)
        self.assertEqual(columns["source_item_id"]["notnull"], 1)
        self.assertIn("uq_queue_active_source_item", indexes)
        self.assertEqual(indexes["uq_queue_active_source_item"]["unique"], 1)
        self.assertEqual(index_columns, ["source_id", "source_item_id"])
        self.assertNotIn("uq_queue_active_flibusta", indexes)

    def test_b_legacy_schema_is_backfilled_without_rewriting_row_history(self):
        create_pre_identity_schema(QUEUE.QUEUE_DB_FILE)
        original_book_json = json.dumps({"id": "12345", "legacy": True})
        with contextlib.closing(sqlite3.connect(QUEUE.QUEUE_DB_FILE)) as conn:
            conn.execute(
                """INSERT INTO queue_items(
                       flibusta_id,title,author,book_json,status,added_at,
                       started_at,finished_at,attempts,detail,error,error_category,
                       retry_queued,retry_of_id,run_id
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "12345", "Legacy title", "Legacy author", original_book_json,
                    "error", 10.0, 11.0, 12.0, 3, "history detail", "history error",
                    "network", 1, 77, "legacy-run",
                ),
            )
            conn.execute(
                """CREATE UNIQUE INDEX uq_queue_active_flibusta
                   ON queue_items(flibusta_id)
                WHERE status IN ('pending','downloading')"""
            )
            conn.commit()
        QUEUE.init_queue_db()
        row = self.rows("SELECT * FROM queue_items")[0]
        self.assertEqual(row["source_id"], QUEUE.LEGACY_QUEUE_SOURCE_ID)
        self.assertEqual(row["source_item_id"], "12345")
        self.assertEqual(row["flibusta_id"], "12345")
        self.assertEqual(row["book_json"], original_book_json)
        self.assertEqual(row["status"], "error")
        self.assertEqual(row["detail"], "history detail")
        self.assertEqual(row["error"], "history error")
        self.assertEqual(row["attempts"], 3)
        self.assertEqual(row["retry_of_id"], 77)
        index_names = {
            row["name"] for row in self.rows("PRAGMA index_list(queue_items)")
        }
        self.assertNotIn("uq_queue_active_flibusta", index_names)

    def test_c_migration_deduplicates_pairs_but_keeps_same_id_across_sources(self):
        create_pre_identity_schema(QUEUE.QUEUE_DB_FILE, source_aware=True)
        rows = (
            ("source-a", "same-id", "A first", "pending"),
            ("source-a", "same-id", "A duplicate", "pending"),
            ("source-b", "same-id", "B independent", "pending"),
            ("source-c", "preferred", "C pending", "pending"),
            ("source-c", "preferred", "C downloading", "downloading"),
        )
        with contextlib.closing(sqlite3.connect(QUEUE.QUEUE_DB_FILE)) as conn:
            conn.executemany(
                """INSERT INTO queue_items(
                       flibusta_id,source_id,source_item_id,title,author,
                       book_json,status,added_at
                   ) VALUES(?,?,?,?,?,'{}',?,1.0)""",
                [(item_id, source_id, item_id, title, "Writer", status)
                 for source_id, item_id, title, status in rows],
            )
            conn.commit()
        QUEUE.init_queue_db()
        active = self.rows(
            """SELECT source_id,source_item_id,title,status FROM queue_items
               WHERE status IN ('pending','downloading') ORDER BY id"""
        )
        self.assertEqual(
            [(row["source_id"], row["source_item_id"], row["title"]) for row in active],
            [
                ("source-a", "same-id", "A first"),
                ("source-b", "same-id", "B independent"),
                ("source-c", "preferred", "C downloading"),
            ],
        )
        skipped = self.rows(
            """SELECT title,error_category FROM queue_items
               WHERE status='skipped' ORDER BY id"""
        )
        self.assertEqual(
            [(row["title"], row["error_category"]) for row in skipped],
            [
                ("A duplicate", "duplicate_queue"),
                ("C pending", "duplicate_queue"),
            ],
        )

    def test_d_queue_book_identity_is_opaque_and_uses_legacy_fallback(self):
        opaque_ids = (
            "urn:uuid:0d508a30-073f-4028-b522-592a2acbdb98",
            "tag:catalog.example,2026:item",
            "book?id=10&edition=2",
        )
        for item_id in opaque_ids:
            with self.subTest(item_id=item_id):
                self.assertEqual(
                    QUEUE.queue_book_identity(
                        {"source_id": "source-a", "id": item_id}
                    ),
                    ("source-a", item_id),
                )
        self.assertEqual(
            QUEUE.queue_book_identity({"id": 123}),
            (QUEUE.LEGACY_QUEUE_SOURCE_ID, "123"),
        )

    def test_e_same_pair_is_ignored_but_same_item_in_other_source_is_allowed(self):
        QUEUE.init_queue_db()
        self.assertTrue(QUEUE.queue_add_book(queue_book("source-a", "opaque-id")))
        self.assertFalse(QUEUE.queue_add_book(queue_book("source-a", "opaque-id")))
        self.assertTrue(QUEUE.queue_add_book(queue_book("source-b", "opaque-id")))
        rows = self.rows(
            """SELECT flibusta_id,source_id,source_item_id,status
               FROM queue_items ORDER BY id"""
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            [(row["source_id"], row["source_item_id"]) for row in rows],
            [("source-a", "opaque-id"), ("source-b", "opaque-id")],
        )
        self.assertTrue(all(row["flibusta_id"] == "opaque-id" for row in rows))

    def test_f_legacy_add_uses_legacy_namespace_and_mirror(self):
        QUEUE.init_queue_db()
        self.assertTrue(QUEUE.queue_add_book(queue_book(item_id="123")))
        self.assertFalse(QUEUE.queue_add_book(queue_book(item_id="123")))
        row = self.rows(
            "SELECT flibusta_id,source_id,source_item_id FROM queue_items"
        )[0]
        self.assertEqual(
            (row["flibusta_id"], row["source_id"], row["source_item_id"]),
            ("123", QUEUE.LEGACY_QUEUE_SOURCE_ID, "123"),
        )

    def test_g_active_helpers_are_isolated_by_source_and_legacy_wrapper(self):
        QUEUE.init_queue_db()
        QUEUE.queue_add_book(queue_book("source-a", "same-id"))
        QUEUE.queue_add_book(queue_book("source-a", "a-only"))
        QUEUE.queue_add_book(queue_book("source-b", "same-id"))
        QUEUE.queue_add_book(queue_book(item_id="legacy-only"))
        self.assertEqual(
            QUEUE.queue_active_source_item_ids("source-a"),
            {"same-id", "a-only"},
        )
        self.assertEqual(
            QUEUE.queue_active_source_item_ids("source-b"),
            {"same-id"},
        )
        self.assertEqual(QUEUE.queue_active_book_ids(), {"legacy-only"})
        self.assertTrue(QUEUE.queue_active_exists("same-id", source_id="source-a"))
        self.assertTrue(QUEUE.queue_active_exists("same-id", source_id="source-b"))
        self.assertFalse(QUEUE.queue_active_exists("same-id", source_id="source-c"))
        self.assertTrue(QUEUE.queue_active_exists("legacy-only"))

    def test_h_retry_copies_db_identity_and_other_source_does_not_block(self):
        QUEUE.init_queue_db()
        QUEUE.queue_add_book(queue_book("source-a", "opaque-id"))
        with QUEUE.queue_connect() as conn:
            original = conn.execute(
                "SELECT id FROM queue_items WHERE source_id='source-a'"
            ).fetchone()["id"]
            authoritative_json = json.dumps(
                {"source_id": "wrong-source", "id": "wrong-id"}
            )
            conn.execute(
                """UPDATE queue_items
                   SET status='error',run_id='retry-run',book_json=?
                   WHERE id=?""",
                (authoritative_json, original),
            )
            conn.commit()
        QUEUE.queue_add_book(queue_book("source-b", "opaque-id"))
        self.assertEqual(QUEUE.queue_retry_error_copies("retry-run"), (1, 0))
        retry = self.rows(
            "SELECT * FROM queue_items WHERE retry_of_id=?",
            (original,),
        )[0]
        self.assertEqual(
            (retry["source_id"], retry["source_item_id"], retry["flibusta_id"]),
            ("source-a", "opaque-id", "opaque-id"),
        )
        self.assertEqual(retry["book_json"], authoritative_json)
        original_row = self.rows(
            "SELECT retry_queued FROM queue_items WHERE id=?",
            (original,),
        )[0]
        self.assertEqual(original_row["retry_queued"], 1)

    def test_i_retry_is_blocked_only_by_active_row_with_same_pair(self):
        QUEUE.init_queue_db()
        QUEUE.queue_add_book(queue_book("source-a", "blocked-id"))
        with QUEUE.queue_connect() as conn:
            original = conn.execute(
                "SELECT id FROM queue_items WHERE source_id='source-a'"
            ).fetchone()["id"]
            conn.execute(
                """UPDATE queue_items SET status='error',run_id='blocked-run'
                   WHERE id=?""",
                (original,),
            )
            conn.commit()
        self.assertTrue(QUEUE.queue_add_book(queue_book("source-a", "blocked-id")))
        self.assertTrue(QUEUE.queue_add_book(queue_book("source-b", "blocked-id")))
        self.assertEqual(QUEUE.queue_retry_error_copies("blocked-run"), (0, 1))
        self.assertEqual(
            self.rows(
                "SELECT retry_queued FROM queue_items WHERE id=?",
                (original,),
            )[0]["retry_queued"],
            0,
        )

    def test_j_new_identity_helper_has_no_numeric_or_runtime_assumptions(self):
        node = next(
            node
            for node in QUEUE.__source_tree__.body
            if getattr(node, "name", None) == "queue_book_identity"
        )
        source = ast.get_source_segment(QUEUE.__source_text__, node) or ""
        for forbidden in ("int(", "isdigit", "re.", "current_source", "APP_CONFIG"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)
        self.assertEqual(QUEUE.LEGACY_QUEUE_SOURCE_ID, "legacy-v1")

    def test_k_catalog_ref_is_json_safe_without_mutating_runtime_book(self):
        QUEUE.init_queue_db()
        catalog_ref = QUEUE.CatalogRef(
            source_id="source-a",
            url="https://catalog.example/author/1",
            title="Author",
            kind="related",
        )
        acquisition_links = [
            {
                "href": "https://catalog.example/book.epub",
                "mime_type": "application/epub+zip",
                "rel": "http://opds-spec.org/acquisition",
            }
        ]
        book = {
            "source_id": "source-a",
            "id": "opaque-id",
            "title": "Example",
            "author": "Writer",
            "related": (catalog_ref,),
            "epub_url": "https://catalog.example/book.epub",
            "fb2_url": "https://catalog.example/book.fb2",
            "epub_mime_type": "application/epub+zip",
            "fb2_mime_type": "application/fb2+zip",
            "acquisition_links": acquisition_links,
            "size_bytes": 12345,
        }

        self.assertTrue(QUEUE.queue_add_book(book))

        stored = json.loads(self.rows("SELECT book_json FROM queue_items")[0][0])
        self.assertEqual(
            stored["related"],
            [
                {
                    "source_id": "source-a",
                    "url": "https://catalog.example/author/1",
                    "title": "Author",
                    "kind": "related",
                }
            ],
        )
        for field in (
            "source_id",
            "id",
            "title",
            "author",
            "epub_url",
            "fb2_url",
            "epub_mime_type",
            "fb2_mime_type",
            "acquisition_links",
            "size_bytes",
        ):
            with self.subTest(field=field):
                self.assertEqual(stored[field], book[field])
        self.assertIsInstance(book["related"][0], QUEUE.CatalogRef)
        self.assertIs(book["related"][0], catalog_ref)

    def test_l_dict_related_is_preserved_as_an_independent_copy(self):
        related = [
            {
                "source_id": "source-a",
                "url": "https://catalog.example/author/1",
                "title": "Author",
                "kind": "related",
            }
        ]
        book = {"source_id": "source-a", "id": "opaque-id", "related": related}

        snapshot = QUEUE.queue_book_json_snapshot(book)

        self.assertEqual(snapshot["related"], related)
        self.assertIsNot(snapshot, book)
        self.assertIsNot(snapshot["related"], related)
        self.assertIsNot(snapshot["related"][0], related[0])

    def test_m_unexpected_object_is_not_silently_stringified(self):
        with self.assertRaises(TypeError):
            QUEUE.queue_book_json_snapshot(
                {"source_id": "source-a", "id": "opaque-id", "unexpected": object()}
            )
        helper = next(
            node
            for node in QUEUE.__source_tree__.body
            if getattr(node, "name", None) == "queue_book_json_snapshot"
        )
        source = ast.get_source_segment(QUEUE.__source_text__, helper) or ""
        self.assertNotIn("default=str", source)
        self.assertNotIn("repr(", source)


if __name__ == "__main__":
    unittest.main()
