import ast
import dataclasses
import hashlib
import ipaddress
import sys
import time
import types
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = PROJECT_ROOT / "tests" / "fixtures" / "opds"


def load_http_module():
    """Загружает нейтральные OPDS-компоненты без импорта runtime приложения."""
    source = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    wanted = {
        "normalize_opds_url",
        "resolve_opds_url",
        "AcquisitionLink",
        "BookRecord",
        "CatalogRef",
        "OPDSFeed",
        "OPDS1Provider",
        "HTTPFetchResult",
        "SourceValidationResult",
        "OPDSHTTPClient",
        "validate_opds_source",
    }
    body = []
    for node in tree.body:
        if getattr(node, "name", None) in wanted:
            body.append(node)
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name)
            and (
                target.id.startswith("OPDS1_")
                or target.id == "OPENSEARCH_1_1"
            )
            for target in node.targets
        ):
            body.append(node)
    module = types.ModuleType("isolated_opds_http_test")
    sys.modules[module.__name__] = module
    module.__dict__.update(
        dataclass=dataclasses.dataclass,
        hashlib=hashlib,
        ipaddress=ipaddress,
        ET=ET,
        requests=requests,
        time=time,
        urljoin=urljoin,
        urlsplit=urlsplit,
        urlunsplit=urlunsplit,
    )
    exec(compile(ast.Module(body=body, type_ignores=[]), "app.py", "exec"), module.__dict__)
    module.__source_text__ = source
    module.__source_tree__ = tree
    return module


HTTP_MODULE = load_http_module()


class FakeResponse:
    def __init__(self, content=b"", url="https://example.org/opds", headers=None, status=200):
        self.url = url
        self.headers = headers or {}
        self.status_code = status
        self.chunks = [content]
        self.iterated = False
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size):
        self.iterated = True
        yield from self.chunks

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, response=None, error=None, responses=None):
        self.response = response
        self.error = error
        self.responses = None if responses is None else list(responses)
        self.calls = []
        self.returned_responses = []
        self.previous_responses_closed = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error is not None:
            raise self.error
        if self.responses is not None:
            self.previous_responses_closed.append(
                all(response.closed for response in self.returned_responses)
            )
            if not self.responses:
                raise AssertionError("Unexpected extra GET")
            response = self.responses.pop(0)
            if isinstance(response, BaseException):
                raise response
            self.returned_responses.append(response)
            return response
        return self.response


def fixture_bytes(name):
    return (FIXTURE_DIR / name).read_bytes()


class OPDSHTTPClientTests(unittest.TestCase):
    def test_a_successful_get_preserves_metadata_and_content(self):
        response = FakeResponse(
            content=b"<feed />",
            url="https://example.org/final.xml#fragment",
            headers={"Content-Type": "application/atom+xml; charset=utf-8"},
        )
        session = FakeSession(response=response)
        client = HTTP_MODULE.OPDSHTTPClient(session=session)
        result = client.fetch(" HTTPS://Example.ORG/opds#old ")
        self.assertEqual(result.requested_url, "https://example.org/opds")
        self.assertEqual(result.final_url, "https://example.org/final.xml")
        self.assertEqual(result.content, b"<feed />")
        self.assertEqual(result.content_type, "application/atom+xml; charset=utf-8")
        self.assertTrue(response.closed)

    def test_b_fetch_feed_resolves_links_from_redirect_target(self):
        response = FakeResponse(
            content=fixture_bytes("navigation.xml"),
            url="https://catalog.example.org/feeds/feed.xml",
        )
        client = HTTP_MODULE.OPDSHTTPClient(session=FakeSession(response=response))
        feed = client.fetch_feed("https://example.org/opds")
        self.assertIn(
            "https://catalog.example.org/feeds/publications_page_1.xml",
            {ref.url for ref in feed.navigation},
        )

    def test_c_oversized_content_length_is_rejected_before_reading(self):
        response = FakeResponse(
            content=b"small",
            headers={"Content-Length": "101"},
        )
        client = HTTP_MODULE.OPDSHTTPClient(
            session=FakeSession(response=response),
            max_response_bytes=100,
        )
        with self.assertRaisesRegex(ValueError, "превышает"):
            client.fetch("https://example.org/opds")
        self.assertFalse(response.iterated)
        self.assertTrue(response.closed)

    def test_d_streamed_content_is_limited_without_content_length(self):
        response = FakeResponse(headers={})
        response.chunks = [b"12345", b"678901"]
        client = HTTP_MODULE.OPDSHTTPClient(
            session=FakeSession(response=response),
            max_response_bytes=10,
        )
        with self.assertRaisesRegex(ValueError, "превышает"):
            client.fetch("https://example.org/opds")
        self.assertTrue(response.closed)

    def test_e_unsupported_redirect_target_is_rejected(self):
        response = FakeResponse(content=b"ignored", url="file:///tmp/feed.xml")
        client = HTTP_MODULE.OPDSHTTPClient(session=FakeSession(response=response))
        with self.assertRaises(ValueError):
            client.fetch("https://example.org/opds")
        self.assertTrue(response.closed)

    def test_f_http_errors_are_raised(self):
        with patch.object(HTTP_MODULE.time, "sleep"):
            for status in (404, 500):
                with self.subTest(status=status):
                    response_count = 4 if status == 500 else 1
                    responses = [
                        FakeResponse(status=status)
                        for _ in range(response_count)
                    ]
                    client = HTTP_MODULE.OPDSHTTPClient(
                        session=FakeSession(responses=responses),
                    )
                    with self.assertRaises(requests.HTTPError):
                        client.fetch("https://example.org/opds")
                    self.assertTrue(all(response.closed for response in responses))
                    validation_responses = [
                        FakeResponse(status=status)
                        for _ in range(response_count)
                    ]
                    validation_client = HTTP_MODULE.OPDSHTTPClient(
                        session=FakeSession(responses=validation_responses),
                    )
                    result = HTTP_MODULE.validate_opds_source(
                        "https://example.org/opds",
                        validation_client,
                    )
                    self.assertFalse(result.valid)
                    self.assertIn("HTTP", result.error)

    def test_g_timeout_returns_invalid_validation_result(self):
        client = HTTP_MODULE.OPDSHTTPClient(
            session=FakeSession(error=requests.Timeout("timeout")),
        )
        result = HTTP_MODULE.validate_opds_source("https://example.org/opds", client)
        self.assertFalse(result.valid)
        self.assertIn("время ожидания", result.error)

    def test_h_connection_error_returns_invalid_validation_result(self):
        client = HTTP_MODULE.OPDSHTTPClient(
            session=FakeSession(error=requests.ConnectionError("offline")),
        )
        with patch.object(HTTP_MODULE.time, "sleep"):
            result = HTTP_MODULE.validate_opds_source(
                "https://example.org/opds",
                client,
            )
        self.assertFalse(result.valid)
        self.assertIn("подключиться", result.error)

    def test_i_invalid_xml_returns_invalid_validation_result(self):
        response = FakeResponse(content=b"<feed>")
        client = HTTP_MODULE.OPDSHTTPClient(session=FakeSession(response=response))
        result = HTTP_MODULE.validate_opds_source("https://example.org/opds", client)
        self.assertFalse(result.valid)
        self.assertIn("XML", result.error)

    def test_j_non_atom_root_returns_invalid_validation_result(self):
        response = FakeResponse(content=b"<feed><title>Wrong</title></feed>")
        client = HTTP_MODULE.OPDSHTTPClient(session=FakeSession(response=response))
        result = HTTP_MODULE.validate_opds_source("https://example.org/opds", client)
        self.assertFalse(result.valid)
        self.assertIn("Atom feed", result.error)

    def test_k_navigation_fixture_validates_and_exposes_title(self):
        response = FakeResponse(
            content=fixture_bytes("navigation.xml"),
            url="https://catalog.example.org/navigation.xml",
        )
        client = HTTP_MODULE.OPDSHTTPClient(session=FakeSession(response=response))
        result = HTTP_MODULE.validate_opds_source("https://example.org/opds", client)
        self.assertTrue(result.valid)
        self.assertEqual(result.normalized_url, "https://example.org/opds")
        self.assertEqual(result.final_url, "https://catalog.example.org/navigation.xml")
        self.assertEqual(result.title, "Example OPDS navigation")
        self.assertEqual(result.error, "")

    def test_l_publications_fixture_returns_opds_feed(self):
        response = FakeResponse(
            content=fixture_bytes("publications_page_1.xml"),
            url="https://catalog.example.org/publications_page_1.xml",
        )
        client = HTTP_MODULE.OPDSHTTPClient(session=FakeSession(response=response))
        feed = client.fetch_feed("https://example.org/opds", source_id="source-test")
        self.assertIsInstance(feed, HTTP_MODULE.OPDSFeed)
        self.assertGreaterEqual(len(feed.publications), 2)
        self.assertTrue(all(book.source_id == "source-test" for book in feed.publications))

    def test_m_get_uses_required_options_and_default_tls_verification(self):
        response = FakeResponse(content=b"ok")
        session = FakeSession(response=response)
        client = HTTP_MODULE.OPDSHTTPClient(session=session, timeout=7)
        client.fetch("https://example.org/opds")
        self.assertEqual(len(session.calls), 1)
        requested_url, kwargs = session.calls[0]
        self.assertEqual(requested_url, "https://example.org/opds")
        self.assertEqual(kwargs["timeout"], 7)
        self.assertTrue(kwargs["stream"])
        self.assertTrue(kwargs["allow_redirects"])
        self.assertEqual(kwargs["headers"]["User-Agent"], "OPDS-Desktop-Client/1.0")
        self.assertNotIn("proxies", kwargs)
        self.assertNotIn("verify", kwargs)

    def test_n_client_source_has_no_source_specific_or_transport_overrides(self):
        wanted = {
            "HTTPFetchResult",
            "SourceValidationResult",
            "OPDSHTTPClient",
            "validate_opds_source",
        }
        executable_parts = []
        source_snippets = []
        for node in HTTP_MODULE.__source_tree__.body:
            if getattr(node, "name", None) in wanted:
                source_snippets.append(
                    ast.get_source_segment(HTTP_MODULE.__source_text__, node) or ""
                )
                for child in ast.walk(node):
                    if isinstance(child, ast.Name):
                        executable_parts.append(child.id)
                    elif isinstance(child, ast.Attribute):
                        executable_parts.append(child.attr)
                    elif isinstance(child, ast.Constant) and isinstance(child.value, str):
                        executable_parts.append(child.value)
        executable_text = "\n".join(executable_parts).lower()
        for marker in (
            "flibusta",
            "socks",
            "xray",
            "tor",
            "proxy",
            "proxies",
        ):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, executable_text)
        compact_source = "".join("\n".join(source_snippets).lower().split())
        self.assertNotIn("verify=false", compact_source)

    def test_o_internal_session_preserves_requests_default_trust_env(self):
        default_session = requests.Session()
        client = HTTP_MODULE.OPDSHTTPClient()
        try:
            self.assertIs(client.session.trust_env, default_session.trust_env)
        finally:
            client.session.close()
            default_session.close()

    def test_p_injected_session_is_preserved_without_changes(self):
        for trust_env in (False, True):
            with self.subTest(trust_env=trust_env):
                custom_session = FakeSession()
                custom_session.trust_env = trust_env
                client = HTTP_MODULE.OPDSHTTPClient(session=custom_session)
                self.assertIs(client.session, custom_session)
                self.assertIs(custom_session.trust_env, trust_env)

    def test_q_retryable_500_is_closed_before_second_get_succeeds(self):
        first = FakeResponse(status=500)
        second = FakeResponse(content=b"<feed />", status=200)
        session = FakeSession(responses=[first, second])
        client = HTTP_MODULE.OPDSHTTPClient(session=session)
        with patch.object(HTTP_MODULE.time, "sleep") as sleep:
            result = client.fetch("https://example.org/opds")
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(session.previous_responses_closed, [True, True])
        self.assertTrue(first.closed)
        self.assertTrue(second.closed)
        self.assertEqual(result.content, b"<feed />")
        sleep.assert_called_once_with(0.5)
        self.assertTrue(all(call[1]["timeout"] == 15 for call in session.calls))

    def test_r_retry_backoff_covers_three_transient_failures(self):
        responses = [
            FakeResponse(status=500),
            FakeResponse(status=502),
            FakeResponse(status=503),
            FakeResponse(content=b"ok", status=200),
        ]
        session = FakeSession(responses=responses)
        client = HTTP_MODULE.OPDSHTTPClient(session=session)
        with patch.object(HTTP_MODULE.time, "sleep") as sleep:
            result = client.fetch("https://example.org/opds")
        self.assertEqual(len(session.calls), 4)
        self.assertEqual(result.content, b"ok")
        self.assertEqual(
            [call.args[0] for call in sleep.call_args_list],
            [0.5, 1.0, 2.0],
        )
        self.assertTrue(all(response.closed for response in responses))

    def test_s_four_retryable_failures_raise_without_fifth_get(self):
        responses = [FakeResponse(status=500) for _ in range(4)]
        session = FakeSession(responses=responses)
        client = HTTP_MODULE.OPDSHTTPClient(session=session)
        with patch.object(HTTP_MODULE.time, "sleep") as sleep:
            with self.assertRaises(requests.HTTPError):
                client.fetch("https://example.org/opds")
        self.assertEqual(len(session.calls), 4)
        self.assertEqual(
            [call.args[0] for call in sleep.call_args_list],
            [0.5, 1.0, 2.0],
        )
        self.assertTrue(all(response.closed for response in responses))

    def test_t_404_is_not_retried_or_delayed(self):
        response = FakeResponse(status=404)
        session = FakeSession(responses=[response])
        client = HTTP_MODULE.OPDSHTTPClient(session=session)
        with patch.object(HTTP_MODULE.time, "sleep") as sleep:
            with self.assertRaises(requests.HTTPError):
                client.fetch("https://example.org/opds")
        self.assertEqual(len(session.calls), 1)
        sleep.assert_not_called()
        self.assertTrue(response.closed)

    def test_u_success_is_not_retried_or_delayed(self):
        response = FakeResponse(content=b"ok", status=200)
        session = FakeSession(responses=[response])
        client = HTTP_MODULE.OPDSHTTPClient(session=session)
        with patch.object(HTTP_MODULE.time, "sleep") as sleep:
            result = client.fetch("https://example.org/opds")
        self.assertEqual(len(session.calls), 1)
        sleep.assert_not_called()
        self.assertEqual(result.content, b"ok")
        self.assertTrue(response.closed)

    def test_v_read_timeout_is_retried_then_success_is_returned(self):
        response = FakeResponse(content=b"ok", status=200)
        session = FakeSession(
            responses=[requests.ReadTimeout("slow response"), response],
        )
        client = HTTP_MODULE.OPDSHTTPClient(session=session)
        with patch.object(HTTP_MODULE.time, "sleep") as sleep:
            result = client.fetch("https://example.org/opds")
        self.assertEqual(len(session.calls), 2)
        sleep.assert_called_once_with(0.5)
        self.assertEqual(result.content, b"ok")

    def test_w_connect_timeout_is_retried_then_success_is_returned(self):
        response = FakeResponse(content=b"ok", status=200)
        session = FakeSession(
            responses=[requests.ConnectTimeout("slow connect"), response],
        )
        client = HTTP_MODULE.OPDSHTTPClient(session=session)
        with patch.object(HTTP_MODULE.time, "sleep") as sleep:
            result = client.fetch("https://example.org/opds")
        self.assertEqual(len(session.calls), 2)
        sleep.assert_called_once_with(0.5)
        self.assertEqual(result.content, b"ok")

    def test_x_connection_error_is_retried_then_success_is_returned(self):
        response = FakeResponse(content=b"ok", status=200)
        session = FakeSession(
            responses=[requests.ConnectionError("connection reset"), response],
        )
        client = HTTP_MODULE.OPDSHTTPClient(session=session)
        with patch.object(HTTP_MODULE.time, "sleep") as sleep:
            result = client.fetch("https://example.org/opds")
        self.assertEqual(len(session.calls), 2)
        sleep.assert_called_once_with(0.5)
        self.assertEqual(result.content, b"ok")

    def test_y_three_read_timeouts_use_full_backoff_before_success(self):
        response = FakeResponse(content=b"ok", status=200)
        session = FakeSession(
            responses=[
                requests.ReadTimeout("first"),
                requests.ReadTimeout("second"),
                requests.ReadTimeout("third"),
                response,
            ],
        )
        client = HTTP_MODULE.OPDSHTTPClient(session=session)
        with patch.object(HTTP_MODULE.time, "sleep") as sleep:
            result = client.fetch("https://example.org/opds")
        self.assertEqual(len(session.calls), 4)
        self.assertEqual(
            [call.args[0] for call in sleep.call_args_list],
            [0.5, 1.0, 2.0],
        )
        self.assertEqual(result.content, b"ok")

    def test_z_four_read_timeouts_reraise_last_without_final_sleep(self):
        last_timeout = requests.ReadTimeout("fourth")
        session = FakeSession(
            responses=[
                requests.ReadTimeout("first"),
                requests.ReadTimeout("second"),
                requests.ReadTimeout("third"),
                last_timeout,
            ],
        )
        client = HTTP_MODULE.OPDSHTTPClient(session=session)
        with patch.object(HTTP_MODULE.time, "sleep") as sleep:
            with self.assertRaises(requests.ReadTimeout) as caught:
                client.fetch("https://example.org/opds")
        self.assertIs(caught.exception, last_timeout)
        self.assertEqual(len(session.calls), 4)
        self.assertEqual(
            [call.args[0] for call in sleep.call_args_list],
            [0.5, 1.0, 2.0],
        )

    def test_za_transport_and_http_retries_share_attempt_budget(self):
        http_500 = FakeResponse(status=500)
        http_502 = FakeResponse(status=502)
        success = FakeResponse(content=b"ok", status=200)
        session = FakeSession(
            responses=[
                requests.ReadTimeout("first"),
                http_500,
                http_502,
                success,
            ],
        )
        client = HTTP_MODULE.OPDSHTTPClient(session=session)
        with patch.object(HTTP_MODULE.time, "sleep") as sleep:
            result = client.fetch("https://example.org/opds")
        self.assertEqual(len(session.calls), 4)
        self.assertEqual(
            [call.args[0] for call in sleep.call_args_list],
            [0.5, 1.0, 2.0],
        )
        self.assertEqual(result.content, b"ok")
        self.assertTrue(http_500.closed)
        self.assertTrue(http_502.closed)
        self.assertTrue(success.closed)


if __name__ == "__main__":
    unittest.main()
