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
from urllib.parse import urljoin, urlsplit, urlunsplit


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = PROJECT_ROOT / "tests" / "fixtures" / "opds" / "opensearch_description.xml"
START_PAGE_FIXTURE_PATH = (
    PROJECT_ROOT / "tests" / "fixtures" / "opds" / "opensearch_start_page.xml"
)


def load_descriptor_module():
    """Загружает только нейтральный URL-слой и OpenSearch resolver."""
    source = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    wanted = {
        "normalize_opds_url",
        "resolve_opds_url",
        "is_safe_http_url",
        "OPDSSearchRef",
        "OPDSSearchDescriptor",
        "_atom_search_mime_priority",
        "resolve_opensearch_template",
        "parse_opensearch_description",
        "resolve_direct_atom_search",
        "resolve_opds_search_descriptor",
    }
    body = []
    for node in tree.body:
        if getattr(node, "name", None) in wanted:
            body.append(node)
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id.startswith("OPENSEARCH_")
            for target in node.targets
        ):
            body.append(node)
    module = types.ModuleType("isolated_opensearch_descriptor_test")
    sys.modules[module.__name__] = module
    module.__dict__.update(
        dataclass=dataclasses.dataclass,
        hashlib=hashlib,
        ipaddress=ipaddress,
        ET=ET,
        urljoin=urljoin,
        urlsplit=urlsplit,
        urlunsplit=urlunsplit,
    )
    exec(compile(ast.Module(body=body, type_ignores=[]), "app.py", "exec"), module.__dict__)
    module.__source_text__ = source
    module.__source_tree__ = tree
    return module


DESCRIPTOR_MODULE = load_descriptor_module()
FIXTURE_CONTENT = FIXTURE_PATH.read_bytes()
START_PAGE_FIXTURE_CONTENT = START_PAGE_FIXTURE_PATH.read_bytes()


def descriptor_xml(template, mime_type="application/atom+xml", page_offset=None):
    page_offset_attribute = (
        f' pageOffset="{page_offset}"' if page_offset is not None else ""
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
    <OpenSearchDescription xmlns="http://a9.com/-/spec/opensearch/1.1/">
      <ShortName>Synthetic</ShortName>
      <Url type="{mime_type}" template="{template}"{page_offset_attribute} />
    </OpenSearchDescription>"""


class FakeClient:
    def __init__(self, content, final_url):
        self.content = content
        self.final_url = final_url
        self.calls = []

    def fetch(self, url):
        self.calls.append(url)
        return types.SimpleNamespace(content=self.content, final_url=self.final_url)


class OpenSearchDescriptorTests(unittest.TestCase):
    def test_a_opensearch_1_1_descriptor_is_parsed(self):
        descriptor = DESCRIPTOR_MODULE.parse_opensearch_description(
            FIXTURE_CONTENT,
            "https://reader.example.test/metadata/description.xml",
        )
        self.assertIsInstance(descriptor, DESCRIPTOR_MODULE.OPDSSearchDescriptor)

    def test_b_opds_profile_has_priority_over_plain_atom(self):
        descriptor = DESCRIPTOR_MODULE.parse_opensearch_description(
            FIXTURE_CONTENT,
            "https://reader.example.test/metadata/description.xml",
        )
        self.assertEqual(
            descriptor.template,
            "https://reader.example.test/feeds/search.atom?q={searchTerms}",
        )

    def test_c_original_mime_type_and_parameters_are_preserved(self):
        descriptor = DESCRIPTOR_MODULE.parse_opensearch_description(
            FIXTURE_CONTENT,
            "https://reader.example.test/metadata/description.xml",
        )
        self.assertEqual(
            descriptor.mime_type,
            "Application/Atom+XML; PROFILE=OPDS-Catalog",
        )

    def test_d_relative_template_uses_final_descriptor_url(self):
        descriptor = DESCRIPTOR_MODULE.parse_opensearch_description(
            FIXTURE_CONTENT,
            "https://redirected.example.test/final/metadata/description.xml",
        )
        self.assertEqual(
            descriptor.template,
            "https://redirected.example.test/final/feeds/search.atom?q={searchTerms}",
        )

    def test_e_absolute_template_is_preserved(self):
        descriptor = DESCRIPTOR_MODULE.parse_opensearch_description(
            descriptor_xml("https://search.example.test/catalog/find?q={searchTerms}"),
            "https://reader.example.test/metadata/description.xml",
        )
        self.assertEqual(
            descriptor.template,
            "https://search.example.test/catalog/find?q={searchTerms}",
        )

    def test_f_search_terms_placeholder_is_not_expanded(self):
        descriptor = DESCRIPTOR_MODULE.parse_opensearch_description(
            FIXTURE_CONTENT,
            "https://reader.example.test/metadata/description.xml",
        )
        self.assertIn("{searchTerms}", descriptor.template)

    def test_g_missing_search_terms_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "не содержит поддерживаемого"):
            DESCRIPTOR_MODULE.parse_opensearch_description(
                descriptor_xml("https://search.example.test/catalog/find?q=static"),
                "https://reader.example.test/metadata/description.xml",
            )

    def test_h_unsupported_result_mime_is_rejected(self):
        for mime_type in (
            "application/json",
            "application/atom+xml;profile=unknown",
        ):
            with self.subTest(mime_type=mime_type), self.assertRaisesRegex(
                ValueError,
                "не содержит поддерживаемого",
            ):
                DESCRIPTOR_MODULE.parse_opensearch_description(
                    descriptor_xml(
                        "https://search.example.test/catalog/find?q={searchTerms}",
                        mime_type,
                    ),
                    "https://reader.example.test/metadata/description.xml",
                )

    def test_i_non_http_templates_are_rejected(self):
        for template in (
            "file:///catalog/find?q={searchTerms}",
            "ftp://search.example.test/find?q={searchTerms}",
        ):
            with self.subTest(template=template), self.assertRaises(ValueError):
                DESCRIPTOR_MODULE.parse_opensearch_description(
                    descriptor_xml(template),
                    "https://reader.example.test/metadata/description.xml",
                )

    def test_j_credentials_and_malformed_templates_are_rejected(self):
        for template in (
            "https://user:secret@search.example.test/find?q={searchTerms}",
            "https://[2001:db8::1/find?q={searchTerms}",
        ):
            with self.subTest(template=template), self.assertRaises(ValueError):
                DESCRIPTOR_MODULE.parse_opensearch_description(
                    descriptor_xml(template),
                    "https://reader.example.test/metadata/description.xml",
                )

    def test_k_malformed_xml_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Некорректный OpenSearch XML"):
            DESCRIPTOR_MODULE.parse_opensearch_description(
                b"<OpenSearchDescription>",
                "https://reader.example.test/metadata/description.xml",
            )

    def test_l_direct_atom_reference_does_not_call_http_client(self):
        class FailingClient:
            def fetch(self, url):
                raise AssertionError(f"Unexpected HTTP call: {url}")

        search_ref = DESCRIPTOR_MODULE.OPDSSearchRef(
            url="https://reader.example.test/find?q={searchTerms}",
            mime_type="application/atom+xml;profile=opds-catalog",
        )
        descriptor = DESCRIPTOR_MODULE.resolve_opds_search_descriptor(
            search_ref,
            client=FailingClient(),
        )
        self.assertEqual(descriptor.template, search_ref.url)
        self.assertEqual(descriptor.mime_type, search_ref.mime_type)
        self.assertEqual(descriptor.page_offset, 1)

    def test_m_opensearch_reference_uses_client_fetch(self):
        client = FakeClient(
            FIXTURE_CONTENT,
            "https://redirected.example.test/final/metadata/description.xml",
        )
        search_ref = DESCRIPTOR_MODULE.OPDSSearchRef(
            url="https://reader.example.test/metadata/description.xml",
            mime_type="application/opensearchdescription+xml",
        )
        descriptor = DESCRIPTOR_MODULE.resolve_opds_search_descriptor(
            search_ref,
            client=client,
        )
        self.assertEqual(client.calls, [search_ref.url])
        self.assertEqual(
            descriptor.template,
            "https://redirected.example.test/final/feeds/search.atom?q={searchTerms}",
        )

    def test_n_default_resolver_constructs_existing_http_client(self):
        client = FakeClient(
            FIXTURE_CONTENT,
            "https://redirected.example.test/final/metadata/description.xml",
        )
        original_client = DESCRIPTOR_MODULE.__dict__.get("OPDSHTTPClient")
        DESCRIPTOR_MODULE.OPDSHTTPClient = lambda: client
        try:
            search_ref = DESCRIPTOR_MODULE.OPDSSearchRef(
                url="https://reader.example.test/metadata/description.xml",
                mime_type="application/opensearchdescription+xml",
            )
            DESCRIPTOR_MODULE.resolve_opds_search_descriptor(search_ref)
        finally:
            if original_client is None:
                DESCRIPTOR_MODULE.__dict__.pop("OPDSHTTPClient", None)
            else:
                DESCRIPTOR_MODULE.OPDSHTTPClient = original_client
        self.assertEqual(client.calls, [search_ref.url])

    def test_o_resolver_does_not_call_requests_directly(self):
        function_names = {
            "resolve_opensearch_template",
            "parse_opensearch_description",
            "resolve_direct_atom_search",
            "resolve_opds_search_descriptor",
        }
        source = "\n".join(
            ast.get_source_segment(DESCRIPTOR_MODULE.__source_text__, node) or ""
            for node in DESCRIPTOR_MODULE.__source_tree__.body
            if getattr(node, "name", None) in function_names
        )
        self.assertNotIn("requests.", source)
        self.assertIn("http_client.fetch(search_ref.url)", source)

    def test_p_resolver_contains_no_query_expansion(self):
        descriptor = DESCRIPTOR_MODULE.parse_opensearch_description(
            FIXTURE_CONTENT,
            "https://reader.example.test/metadata/description.xml",
        )
        self.assertIn("{searchTerms}", descriptor.template)
        source = DESCRIPTOR_MODULE.__source_text__
        tree = DESCRIPTOR_MODULE.__source_tree__
        function_names = {
            "resolve_opensearch_template",
            "parse_opensearch_description",
            "resolve_direct_atom_search",
            "resolve_opds_search_descriptor",
        }
        resolver_source = "\n".join(
            ast.get_source_segment(source, node) or ""
            for node in tree.body
            if getattr(node, "name", None) in function_names
        )
        self.assertNotIn("quote_plus", resolver_source)
        self.assertNotIn("urlencode", resolver_source)

    def test_q_new_resolver_has_no_legacy_source_coupling(self):
        names = {
            "OPDSSearchDescriptor",
            "_atom_search_mime_priority",
            "resolve_opensearch_template",
            "parse_opensearch_description",
            "resolve_direct_atom_search",
            "resolve_opds_search_descriptor",
        }
        resolver_source = "\n".join(
            ast.get_source_segment(DESCRIPTOR_MODULE.__source_text__, node) or ""
            for node in DESCRIPTOR_MODULE.__source_tree__.body
            if getattr(node, "name", None) in names
        )
        test_source = Path(__file__).read_text(encoding="utf-8")
        corpus = "\n".join(
            (
                resolver_source,
                FIXTURE_PATH.read_text(encoding="utf-8"),
                START_PAGE_FIXTURE_PATH.read_text(encoding="utf-8"),
                test_source,
            )
        ).lower()
        corpus = corpus.replace("{searchterms}", "")
        forbidden_exact = (
            "opds" + "_base",
            "flibu" + "sta",
            "/opds/" + "search",
            "search" + "type",
            "search" + "term",
            "page" + "number",
        )
        forbidden_words = (
            "que" + "ue",
            "down" + "load",
            "pro" + "xy",
            "so" + "cks",
            "x" + "ray",
            "t" + "or",
        )
        for marker in forbidden_exact:
            with self.subTest(marker=marker):
                self.assertNotIn(marker, corpus)
        for marker in forbidden_words:
            with self.subTest(marker=marker):
                self.assertIsNone(re.search(rf"\b{re.escape(marker)}\b", corpus))

    def test_r_missing_page_offset_defaults_to_one(self):
        descriptor = DESCRIPTOR_MODULE.parse_opensearch_description(
            FIXTURE_CONTENT,
            "https://reader.example.test/metadata/description.xml",
        )
        self.assertEqual(descriptor.page_offset, 1)

    def test_s_negative_page_offset_is_preserved(self):
        descriptor = DESCRIPTOR_MODULE.parse_opensearch_description(
            descriptor_xml(
                "https://reader.example.test/find?q={searchTerms}",
                page_offset="-2",
            ),
            "https://reader.example.test/metadata/description.xml",
        )
        self.assertEqual(descriptor.page_offset, -2)

    def test_t_non_default_page_offset_is_preserved(self):
        descriptor = DESCRIPTOR_MODULE.parse_opensearch_description(
            descriptor_xml(
                "https://reader.example.test/find?q={searchTerms}",
                page_offset="7",
            ),
            "https://reader.example.test/metadata/description.xml",
        )
        self.assertEqual(descriptor.page_offset, 7)

    def test_u_invalid_page_offset_candidate_is_rejected(self):
        for page_offset in ("abc", "", "1.5"):
            with self.subTest(page_offset=page_offset):
                with self.assertRaisesRegex(ValueError, "не содержит поддерживаемого"):
                    DESCRIPTOR_MODULE.parse_opensearch_description(
                        descriptor_xml(
                            "https://reader.example.test/find?q={searchTerms}",
                            page_offset=page_offset,
                        ),
                        "https://reader.example.test/metadata/description.xml",
                    )

    def test_v_valid_candidate_follows_invalid_page_offset_candidate(self):
        content = """<?xml version="1.0" encoding="UTF-8"?>
        <OpenSearchDescription xmlns="http://a9.com/-/spec/opensearch/1.1/">
          <ShortName>Synthetic</ShortName>
          <Url type="application/atom+xml" pageOffset="invalid"
               template="https://reader.example.test/invalid?q={searchTerms}" />
          <Url type="application/atom+xml" pageOffset="7"
               template="https://reader.example.test/valid?q={searchTerms}" />
        </OpenSearchDescription>"""
        descriptor = DESCRIPTOR_MODULE.parse_opensearch_description(
            content,
            "https://reader.example.test/metadata/description.xml",
        )
        self.assertEqual(
            descriptor.template,
            "https://reader.example.test/valid?q={searchTerms}",
        )
        self.assertEqual(descriptor.page_offset, 7)

    def test_w_direct_atom_descriptor_defaults_to_page_offset_one(self):
        descriptor = DESCRIPTOR_MODULE.resolve_direct_atom_search(
            DESCRIPTOR_MODULE.OPDSSearchRef(
                url="https://reader.example.test/find?q={searchTerms}",
                mime_type="application/atom+xml;profile=opds-catalog",
            )
        )
        self.assertEqual(descriptor.page_offset, 1)

    def test_x_realistic_start_page_fixture_is_preserved_for_expansion(self):
        descriptor = DESCRIPTOR_MODULE.parse_opensearch_description(
            START_PAGE_FIXTURE_CONTENT,
            "https://reader.example.test/metadata/description.xml",
        )
        self.assertEqual(
            descriptor.template,
            "https://reader.example.test/find?q={searchTerms}&page={startPage?}",
        )
        self.assertEqual(descriptor.page_offset, 1)


if __name__ == "__main__":
    unittest.main()
