import ast
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_transport_module():
    source = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    body = []
    for node in tree.body:
        if getattr(node, "name", None) == "download_one_book":
            body.append(node)
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name)
            and target.id == "LEGACY_QUEUE_SOURCE_ID"
            for target in node.targets
        ):
            body.append(node)

    module = types.ModuleType("isolated_download_one_book_transport_test")
    sys.modules[module.__name__] = module
    module.__dict__.update(os=os, time=time)
    exec(
        compile(ast.Module(body=body, type_ignores=[]), "app.py", "exec"),
        module.__dict__,
    )
    module.__source_text__ = source
    module.__source_tree__ = tree
    return module


TRANSPORT = load_transport_module()


def book(source_id=None, item_id="opaque-id"):
    result = {
        "id": item_id,
        "title": "Example",
        "author": "Writer",
    }
    if source_id is not None:
        result["source_id"] = source_id
    return result


class DownloadOneBookTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.calls = {"epub": [], "fb2": [], "opds": []}
        TRANSPORT.duplicate_storage_title = lambda value: value["title"]
        TRANSPORT.duplicate_storage_title_candidates = lambda value: (value["title"],)
        TRANSPORT.local_paths = lambda author, title: {
            "epub": str(self.root / f"{title}.epub"),
            "fb2": str(self.root / f"{title}.fb2"),
        }

        def save_epub(item_id, destination, progress=None):
            self.calls["epub"].append((item_id, destination, progress))
            Path(destination).write_bytes(b"legacy epub")
            return 1.25

        def save_fb2(item_id, destination, progress=None):
            self.calls["fb2"].append((item_id, destination, progress))
            Path(destination).write_bytes(b"legacy fb2")
            return 2.5

        def save_opds_acquisition(
            url,
            destination,
            file_format,
            mime_type="",
            progress=None,
        ):
            self.calls["opds"].append(
                (url, destination, file_format, mime_type, progress)
            )
            Path(destination).write_bytes(b"neutral acquisition")
            return 3.75

        TRANSPORT.save_epub = save_epub
        TRANSPORT.save_fb2 = save_fb2
        TRANSPORT.save_opds_acquisition = save_opds_acquisition

    def tearDown(self):
        self.temp_dir.cleanup()

    def destination(self, file_format):
        return self.root / f"Example.{file_format}"

    def test_a_legacy_epub_uses_id_transport_and_publishes_atomically(self):
        progress = object()
        result = TRANSPORT.download_one_book(book(item_id="123"), "epub", progress=progress)
        destination = self.destination("epub")
        self.assertEqual(result, ("downloaded", str(destination), 1.25))
        self.assertEqual(
            self.calls["epub"],
            [("123", str(destination) + ".part", progress)],
        )
        self.assertEqual(self.calls["opds"], [])
        self.assertTrue(destination.is_file())
        self.assertFalse(Path(str(destination) + ".part").exists())

    def test_b_legacy_fb2_uses_id_transport(self):
        progress = object()
        result = TRANSPORT.download_one_book(book(item_id="456"), "fb2", progress=progress)
        destination = self.destination("fb2")
        self.assertEqual(result, ("downloaded", str(destination), 2.5))
        self.assertEqual(
            self.calls["fb2"],
            [("456", str(destination) + ".part", progress)],
        )
        self.assertEqual(self.calls["opds"], [])
        self.assertTrue(destination.is_file())

    def test_c_neutral_epub_uses_acquisition_url_mime_and_progress(self):
        progress = object()
        value = book("source-a", "urn:uuid:opaque-book")
        value.update(
            epub_url="https://files.example/book.epub",
            epub_mime_type="application/epub+zip",
        )
        result = TRANSPORT.download_one_book(value, "epub", progress=progress)
        destination = self.destination("epub")
        self.assertEqual(result, ("downloaded", str(destination), 3.75))
        self.assertEqual(
            self.calls["opds"],
            [(
                "https://files.example/book.epub",
                str(destination) + ".part",
                "epub",
                "application/epub+zip",
                progress,
            )],
        )
        self.assertEqual(self.calls["epub"], [])
        self.assertNotIn("urn:uuid:opaque-book", repr(self.calls["opds"]))
        self.assertTrue(destination.is_file())
        self.assertFalse(Path(str(destination) + ".part").exists())

    def test_d_neutral_fb2_uses_acquisition_url_mime_and_progress(self):
        progress = object()
        value = book("source-b", "tag:catalog.example,2026:item")
        value.update(
            fb2_url="https://files.example/book.fb2.zip",
            fb2_mime_type="application/zip",
        )
        result = TRANSPORT.download_one_book(value, "fb2", progress=progress)
        destination = self.destination("fb2")
        self.assertEqual(result, ("downloaded", str(destination), 3.75))
        self.assertEqual(
            self.calls["opds"],
            [(
                "https://files.example/book.fb2.zip",
                str(destination) + ".part",
                "fb2",
                "application/zip",
                progress,
            )],
        )
        self.assertEqual(self.calls["fb2"], [])
        self.assertNotIn("tag:catalog.example,2026:item", repr(self.calls["opds"]))
        self.assertTrue(destination.is_file())

    def test_e_neutral_missing_url_never_falls_back_to_legacy_transport(self):
        cases = (
            ("epub", "Для EPUB отсутствует acquisition URL"),
            ("fb2", "Для FB2 отсутствует acquisition URL"),
        )
        for file_format, message in cases:
            with self.subTest(file_format=file_format):
                with self.assertRaisesRegex(RuntimeError, message):
                    TRANSPORT.download_one_book(
                        book("source-a", "book?id=10&edition=2"),
                        file_format,
                    )
                destination = self.destination(file_format)
                self.assertFalse(destination.exists())
                self.assertFalse(Path(str(destination) + ".part").exists())
        self.assertEqual(self.calls, {"epub": [], "fb2": [], "opds": []})

    def test_f_explicit_legacy_source_ignores_url_like_fields(self):
        value = book(TRANSPORT.LEGACY_QUEUE_SOURCE_ID, "123")
        value["epub_url"] = "https://somewhere.example/book.epub"
        TRANSPORT.download_one_book(value, "epub")
        self.assertEqual(self.calls["epub"][0][0], "123")
        self.assertEqual(self.calls["opds"], [])

    def test_g_existing_local_file_skips_all_downloaders(self):
        destination = self.destination("epub")
        destination.write_bytes(b"existing")
        value = book("source-a", "opaque-id")
        value["epub_url"] = "https://files.example/book.epub"
        self.assertEqual(
            TRANSPORT.download_one_book(value, "epub"),
            ("skipped", "Уже существует", 0.0),
        )
        self.assertEqual(self.calls, {"epub": [], "fb2": [], "opds": []})

    def test_h_neutral_downloader_error_removes_outer_temporary(self):
        destination = self.destination("epub")

        def fail_after_write(url, temporary, file_format, mime_type="", progress=None):
            Path(temporary).write_bytes(b"partial")
            raise RuntimeError("download failed")

        TRANSPORT.save_opds_acquisition = fail_after_write
        value = book("source-a", "opaque-id")
        value["epub_url"] = "https://files.example/book.epub"
        with self.assertRaisesRegex(RuntimeError, "download failed"):
            TRANSPORT.download_one_book(value, "epub")
        self.assertFalse(destination.exists())
        self.assertFalse(Path(str(destination) + ".part").exists())
        self.assertEqual(self.calls["epub"], [])

    def test_i_legacy_downloader_error_still_removes_outer_temporary(self):
        destination = self.destination("fb2")

        def fail_after_write(item_id, temporary, progress=None):
            Path(temporary).write_bytes(b"partial")
            raise RuntimeError("legacy download failed")

        TRANSPORT.save_fb2 = fail_after_write
        with self.assertRaisesRegex(RuntimeError, "legacy download failed"):
            TRANSPORT.download_one_book(book(item_id="789"), "fb2")
        self.assertFalse(destination.exists())
        self.assertFalse(Path(str(destination) + ".part").exists())
        self.assertEqual(self.calls["opds"], [])

    def test_j_transport_selection_has_no_runtime_source_or_url_reconstruction(self):
        node = next(
            node
            for node in TRANSPORT.__source_tree__.body
            if getattr(node, "name", None) == "download_one_book"
        )
        source = ast.get_source_segment(TRANSPORT.__source_text__, node) or ""
        for forbidden in (
            "current_source_id",
            "current_source_config",
            "APP_CONFIG",
            "LEGACY_OPDS_BASE",
            "normalize_opds_url",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
