import ast
import io
import ipaddress
import os
import re
import sys
import tempfile
import time as real_time
import types
import unittest
import uuid
import zipfile as stdlib_zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import requests as real_requests


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FakeTime:
    def __init__(self):
        self.sleeps = []

    @staticmethod
    def monotonic():
        return real_time.monotonic()

    def sleep(self, delay):
        self.sleeps.append(delay)


def load_downloader_module():
    source = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    wanted = {
        "normalize_opds_url",
        "compact_error",
        "emit_download_progress",
        "DownloadValidationError",
        "download_error_info",
        "validate_epub_file",
        "validate_fb2_file",
        "save_opds_acquisition",
    }
    constants = {
        "DOWNLOAD_CONNECT_TIMEOUT",
        "DOWNLOAD_READ_TIMEOUT",
        "DOWNLOAD_RETRY_ATTEMPTS",
        "DOWNLOAD_RETRY_DELAY",
        "MAX_DOWNLOAD_SIZE",
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

    request_api = types.SimpleNamespace(
        Session=real_requests.Session,
        Timeout=real_requests.Timeout,
        ConnectionError=real_requests.ConnectionError,
        HTTPError=real_requests.HTTPError,
        RequestException=real_requests.RequestException,
    )
    zipfile_api = types.SimpleNamespace(
        BadZipFile=stdlib_zipfile.BadZipFile,
        ZipFile=stdlib_zipfile.ZipFile,
        is_zipfile=stdlib_zipfile.is_zipfile,
    )
    module = types.ModuleType("isolated_opds_acquisition_downloader_test")
    sys.modules[module.__name__] = module
    module.__dict__.update(
        ET=ET,
        ipaddress=ipaddress,
        os=os,
        re=re,
        requests=request_api,
        time=FakeTime(),
        urlsplit=urlsplit,
        urlunsplit=urlunsplit,
        zipfile=zipfile_api,
    )
    exec(
        compile(ast.Module(body=body, type_ignores=[]), "app.py", "exec"),
        module.__dict__,
    )
    module.__source_text__ = source
    module.__source_tree__ = tree
    return module


DOWNLOADER = load_downloader_module()


class FakeResponse:
    def __init__(
        self,
        body=b"",
        *,
        url="https://files.example.test/book",
        status_code=200,
        headers=None,
        chunks=None,
    ):
        self.body = body
        self.url = url
        self.status_code = status_code
        self.headers = (
            {"Content-Length": str(len(body))} if headers is None else headers
        )
        self.chunks = chunks
        self.close_calls = 0
        self.iter_content_calls = 0

    def raise_for_status(self):
        if self.status_code >= 400:
            raise real_requests.HTTPError(
                f"HTTP {self.status_code}",
                response=self,
            )

    def iter_content(self, chunk_size):
        self.iter_content_calls += 1
        chunks = self.chunks if self.chunks is not None else (self.body,)
        yield from chunks

    def close(self):
        self.close_calls += 1


class FakeSession:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []
        self.close_calls = 0
        self.trust_env = True

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def close(self):
        self.close_calls += 1


def valid_epub_bytes():
    output = io.BytesIO()
    with stdlib_zipfile.ZipFile(output, "w") as archive:
        archive.writestr("mimetype", b"application/epub+zip")
        archive.writestr(
            "META-INF/container.xml",
            b"""<?xml version="1.0"?>
            <container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
              <rootfiles><rootfile full-path="content.opf" /></rootfiles>
            </container>""",
        )
        archive.writestr(
            "content.opf",
            b"""<?xml version="1.0"?>
            <package xmlns="http://www.idpf.org/2007/opf" version="3.0"></package>""",
        )
    return output.getvalue()


def valid_fb2_bytes(extra=b""):
    return (
        b'<?xml version="1.0" encoding="utf-8"?>'
        b'<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0">'
        b"<description><title-info><book-title>Example</book-title>"
        b"</title-info></description><body><section><p>Text</p></section></body>"
        + extra
        + b"</FictionBook>"
    )


def fb2_zip_bytes(member_name="book.fb2", data=None, extras=None):
    output = io.BytesIO()
    with stdlib_zipfile.ZipFile(
        output,
        "w",
        compression=stdlib_zipfile.ZIP_DEFLATED,
    ) as archive:
        archive.writestr(member_name, data or valid_fb2_bytes())
        for name, content in extras or ():
            archive.writestr(name, content)
    return output.getvalue()


class OPDSAcquisitionDownloaderTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.original_max_size = DOWNLOADER.MAX_DOWNLOAD_SIZE
        self.original_session_factory = DOWNLOADER.requests.Session
        self.original_zipfile = DOWNLOADER.zipfile.ZipFile
        self.original_is_zipfile = DOWNLOADER.zipfile.is_zipfile
        DOWNLOADER.time.sleeps.clear()

    def tearDown(self):
        DOWNLOADER.MAX_DOWNLOAD_SIZE = self.original_max_size
        DOWNLOADER.requests.Session = self.original_session_factory
        DOWNLOADER.zipfile.ZipFile = self.original_zipfile
        DOWNLOADER.zipfile.is_zipfile = self.original_is_zipfile
        self.temp_dir.cleanup()

    def destination(self, suffix):
        return self.root / f"book.{suffix}"

    def test_a_initial_url_is_normalized_before_streaming_get(self):
        response = FakeResponse(valid_epub_bytes())
        session = FakeSession(response)
        destination = self.destination("epub")
        elapsed = DOWNLOADER.save_opds_acquisition(
            " HTTPS://Files.Example.Test:443/book.epub#fragment ",
            destination,
            "epub",
            session=session,
        )
        self.assertIsInstance(elapsed, float)
        self.assertEqual(session.calls[0][0], "https://files.example.test:443/book.epub")
        kwargs = session.calls[0][1]
        self.assertIs(kwargs["stream"], True)
        self.assertIs(kwargs["allow_redirects"], True)
        self.assertEqual(
            kwargs["timeout"],
            (DOWNLOADER.DOWNLOAD_CONNECT_TIMEOUT, DOWNLOADER.DOWNLOAD_READ_TIMEOUT),
        )
        self.assertEqual(kwargs["headers"]["User-Agent"], "OPDS-Desktop-Client/1.0")
        self.assertNotIn("verify", kwargs)
        self.assertEqual(response.close_calls, 1)

    def test_b_invalid_scheme_is_rejected_before_network(self):
        session = FakeSession(FakeResponse(valid_epub_bytes()))
        with self.assertRaises(ValueError):
            DOWNLOADER.save_opds_acquisition(
                "file:///tmp/book.epub",
                self.destination("epub"),
                "epub",
                session=session,
            )
        self.assertEqual(session.calls, [])

    def test_c_unsupported_format_is_rejected_before_network(self):
        session = FakeSession(FakeResponse(b"data"))
        with self.assertRaisesRegex(ValueError, "Неподдерживаемый формат"):
            DOWNLOADER.save_opds_acquisition(
                "https://files.example.test/book.pdf",
                self.destination("pdf"),
                "pdf",
                session=session,
            )
        self.assertEqual(session.calls, [])

    def test_d_invalid_final_redirect_url_closes_response_and_cleans_up(self):
        response = FakeResponse(valid_epub_bytes(), url="file:///tmp/book.epub")
        session = FakeSession(response)
        destination = self.destination("epub")
        destination.write_bytes(b"old destination")
        with self.assertRaises(ValueError):
            DOWNLOADER.save_opds_acquisition(
                "https://files.example.test/book.epub",
                destination,
                "epub",
                session=session,
            )
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(response.close_calls, 1)
        self.assertFalse(destination.exists())

    def test_e_http_404_is_not_retried_and_cleans_up(self):
        response = FakeResponse(status_code=404)
        session = FakeSession(response)
        destination = self.destination("epub")
        destination.write_bytes(b"old destination")
        with self.assertRaises(real_requests.HTTPError):
            DOWNLOADER.save_opds_acquisition(
                "https://files.example.test/missing.epub",
                destination,
                "epub",
                session=session,
            )
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(response.close_calls, 1)
        self.assertFalse(destination.exists())

    def test_f_default_session_preserves_system_environment_behavior(self):
        response = FakeResponse(valid_epub_bytes())
        created_session = FakeSession(response)
        factories = []

        def session_factory():
            factories.append(True)
            return created_session

        DOWNLOADER.requests.Session = session_factory
        DOWNLOADER.save_opds_acquisition(
            "https://files.example.test/book.epub",
            self.destination("epub"),
            "epub",
        )
        self.assertEqual(factories, [True])
        self.assertIs(created_session.trust_env, True)
        self.assertEqual(created_session.close_calls, 1)

    def test_g_content_length_over_limit_stops_before_body_write(self):
        DOWNLOADER.MAX_DOWNLOAD_SIZE = 10
        response = FakeResponse(b"ignored", headers={"Content-Length": "11"})
        session = FakeSession(response)
        destination = self.destination("epub")
        with self.assertRaisesRegex(RuntimeError, "превышает допустимый размер"):
            DOWNLOADER.save_opds_acquisition(
                "https://files.example.test/book.epub",
                destination,
                "epub",
                session=session,
            )
        self.assertEqual(response.iter_content_calls, 0)
        self.assertFalse(destination.exists())

    def test_h_actual_stream_size_over_limit_removes_partial_file(self):
        DOWNLOADER.MAX_DOWNLOAD_SIZE = 10
        response = FakeResponse(
            headers={"Content-Length": "not-an-integer"},
            chunks=(b"123456", b"78901"),
        )
        session = FakeSession(response)
        destination = self.destination("epub")
        with self.assertRaisesRegex(RuntimeError, "превышает допустимый размер"):
            DOWNLOADER.save_opds_acquisition(
                "https://files.example.test/book.epub",
                destination,
                "epub",
                session=session,
            )
        self.assertFalse(destination.exists())

    def test_i_valid_epub_is_streamed_and_structurally_validated(self):
        body = valid_epub_bytes()
        destination = self.destination("epub")
        response = FakeResponse(chunks=(body[:31], b"", body[31:]))
        elapsed = DOWNLOADER.save_opds_acquisition(
            "https://files.example.test/book.epub",
            destination,
            "epub",
            mime_type="application/octet-stream",
            session=FakeSession(response),
        )
        self.assertIsInstance(elapsed, float)
        self.assertEqual(destination.read_bytes(), body)
        self.assertTrue(DOWNLOADER.validate_epub_file(destination))

    def test_j_invalid_epub_is_removed_after_validation_failure(self):
        response = FakeResponse(b"not an epub archive" * 10)
        session = FakeSession(response)
        destination = self.destination("epub")
        with self.assertRaises(DOWNLOADER.DownloadValidationError):
            DOWNLOADER.save_opds_acquisition(
                "https://files.example.test/book.epub",
                destination,
                "epub",
                session=session,
            )
        self.assertEqual(len(session.calls), DOWNLOADER.DOWNLOAD_RETRY_ATTEMPTS)
        self.assertFalse(destination.exists())

    def test_k_direct_fb2_accepts_declared_and_generic_mime_types(self):
        body = valid_fb2_bytes()
        for index, mime_type in enumerate(
            ("application/fb2+xml", "application/octet-stream")
        ):
            with self.subTest(mime_type=mime_type):
                destination = self.root / f"direct-{index}.fb2"
                DOWNLOADER.save_opds_acquisition(
                    "https://files.example.test/book.fb2",
                    destination,
                    "fb2",
                    mime_type=mime_type,
                    session=FakeSession(FakeResponse(body)),
                )
                self.assertEqual(destination.read_bytes(), body)
                self.assertTrue(DOWNLOADER.validate_fb2_file(destination))

    def test_l_fb2_zip_is_detected_by_content_and_only_member_is_written(self):
        body = valid_fb2_bytes()
        archive_body = fb2_zip_bytes(
            "BOOK.FB2",
            body,
            extras=(("ignored.txt", b"not a book"),),
        )
        destination = self.destination("fb2")
        DOWNLOADER.save_opds_acquisition(
            "https://files.example.test/archive",
            destination,
            "fb2",
            mime_type="application/octet-stream",
            session=FakeSession(FakeResponse(archive_body)),
        )
        self.assertEqual(destination.read_bytes(), body)
        self.assertFalse(stdlib_zipfile.is_zipfile(destination))
        self.assertFalse(Path(str(destination) + ".opds-download.part").exists())
        self.assertFalse((self.root / "ignored.txt").exists())

    def test_m_zip_member_path_is_never_used_as_filesystem_destination(self):
        escaped_name = f"escape-{uuid.uuid4().hex}.fb2"
        body = valid_fb2_bytes()
        archive_body = fb2_zip_bytes(f"../../{escaped_name}", body)
        destination = self.destination("fb2")
        escaped_path = self.root.parent / escaped_name
        DOWNLOADER.save_opds_acquisition(
            "https://files.example.test/archive.zip",
            destination,
            "fb2",
            session=FakeSession(FakeResponse(archive_body)),
        )
        self.assertEqual(destination.read_bytes(), body)
        self.assertFalse(escaped_path.exists())

    def test_n_zip_without_fb2_is_rejected_and_cleaned_up(self):
        archive_body = fb2_zip_bytes("readme.txt", b"no fb2 here")
        session = FakeSession(FakeResponse(archive_body))
        destination = self.destination("fb2")
        with self.assertRaisesRegex(
            DOWNLOADER.DownloadValidationError,
            "не найден FB2",
        ):
            DOWNLOADER.save_opds_acquisition(
                "https://files.example.test/archive.zip",
                destination,
                "fb2",
                session=session,
            )
        self.assertFalse(destination.exists())
        self.assertFalse(Path(str(destination) + ".opds-download.part").exists())

    def test_o_declared_uncompressed_zip_size_over_limit_is_rejected(self):
        body = valid_fb2_bytes(extra=b"x" * 5000)
        archive_body = fb2_zip_bytes(data=body)
        self.assertLess(len(archive_body), len(body))
        DOWNLOADER.MAX_DOWNLOAD_SIZE = (len(archive_body) + len(body)) // 2
        destination = self.destination("fb2")
        with self.assertRaisesRegex(RuntimeError, "распакованный размер"):
            DOWNLOADER.save_opds_acquisition(
                "https://files.example.test/archive.zip",
                destination,
                "fb2",
                session=FakeSession(FakeResponse(archive_body)),
            )
        self.assertFalse(destination.exists())

    def test_p_actual_extracted_size_is_limited_even_if_metadata_underreports(self):
        DOWNLOADER.MAX_DOWNLOAD_SIZE = 10

        class FakeInfo:
            filename = "book.fb2"
            file_size = 0

            @staticmethod
            def is_dir():
                return False

        class FakeArchive:
            def __init__(self, path):
                self.path = path

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            @staticmethod
            def infolist():
                return [FakeInfo()]

            @staticmethod
            def open(member):
                return io.BytesIO(b"x" * 11)

        DOWNLOADER.zipfile.is_zipfile = lambda path: True
        DOWNLOADER.zipfile.ZipFile = FakeArchive
        destination = self.destination("fb2")
        with self.assertRaisesRegex(RuntimeError, "распакованный размер"):
            DOWNLOADER.save_opds_acquisition(
                "https://files.example.test/archive.zip",
                destination,
                "fb2",
                session=FakeSession(FakeResponse(b"PK")),
            )
        self.assertFalse(destination.exists())

    def test_q_connection_error_retries_once_then_succeeds(self):
        response = FakeResponse(valid_epub_bytes())
        session = FakeSession(real_requests.ConnectionError("offline"), response)
        events = []
        elapsed = DOWNLOADER.save_opds_acquisition(
            "https://files.example.test/book.epub",
            self.destination("epub"),
            "epub",
            progress=events.append,
            session=session,
        )
        self.assertIsInstance(elapsed, float)
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(DOWNLOADER.time.sleeps, [DOWNLOADER.DOWNLOAD_RETRY_DELAY])
        self.assertIn("retry_wait", [event["stage"] for event in events])
        self.assertEqual(events[-1]["stage"], "success")
        for event in events:
            self.assertNotIn("Flibusta", event["detail"])
            self.assertNotIn("Флибуста", event["detail"])

    def test_r_helper_source_is_provider_neutral_and_streaming_safe(self):
        node = next(
            node
            for node in DOWNLOADER.__source_tree__.body
            if getattr(node, "name", None) == "save_opds_acquisition"
        )
        source = ast.get_source_segment(DOWNLOADER.__source_text__, node) or ""
        for forbidden in (
            "LEGACY_OPDS_BASE",
            "legacy_opds_get",
            "allowed_legacy_opds_url",
            "trust_env",
            "verify=False",
            "ZipFile.extract",
            "download_one_book",
            "queue_add",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
