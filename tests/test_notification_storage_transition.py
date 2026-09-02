import ast
import re
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)


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


QUEUE_TEMPLATE = assignment_value("QUEUE_HTML")


class NotificationStorageTransitionTests(unittest.TestCase):
    def test_a_storage_keys_are_declared_once(self):
        self.assertRegex(
            QUEUE_TEMPLATE,
            r"const\s+notificationStorageKey\s*=\s*"
            r"['\"]opdsDeskLastNotificationId['\"]\s*;",
        )
        self.assertRegex(
            QUEUE_TEMPLATE,
            r"const\s+legacyNotificationStorageKey\s*=\s*"
            r"['\"]flibustaLastNotificationId['\"]\s*;",
        )
        self.assertEqual(QUEUE_TEMPLATE.count("opdsDeskLastNotificationId"), 1)
        self.assertEqual(QUEUE_TEMPLATE.count("flibustaLastNotificationId"), 1)

    def test_b_new_key_is_read_first_and_has_priority(self):
        new_read = "localStorage.getItem(notificationStorageKey)"
        strict_fallback = "if(storedNotificationId===null)"
        legacy_read = "localStorage.getItem(legacyNotificationStorageKey)"

        self.assertIn(new_read, QUEUE_TEMPLATE)
        self.assertIn(strict_fallback, QUEUE_TEMPLATE)
        self.assertIn(legacy_read, QUEUE_TEMPLATE)
        self.assertLess(QUEUE_TEMPLATE.index(new_read), QUEUE_TEMPLATE.index(strict_fallback))
        self.assertLess(
            QUEUE_TEMPLATE.index(strict_fallback),
            QUEUE_TEMPLATE.index(legacy_read),
        )
        self.assertNotRegex(
            QUEUE_TEMPLATE,
            r"if\s*\(\s*!\s*storedNotificationId\s*\)",
        )

    def test_c_legacy_value_migrates_to_new_key(self):
        migration_start = QUEUE_TEMPLATE.index("if(storedNotificationId===null)")
        migration_end = QUEUE_TEMPLATE.index(
            "let lastNotificationId=Number(storedNotificationId||0)",
            migration_start,
        )
        migration = QUEUE_TEMPLATE[migration_start:migration_end]

        self.assertIn("if(legacyNotificationId!==null)", migration)
        self.assertIn("storedNotificationId=legacyNotificationId", migration)
        self.assertIn(
            "localStorage.setItem(notificationStorageKey,legacyNotificationId)",
            migration,
        )

    def test_d_polling_writes_only_new_key_and_never_removes_legacy(self):
        poll_start = QUEUE_TEMPLATE.index("async function pollBridgeNotification()")
        poll_end = QUEUE_TEMPLATE.index(
            "setTimeout(pollBridgeNotification,3000)",
            poll_start,
        )
        poll = QUEUE_TEMPLATE[poll_start:poll_end]

        self.assertIn(
            "lastNotificationId=n.id;"
            "localStorage.setItem(notificationStorageKey,String(n.id))",
            poll,
        )
        self.assertNotIn(
            "localStorage.setItem(legacyNotificationStorageKey",
            QUEUE_TEMPLATE,
        )
        self.assertNotIn(
            "localStorage.removeItem(legacyNotificationStorageKey",
            QUEUE_TEMPLATE,
        )
        self.assertNotIn("localStorage.removeItem('flibustaLastNotificationId'", QUEUE_TEMPLATE)

    def test_e_storage_cursor_is_not_sent_by_fetch(self):
        fetch_lines = [
            line
            for line in QUEUE_TEMPLATE.splitlines()
            if "fetch(" in line
        ]
        self.assertTrue(fetch_lines)
        for line in fetch_lines:
            with self.subTest(fetch=line.strip()):
                self.assertNotIn("notificationStorageKey", line)
                self.assertNotIn("legacyNotificationStorageKey", line)
                self.assertNotIn("storedNotificationId", line)
                self.assertNotIn("lastNotificationId", line)


if __name__ == "__main__":
    unittest.main()
