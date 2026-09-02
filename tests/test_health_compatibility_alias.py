import ast
import json
import os
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import Mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)
LEGACY_OPDS_BASE = "https://legacy-opds.test"


def health_snapshot_source():
    node = next(
        item
        for item in TREE.body
        if isinstance(item, ast.FunctionDef) and item.name == "health_snapshot"
    )
    return node, ast.get_source_segment(SOURCE, node) or ""


def load_health_snapshot(request_get):
    node, _ = health_snapshot_source()
    namespace = {
        "time": types.SimpleNamespace(time=lambda: 1234.0),
        "health_cache_lock": threading.Lock(),
        "health_cache": {"data": None, "time": 0},
        "HEALTH_CACHE_TTL": 30,
        "json": json,
        "requests": types.SimpleNamespace(get=request_get),
        "LEGACY_OPDS_BASE": LEGACY_OPDS_BASE,
        "os": os,
        "DESTINATION": str(PROJECT_ROOT),
        "disk_status": lambda: {
            "low": False,
            "free_text": "1 ГБ",
            "total_text": "2 ГБ",
            "min_free_gb": 1,
        },
        "queue_counts": lambda: {"pending": 0, "error": 0},
        "queue_is_worker_active": lambda: False,
        "queue_setting_get": lambda key, default=None: default,
        "next_auto_run_info": lambda: {
            "state": "ok",
            "text": "scheduled",
            "detail": "scheduled",
        },
        "format_time": lambda value: "checked",
    }
    exec(
        compile(ast.Module(body=[node], type_ignores=[]), "app.py", "exec"),
        namespace,
    )
    return namespace["health_snapshot"]


class ResponseStub:
    def __init__(self, status_code):
        self.status_code = status_code
        self.close = Mock()


class HealthCompatibilityAliasTests(unittest.TestCase):
    def snapshot_for_status(self, status_code):
        request_get = Mock(return_value=ResponseStub(status_code))
        return load_health_snapshot(request_get)(force=True), request_get

    def test_a_success_http_exposes_equal_neutral_and_legacy_aliases(self):
        snapshot, _ = self.snapshot_for_status(204)

        self.assertIn("legacy_opds", snapshot)
        self.assertIn("flibusta", snapshot)
        self.assertEqual(snapshot["legacy_opds"], snapshot["flibusta"])
        self.assertEqual(
            snapshot["legacy_opds"],
            {
                "state": "ok",
                "text": "Доступна · HTTP 204",
                "detail": LEGACY_OPDS_BASE,
            },
        )

    def test_b_non_success_http_preserves_error_semantics_for_both_aliases(self):
        snapshot, _ = self.snapshot_for_status(503)

        expected = {
            "state": "error",
            "text": "HTTP 503",
            "detail": LEGACY_OPDS_BASE,
        }
        self.assertEqual(snapshot["legacy_opds"], expected)
        self.assertEqual(snapshot["flibusta"], expected)

    def test_c_exception_preserves_error_semantics_for_both_aliases(self):
        request_get = Mock(side_effect=RuntimeError("transport failed"))
        snapshot = load_health_snapshot(request_get)(force=True)

        expected = {
            "state": "error",
            "text": "Недоступна",
            "detail": "transport failed",
        }
        self.assertEqual(snapshot["legacy_opds"], expected)
        self.assertEqual(snapshot["flibusta"], expected)

    def test_d_request_still_targets_legacy_opds_endpoint(self):
        _, request_get = self.snapshot_for_status(200)

        self.assertEqual(request_get.call_args.args, (f"{LEGACY_OPDS_BASE}/opds",))

    def test_e_request_options_remain_unchanged(self):
        _, request_get = self.snapshot_for_status(200)

        self.assertEqual(
            request_get.call_args.kwargs,
            {
                "timeout": (3, 5),
                "allow_redirects": True,
                "stream": True,
            },
        )

    def test_f_alias_objects_expose_the_same_fields_only(self):
        snapshot, _ = self.snapshot_for_status(200)

        self.assertEqual(
            set(snapshot["legacy_opds"]),
            set(snapshot["flibusta"]),
        )
        self.assertEqual(set(snapshot["legacy_opds"]), {"state", "text", "detail"})

    def test_g_legacy_alias_assignment_remains_in_health_snapshot(self):
        _, source = health_snapshot_source()

        self.assertIn('data["legacy_opds"] = health_entry', source)
        self.assertIn('data["flibusta"] = health_entry', source)


if __name__ == "__main__":
    unittest.main()
