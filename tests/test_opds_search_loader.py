import ast
import dataclasses
import hashlib
import ipaddress
import re
import sys
import types
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit, urlunsplit


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = PROJECT_ROOT / "tests" / "fixtures" / "opds" / "search_results_page_1.xml"


def load_search_loader_module():
    """Загружает только neutral search execution и существующий OPDS pipeline."""
    source = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    wanted = {
        "normalize_opds_url",
        "resolve_opds_url",
        "is_safe_http_url",
        "AcquisitionLink",
        "BookRecord",
        "CatalogRef",
        "OPDSSearchRef",
        "OPDSSearchDescriptor",
        "OPDSFeed",
        "OPDSCatalogPage",
        "OPDS1Provider",
        "_atom_search_mime_priority",
        "resolve_opensearch_template",
        "normalize_opds_search_query",
        "expand_opds_search_template",
        "_catalog_acquisition_format",
        "book_record_to_catalog_book",
        "load_opds_catalog_page",
        "load_opds_search_page",
    }
    body = []
    for node in tree.body:
        if getattr(node, "name", None) in wanted:
            body.append(node)
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name)
            and (
                target.id.startswith("OPDS1_")
                or target.id.startswith("OPENSEARCH_")
            )
            for target in node.targets
        ):
            body.append(node)
    module = types.ModuleType("isolated_opds_search_loader_test")
    sys.modules[module.__name__] = module
    module.__dict__.update(
        dataclass=dataclasses.dataclass,
        hashlib=hashlib,
        ipaddress=ipaddress,
        ET=ET,
        quote=quote,
        urljoin=urljoin,
        urlsplit=urlsplit,
        urlunsplit=urlunsplit,
    )
    exec(compile(ast.Module(body=body, type_ignores=[]), "app.py", "exec"), module.__dict__)
    module.__source_text__ = source
    module.__source_tree__ = tree
    return module


SEARCH_MODULE = load_search_loader_module()
FIXTURE_CONTENT = FIXTURE_PATH.read_bytes()


class FakeClient:
    def __init__(self, final_url="https://reader.example.test/find/results.xml"):
        self.final_url = final_url
        self.calls = []

    def fetch(self, url):
        self.calls.append(url)
        return types.SimpleNamespace(
            requested_url=url,
            final_url=self.final_url,
            content=FIXTURE_CONTENT,
        )


def search_descriptor(
    template="https://reader.example.test/find?term={searchTerms}",
    page_offset=1,
):
    return SEARCH_MODULE.OPDSSearchDescriptor(
        template=template,
        mime_type="application/atom+xml;profile=opds-catalog",
        page_offset=page_offset,
    )


class OPDSSearchLoaderTests(unittest.TestCase):
    def test_a_empty_or_whitespace_query_is_rejected(self):
        for query in ("", " ", "\t\r\n"):
            with self.subTest(query=query), self.assertRaisesRegex(
                ValueError,
                "Поисковый запрос не указан",
            ):
                SEARCH_MODULE.normalize_opds_search_query(query)

    def test_b_non_string_query_is_rejected(self):
        for query in (None, 123, []):
            with self.subTest(query=query), self.assertRaises(ValueError):
                SEARCH_MODULE.normalize_opds_search_query(query)

    def test_c_unicode_query_is_trimmed_without_aggressive_normalization(self):
        self.assertEqual(
            SEARCH_MODULE.normalize_opds_search_query("  Дюна  Герберт  "),
            "Дюна  Герберт",
        )

    def test_d_search_terms_is_replaced_with_utf8_percent_encoding(self):
        expanded = SEARCH_MODULE.expand_opds_search_template(
            search_descriptor(),
            "Дюна Герберт",
        )
        encoded = quote("Дюна Герберт", safe="")
        self.assertEqual(
            expanded,
            f"https://reader.example.test/find?term={encoded}",
        )
        self.assertNotIn("{searchTerms}", expanded)
        self.assertNotIn("Дюна Герберт", expanded)
        self.assertNotIn(" ", expanded)

    def test_e_parameter_name_comes_only_from_descriptor(self):
        descriptor = search_descriptor(
            "https://reader.example.test/find?catalog-key={searchTerms}"
        )
        expanded = SEARCH_MODULE.expand_opds_search_template(descriptor, "ocean")
        self.assertEqual(
            expanded,
            "https://reader.example.test/find?catalog-key=ocean",
        )
        function_source = self._new_function_source()
        self.assertNotIn('"q=', function_source)
        self.assertNotIn("'q=", function_source)
        self.assertNotIn("term=", function_source)
        self.assertNotIn("cursor=", function_source)

    def test_f_remaining_placeholder_is_rejected(self):
        descriptor = search_descriptor(
            "https://reader.example.test/find?term={searchTerms}&cursor={startIndex}"
        )
        with self.assertRaisesRegex(ValueError, "неподдерживаемые placeholders"):
            SEARCH_MODULE.expand_opds_search_template(descriptor, "ocean")

    def test_g_expanded_url_uses_existing_http_safety_validation(self):
        for template in (
            "file:///find?term={searchTerms}",
            "ftp://reader.example.test/find?term={searchTerms}",
            "https://user:secret@reader.example.test/find?term={searchTerms}",
            "https://[2001:db8::1/find?term={searchTerms}",
            "https://reader.example.test/find results?term={searchTerms}",
        ):
            with self.subTest(template=template), self.assertRaises(ValueError):
                SEARCH_MODULE.expand_opds_search_template(
                    search_descriptor(template),
                    "ocean",
                )

    def test_h_loader_returns_existing_catalog_page_and_uses_fake_client(self):
        client = FakeClient()
        result = SEARCH_MODULE.load_opds_search_page(
            search_descriptor(),
            "ocean",
            source_id="source:alpha",
            client=client,
        )
        self.assertIsInstance(result, SEARCH_MODULE.OPDSCatalogPage)
        self.assertEqual(result.total_results, 57)
        self.assertEqual(client.calls, ["https://reader.example.test/find?term=ocean"])

    def test_i_results_use_existing_book_record_catalog_adapter(self):
        result = SEARCH_MODULE.load_opds_search_page(
            search_descriptor(),
            "ocean",
            client=FakeClient(),
        )
        self.assertEqual(len(result.books), 2)
        self.assertEqual(result.books[0]["title"], "The Northern Archive")
        self.assertTrue(result.books[0]["epub"])
        self.assertTrue(result.books[1]["fb2"])

    def test_j_opaque_ids_and_source_identity_are_preserved(self):
        result = SEARCH_MODULE.load_opds_search_page(
            search_descriptor(),
            "ocean",
            source_id="source:alpha",
            client=FakeClient(),
        )
        self.assertEqual(result.source_id, "source:alpha")
        self.assertEqual(
            [book["id"] for book in result.books],
            [
                "urn:uuid:c0119547-cffe-4207-857d-14d71c79f1e7",
                "tag:catalog.example.test,2026:amber-observatory",
            ],
        )
        self.assertTrue(all(book["source_id"] == "source:alpha" for book in result.books))

    def test_k_real_next_url_is_returned_without_followup_request(self):
        client = FakeClient(
            final_url="https://reader.example.test/find/results-page-1.xml"
        )
        result = SEARCH_MODULE.load_opds_search_page(
            search_descriptor(),
            "ocean",
            client=client,
        )
        self.assertEqual(
            result.next_url,
            "https://reader.example.test/find/results-page-1.xml?cursor=next",
        )
        self.assertEqual(len(client.calls), 1)

    def test_l_loader_delegates_to_existing_catalog_loader(self):
        source = self._new_function_source()
        loader_source = self._function_source("load_opds_search_page")
        self.assertIn("load_opds_catalog_page(", loader_source)
        self.assertNotIn("OPDS1Provider(", loader_source)
        self.assertNotIn("book_record_to_catalog_book(", loader_source)
        self.assertNotIn("requests.", source)

    def test_m_new_execution_layer_has_no_legacy_source_coupling(self):
        source = self._new_function_source()
        fixture_source = FIXTURE_PATH.read_text(encoding="utf-8")
        test_source = Path(__file__).read_text(encoding="utf-8")
        corpus = "\n".join((source, fixture_source, test_source)).lower()
        corpus = corpus.replace("{searchterms}", "")
        forbidden = (
            "opds" + "_base",
            "flibu" + "sta",
            "/opds/" + "search",
            "search" + "type",
            "search" + "term",
            "page" + "number",
        )
        for marker in forbidden:
            with self.subTest(marker=marker):
                self.assertNotIn(marker, corpus)

    def test_n_optional_start_page_uses_default_page_offset(self):
        expanded = SEARCH_MODULE.expand_opds_search_template(
            search_descriptor(
                "https://reader.example.test/find?q={searchTerms}&page={startPage?}"
            ),
            "ocean",
        )
        self.assertEqual(
            expanded,
            "https://reader.example.test/find?q=ocean&page=1",
        )

    def test_o_optional_start_page_supports_negative_offset(self):
        expanded = SEARCH_MODULE.expand_opds_search_template(
            search_descriptor(
                "https://reader.example.test/find?q={searchTerms}&page={startPage?}",
                page_offset=-2,
            ),
            "ocean",
        )
        self.assertEqual(
            expanded,
            "https://reader.example.test/find?q=ocean&page=-2",
        )

    def test_p_required_start_page_supports_negative_offset(self):
        expanded = SEARCH_MODULE.expand_opds_search_template(
            search_descriptor(
                "https://reader.example.test/find?q={searchTerms}&page={startPage}",
                page_offset=-2,
            ),
            "ocean",
        )
        self.assertEqual(
            expanded,
            "https://reader.example.test/find?q=ocean&page=-2",
        )

    def test_q_start_page_expansion_keeps_utf8_query_encoding(self):
        expanded = SEARCH_MODULE.expand_opds_search_template(
            search_descriptor(
                "https://reader.example.test/find?q={searchTerms}&page={startPage?}"
            ),
            "Дюна Герберт",
        )
        self.assertIn(f"q={quote('Дюна Герберт', safe='')}", expanded)
        self.assertTrue(expanded.endswith("&page=1"))

    def test_r_supported_placeholders_are_fully_expanded(self):
        expanded = SEARCH_MODULE.expand_opds_search_template(
            search_descriptor(
                "https://reader.example.test/find?q={searchTerms}"
                "&first={startPage}&optional={startPage?}"
            ),
            "ocean",
        )
        self.assertNotIn("{", expanded)
        self.assertNotIn("}", expanded)

    def test_s_other_optional_placeholder_remains_unsupported(self):
        descriptor = search_descriptor(
            "https://reader.example.test/find?q={searchTerms}&index={startIndex?}"
        )
        with self.assertRaisesRegex(ValueError, "неподдерживаемые placeholders"):
            SEARCH_MODULE.expand_opds_search_template(descriptor, "ocean")

    def _function_source(self, name):
        for node in SEARCH_MODULE.__source_tree__.body:
            if getattr(node, "name", None) == name:
                return ast.get_source_segment(SEARCH_MODULE.__source_text__, node) or ""
        self.fail(f"Function not found: {name}")

    def _new_function_source(self):
        return "\n".join(
            self._function_source(name)
            for name in (
                "normalize_opds_search_query",
                "expand_opds_search_template",
                "load_opds_search_page",
            )
        )


if __name__ == "__main__":
    unittest.main()
