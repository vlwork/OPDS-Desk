import ast
import hashlib
import json
import os
import re
import tempfile
import time
import types
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)
FUNCTIONS = {
    "truncate_utf8",
    "clean_name",
    "download_filename_identity_marker",
    "legacy_duplicate_storage_title",
    "duplicate_storage_title",
    "duplicate_storage_title_candidates",
    "local_paths",
    "apply_duplicate_local_status",
    "download_one_book",
}


def load_marker_module():
    body = [
        node
        for node in TREE.body
        if isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS
    ]
    module = types.ModuleType("isolated_download_filename_markers_test")
    module.__dict__.update(
        hashlib=hashlib,
        json=json,
        os=os,
        re=re,
        time=time,
        LEGACY_QUEUE_SOURCE_ID="legacy-v1",
        MAX_AUTHOR_COMPONENT_BYTES=180,
        MAX_TITLE_COMPONENT_BYTES=220,
    )
    exec(
        compile(ast.Module(body=body, type_ignores=[]), "app.py", "exec"),
        module.__dict__,
    )
    return module


MARKERS = load_marker_module()


def book(source_id="source-a", source_item_id="123", **overrides):
    value = {
        "id": source_item_id,
        "source_id": source_id,
        "title": "Book",
        "author": "Writer",
        "duplicate_count": 2,
        "duplicate_preferred": False,
        "epub_url": "https://files.example/book.epub",
        "epub_mime_type": "application/epub+zip",
    }
    value.update(overrides)
    return value


class DownloadFilenameMarkerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        MARKERS.DESTINATION = self.temp_dir.name
        self.network_calls = []

        def save_opds_acquisition(
            url,
            destination,
            file_format,
            mime_type="",
            progress=None,
        ):
            self.network_calls.append((url, destination, file_format, mime_type))
            Path(destination).write_bytes(b"downloaded")
            return 1.0

        MARKERS.save_opds_acquisition = save_opds_acquisition
        MARKERS.save_epub = lambda *args, **kwargs: self.fail(
            "legacy EPUB transport must not be called"
        )
        MARKERS.save_fb2 = lambda *args, **kwargs: self.fail(
            "legacy FB2 transport must not be called"
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def path_for(self, value, title, file_format):
        return Path(MARKERS.local_paths(value["author"], title)[file_format])

    def touch(self, path, content=b"existing"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def test_a_regular_book_keeps_plain_title(self):
        value = book(duplicate_count=1, duplicate_preferred=False)
        self.assertEqual(MARKERS.duplicate_storage_title(value), "Book")
        self.assertEqual(MARKERS.duplicate_storage_title_candidates(value), ("Book",))

    def test_b_preferred_duplicate_keeps_plain_title(self):
        value = book(duplicate_preferred=True)
        self.assertEqual(MARKERS.duplicate_storage_title(value), "Book")
        self.assertEqual(MARKERS.duplicate_storage_title_candidates(value), ("Book",))

    def test_c_non_preferred_duplicate_uses_neutral_marker(self):
        title = MARKERS.duplicate_storage_title(book())
        self.assertRegex(title, r"^Book \[opds-[0-9a-f]{24}\]$")

    def test_d_primary_title_does_not_use_legacy_marker(self):
        self.assertNotIn("flibusta-", MARKERS.duplicate_storage_title(book()))

    def test_e_marker_is_deterministic(self):
        first = MARKERS.download_filename_identity_marker("source-a", "item-1")
        second = MARKERS.download_filename_identity_marker("source-a", "item-1")
        self.assertEqual(first, second)

    def test_f_source_id_is_part_of_marker_identity(self):
        first = MARKERS.download_filename_identity_marker("source-a", "123")
        second = MARKERS.download_filename_identity_marker("source-b", "123")
        self.assertNotEqual(first, second)

    def test_g_source_item_id_is_part_of_marker_identity(self):
        first = MARKERS.download_filename_identity_marker("source-a", "123")
        second = MARKERS.download_filename_identity_marker("source-a", "456")
        self.assertNotEqual(first, second)

    def test_h_opaque_identifiers_produce_filesystem_safe_markers(self):
        identifiers = (
            "urn:uuid:123",
            "https://example.org/book/10?id=2",
            "book?id=10&edition=2",
            r"a/b\c:d*e?f",
        )
        for identifier in identifiers:
            with self.subTest(identifier=identifier):
                marker = MARKERS.download_filename_identity_marker(
                    "source-a",
                    identifier,
                )
                self.assertRegex(marker, r"^\[opds-[0-9a-f]{24}\]$")
                self.assertIsNone(re.search(r'[<>:"/\\|?*]', marker))

    def test_i_legacy_helper_reproduces_old_basename(self):
        self.assertEqual(
            MARKERS.legacy_duplicate_storage_title(book(source_item_id="123")),
            "Book [flibusta-123]",
        )

    def test_j_candidates_are_neutral_then_legacy(self):
        value = book()
        candidates = MARKERS.duplicate_storage_title_candidates(value)
        self.assertEqual(candidates[0], MARKERS.duplicate_storage_title(value))
        self.assertEqual(candidates[1], "Book [flibusta-123]")
        self.assertEqual(len(candidates), len(set(candidates)))

    def test_k_legacy_epub_is_detected(self):
        value = book()
        legacy = MARKERS.legacy_duplicate_storage_title(value)
        self.touch(self.path_for(value, legacy, "epub"))
        MARKERS.apply_duplicate_local_status(value)
        self.assertTrue(value["duplicate_exists_epub"])
        self.assertTrue(value["duplicate_exists_any"])

    def test_l_legacy_fb2_is_detected(self):
        value = book()
        legacy = MARKERS.legacy_duplicate_storage_title(value)
        self.touch(self.path_for(value, legacy, "fb2"))
        MARKERS.apply_duplicate_local_status(value)
        self.assertTrue(value["duplicate_exists_fb2"])
        self.assertTrue(value["duplicate_exists_any"])

    def test_m_neutral_epub_is_detected(self):
        value = book()
        primary = MARKERS.duplicate_storage_title(value)
        self.touch(self.path_for(value, primary, "epub"))
        MARKERS.apply_duplicate_local_status(value)
        self.assertTrue(value["duplicate_exists_epub"])

    def test_n_neutral_fb2_is_detected(self):
        value = book()
        primary = MARKERS.duplicate_storage_title(value)
        self.touch(self.path_for(value, primary, "fb2"))
        MARKERS.apply_duplicate_local_status(value)
        self.assertTrue(value["duplicate_exists_fb2"])

    def test_o_legacy_file_skips_network_and_neutral_write(self):
        value = book()
        legacy = MARKERS.legacy_duplicate_storage_title(value)
        primary = MARKERS.duplicate_storage_title(value)
        legacy_path = self.path_for(value, legacy, "epub")
        primary_path = self.path_for(value, primary, "epub")
        self.touch(legacy_path)

        result = MARKERS.download_one_book(
            value,
            "epub",
            duplicate_mode=True,
        )

        self.assertEqual(result, ("skipped", "Уже существует", 0.0))
        self.assertEqual(self.network_calls, [])
        self.assertFalse(primary_path.exists())

    def test_p_new_duplicate_download_writes_neutral_destination(self):
        value = book()
        primary = MARKERS.duplicate_storage_title(value)
        destination = self.path_for(value, primary, "epub")

        result = MARKERS.download_one_book(
            value,
            "epub",
            duplicate_mode=True,
        )

        self.assertEqual(result, ("downloaded", str(destination), 1.0))
        self.assertTrue(destination.is_file())
        self.assertIn("[opds-", destination.name)
        self.assertNotIn("flibusta-", destination.name)

    def test_q_legacy_file_is_not_renamed_copied_or_moved(self):
        value = book()
        legacy = MARKERS.legacy_duplicate_storage_title(value)
        primary = MARKERS.duplicate_storage_title(value)
        legacy_path = self.path_for(value, legacy, "fb2")
        primary_path = self.path_for(value, primary, "fb2")
        self.touch(legacy_path, b"legacy-content")

        MARKERS.download_one_book(value, "fb2", duplicate_mode=True)

        self.assertEqual(legacy_path.read_bytes(), b"legacy-content")
        self.assertFalse(primary_path.exists())

    def test_r_long_title_truncation_is_deterministic(self):
        value = book(title="Очень длинное название " * 40)
        title = MARKERS.duplicate_storage_title(value)
        first = self.path_for(value, title, "epub")
        second = self.path_for(value, title, "epub")
        self.assertEqual(first, second)
        self.assertLessEqual(len(first.stem.encode("utf-8")), 220)
        self.assertRegex(first.stem, r"~[0-9a-f]{10}$")


if __name__ == "__main__":
    unittest.main()
