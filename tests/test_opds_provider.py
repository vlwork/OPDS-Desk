import ast
import dataclasses
import hashlib
import ipaddress
import sys
import types
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = PROJECT_ROOT / "tests" / "fixtures" / "opds"


def load_provider_module():
    """Загружает только нейтральные URL и OPDS parser definitions."""
    source = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    wanted = {
        "normalize_opds_url",
        "resolve_opds_url",
        "same_origin",
        "is_safe_http_url",
        "AcquisitionLink",
        "BookRecord",
        "CatalogRef",
        "OPDSFeed",
        "OPDS1Provider",
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
    module = types.ModuleType("isolated_opds_provider_test")
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


PROVIDER_MODULE = load_provider_module()
PROVIDER = PROVIDER_MODULE.OPDS1Provider()


def parse_fixture(name, page_url, source_id="source-test"):
    xml_text = (FIXTURE_DIR / name).read_text(encoding="utf-8")
    return PROVIDER.parse_feed(xml_text, page_url, source_id)


def parse_total_results(value):
    total_results = (
        ""
        if value is None
        else f"<opensearch:totalResults>{value}</opensearch:totalResults>"
    )
    xml_text = f"""<feed xmlns="http://www.w3.org/2005/Atom"
        xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">
      <title>Synthetic search results</title>
      {total_results}
    </feed>"""
    return PROVIDER.parse_feed(
        xml_text,
        "https://reader.example.test/find/results.xml",
    )


class OPDS1ProviderTests(unittest.TestCase):
    def test_a_first_publication_page(self):
        page_url = "https://reader.example.test/catalog/pages/publications_page_1.xml"
        feed = parse_fixture("publications_page_1.xml", page_url)
        self.assertGreaterEqual(len(feed.publications), 2)
        first = feed.publications[0]
        self.assertIsInstance(first, PROVIDER_MODULE.BookRecord)
        self.assertEqual(first.source_id, "source-test")
        self.assertFalse(first.source_item_id.isdigit())
        self.assertEqual(first.title, "The Clockwork Garden")
        self.assertEqual(first.authors, ("Ada North", "Bruno Vale"))
        self.assertEqual(first.language, "en")
        self.assertIn("fiction", first.categories)
        mime_types = {link.mime_type for link in first.acquisition_links}
        self.assertIn("application/epub+zip", mime_types)
        self.assertIn("application/fb2+zip", mime_types)
        self.assertTrue(all(link.href.startswith("https://") for link in first.acquisition_links))
        self.assertEqual(
            first.cover_url,
            "https://reader.example.test/catalog/pages/covers/clockwork-garden.jpg",
        )
        self.assertEqual(
            first.thumbnail_url,
            "https://reader.example.test/catalog/pages/covers/thumbnails/clockwork-garden.jpg",
        )
        self.assertEqual(
            first.web_url,
            "https://catalog.example.test/books/clockwork-garden",
        )
        self.assertEqual(
            feed.next_url,
            "https://reader.example.test/catalog/pages/publications_page_2.xml",
        )

    def test_b_second_publication_page_has_no_next(self):
        feed = parse_fixture(
            "publications_page_2.xml",
            "https://reader.example.test/catalog/pages/publications_page_2.xml",
        )
        self.assertGreaterEqual(len(feed.publications), 1)
        self.assertEqual(feed.next_url, "")
        self.assertTrue(
            all(
                link.href.startswith("https://")
                for link in feed.publications[0].acquisition_links
            )
        )

    def test_c_navigation_uses_declared_relative_and_absolute_urls(self):
        feed = parse_fixture(
            "navigation.xml",
            "https://reader.example.test/catalog/navigation.xml",
        )
        urls = {ref.url for ref in feed.navigation}
        self.assertIn(
            "https://reader.example.test/catalog/publications_page_1.xml",
            urls,
        )
        self.assertIn("https://catalog.example.test/catalog/featured", urls)
        self.assertTrue(
            all(
                ref.kind in {"acquisition", "navigation", "related", "unknown"}
                for ref in feed.navigation
            )
        )

    def test_c2_acquisition_mime_has_priority_over_subsection_rel(self):
        feed = parse_fixture(
            "navigation.xml",
            "https://reader.example.test/catalog/navigation.xml",
        )
        recent = next(
            ref for ref in feed.navigation if ref.title == "Recent publications"
        )
        self.assertEqual(recent.kind, "acquisition")

    def test_c3_acquisition_mime_parameters_are_normalized(self):
        mime_types = (
            "application/atom+xml;profile=opds-catalog;kind=acquisition",
            "application/atom+xml; Profile = OPDS-Catalog; Kind = Acquisition",
            'application/atom+xml;profile="opds-catalog";kind="acquisition"',
        )
        for mime_type in mime_types:
            with self.subTest(mime_type=mime_type):
                xml_text = f"""<feed xmlns="http://www.w3.org/2005/Atom">
                  <title>Parameter variants</title>
                  <entry>
                    <title>Books</title>
                    <link rel="subsection" type='{mime_type}' href="books.xml" />
                  </entry>
                </feed>"""
                feed = PROVIDER.parse_feed(
                    xml_text,
                    "https://reader.example.test/catalog/root.xml",
                )
                self.assertEqual(feed.navigation[0].kind, "acquisition")

    def test_c4_navigation_mime_parameter_is_still_navigation(self):
        xml_text = """<feed xmlns="http://www.w3.org/2005/Atom">
          <title>Navigation parameter</title>
          <entry>
            <title>Sections</title>
            <link rel="related"
                  type='application/atom+xml; Profile = OPDS-Catalog; Kind = Navigation'
                  href="sections.xml" />
          </entry>
        </feed>"""
        feed = PROVIDER.parse_feed(
            xml_text,
            "https://reader.example.test/catalog/root.xml",
        )
        self.assertEqual(feed.navigation[0].kind, "related")

        xml_text = xml_text.replace('rel="related"', 'rel="subsection"')
        feed = PROVIDER.parse_feed(
            xml_text,
            "https://reader.example.test/catalog/root.xml",
        )
        self.assertEqual(feed.navigation[0].kind, "navigation")

    def test_d_direct_fb2_keeps_xml_mime_type(self):
        feed = parse_fixture(
            "direct_fb2.xml",
            "https://reader.example.test/catalog/direct_fb2.xml",
        )
        links = feed.publications[0].acquisition_links
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].mime_type, "application/fb2+xml")
        self.assertEqual(
            links[0].href,
            "https://reader.example.test/catalog/downloads/plain-fictionbook",
        )

    def test_e_relative_links_resolve_from_current_feed(self):
        feed = parse_fixture(
            "relative_links.xml",
            "https://reader.example.test/catalog/pages/relative_links.xml",
        )
        publication = feed.publications[0]
        acquisition_urls = {link.href for link in publication.acquisition_links}
        self.assertEqual(
            acquisition_urls,
            {
                "https://reader.example.test/catalog/pages/book.epub",
                "https://reader.example.test/catalog/pages/book.fb2",
            },
        )
        self.assertEqual(
            publication.cover_url,
            "https://reader.example.test/catalog/covers/cover.jpg",
        )
        self.assertEqual(feed.next_url, "https://reader.example.test/catalog/pages/relative_links.xml?page=2")
        self.assertIn(
            "https://reader.example.test/catalog/root",
            {ref.url for ref in feed.navigation},
        )

    def test_f_feed_without_search_parses_normally(self):
        feed = parse_fixture(
            "no_search.xml",
            "https://reader.example.test/catalog/no_search.xml",
        )
        self.assertEqual(feed.title, "Catalog without search")
        self.assertEqual(feed.publications, ())
        self.assertGreaterEqual(len(feed.navigation), 1)

    def test_g_missing_atom_id_uses_stable_string_fallback(self):
        xml_text = """<?xml version="1.0" encoding="UTF-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <id>urn:synthetic:feed</id><title>Synthetic</title>
          <updated>2026-01-15T00:00:00Z</updated>
          <entry><title>Identifier fallback</title><updated>2026-01-15T00:00:00Z</updated>
            <author><name>Test Author</name></author><summary>Test</summary>
            <link rel="http://opds-spec.org/acquisition/open-access"
                  type="application/epub+zip" href="book.epub" />
          </entry>
        </feed>"""
        page_url = "https://reader.example.test/catalog/synthetic.xml"
        first = PROVIDER.parse_feed(xml_text, page_url).publications[0].source_item_id
        second = PROVIDER.parse_feed(xml_text, page_url).publications[0].source_item_id
        self.assertIsInstance(first, str)
        self.assertTrue(first)
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("sha256:"))

    def test_h_invalid_xml_raises_value_error(self):
        with self.assertRaises(ValueError):
            PROVIDER.parse_feed("<feed>", "https://reader.example.test/catalog/feed.xml")

    def test_i_non_atom_feed_root_raises_value_error(self):
        with self.assertRaises(ValueError):
            PROVIDER.parse_feed(
                "<feed><title>Wrong namespace</title></feed>",
                "https://reader.example.test/catalog/feed.xml",
            )

    def test_j_provider_source_has_no_legacy_assumptions(self):
        wanted = {
            "AcquisitionLink",
            "BookRecord",
            "CatalogRef",
            "OPDSFeed",
            "OPDS1Provider",
        }
        snippets = []
        for node in PROVIDER_MODULE.__source_tree__.body:
            if getattr(node, "name", None) in wanted:
                snippets.append(ast.get_source_segment(PROVIDER_MODULE.__source_text__, node) or "")
        provider_source = "\n".join(snippets).lower()
        for marker in (
            "flibusta",
            "/b/",
            "/opds/author/",
            "/opds/sequencebooks/",
            "searchtype",
            "pagenumber",
        ):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, provider_source)

    def test_k_unsupported_thumbnail_does_not_drop_publication(self):
        xml_text = """<feed xmlns="http://www.w3.org/2005/Atom">
          <title>Images</title>
          <entry><id>urn:book:image</id><title>Image fallback</title>
            <link rel="http://opds-spec.org/acquisition/open-access"
                  type="application/epub+zip" href="book.epub" />
            <link rel="http://opds-spec.org/image/thumbnail"
                  type="image/png" href="data:image/png;base64,AAAA" />
          </entry>
        </feed>"""
        feed = PROVIDER.parse_feed(
            xml_text,
            "https://reader.example.test/catalog/images.xml",
        )
        self.assertEqual(len(feed.publications), 1)
        self.assertEqual(len(feed.publications[0].acquisition_links), 1)
        self.assertEqual(feed.publications[0].thumbnail_url, "")

    def test_l_bad_acquisition_does_not_hide_valid_acquisition(self):
        xml_text = """<feed xmlns="http://www.w3.org/2005/Atom">
          <title>Acquisitions</title>
          <entry><id>urn:book:acquisition</id><title>Valid format remains</title>
            <link rel="http://opds-spec.org/acquisition/open-access"
                  type="application/pdf" href="https://[broken" />
            <link rel="http://opds-spec.org/acquisition/open-access"
                  type="application/epub+zip" href="book.epub" />
          </entry>
        </feed>"""
        feed = PROVIDER.parse_feed(
            xml_text,
            "https://reader.example.test/catalog/acquisitions.xml",
        )
        self.assertEqual(len(feed.publications), 1)
        links = feed.publications[0].acquisition_links
        self.assertEqual(len(links), 1)
        self.assertEqual(
            links[0].href,
            "https://reader.example.test/catalog/book.epub",
        )

    def test_m_malformed_optional_urls_are_ignored(self):
        xml_text = """<feed xmlns="http://www.w3.org/2005/Atom">
          <title>Optional links</title>
          <entry><id>urn:book:optional</id><title>Optional links fail safely</title>
            <link rel="http://opds-spec.org/acquisition/open-access"
                  type="application/epub+zip" href="book.epub" />
            <link rel="http://opds-spec.org/image"
                  type="image/jpeg" href="https://[broken" />
            <link rel="alternate" type="text/html" href="file:///book.html" />
          </entry>
        </feed>"""
        feed = PROVIDER.parse_feed(
            xml_text,
            "https://reader.example.test/catalog/optional.xml",
        )
        self.assertEqual(len(feed.publications), 1)
        self.assertEqual(feed.publications[0].cover_url, "")
        self.assertEqual(feed.publications[0].web_url, "")

    def test_n_unsupported_next_url_is_ignored(self):
        xml_text = """<feed xmlns="http://www.w3.org/2005/Atom">
          <title>Invalid next</title>
          <link rel="next" type="application/atom+xml" href="file:///next.xml" />
          <entry><id>urn:book:next</id><title>Valid publication</title>
            <link rel="http://opds-spec.org/acquisition/open-access"
                  type="application/epub+zip" href="book.epub" />
          </entry>
        </feed>"""
        feed = PROVIDER.parse_feed(
            xml_text,
            "https://reader.example.test/catalog/page.xml",
        )
        self.assertEqual(len(feed.publications), 1)
        self.assertEqual(feed.next_url, "")

    def test_o_unsupported_navigation_url_is_ignored(self):
        xml_text = """<feed xmlns="http://www.w3.org/2005/Atom">
          <title>Invalid navigation</title>
          <entry><id>urn:navigation:invalid</id><title>Unsupported target</title>
            <link rel="subsection"
                  type="application/atom+xml;profile=opds-catalog;kind=navigation"
                  href="ftp://catalog.example.test/navigation.xml" />
          </entry>
        </feed>"""
        feed = PROVIDER.parse_feed(
            xml_text,
            "https://reader.example.test/catalog/navigation.xml",
        )
        self.assertEqual(feed.navigation, ())

    def test_p_total_results_is_parsed_from_generic_search_fixture(self):
        feed = parse_fixture(
            "search_results_page_1.xml",
            "https://reader.example.test/find/results.xml",
        )
        self.assertEqual(feed.total_results, 57)

    def test_q_zero_total_results_is_preserved(self):
        self.assertEqual(parse_total_results("0").total_results, 0)

    def test_r_missing_total_results_is_none(self):
        self.assertIsNone(parse_total_results(None).total_results)

    def test_s_empty_total_results_is_none(self):
        self.assertIsNone(parse_total_results("").total_results)

    def test_t_malformed_or_negative_total_results_is_none(self):
        for value in ("abc", "1.5", "-1"):
            with self.subTest(value=value):
                self.assertIsNone(parse_total_results(value).total_results)

    def test_u_malformed_total_results_keeps_other_feed_content(self):
        xml_text = """<feed xmlns="http://www.w3.org/2005/Atom"
            xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">
          <title>Mixed results</title>
          <opensearch:totalResults>abc</opensearch:totalResults>
          <link rel="next" href="?cursor=next" />
          <entry><id>urn:book:one</id><title>Book one</title>
            <link rel="http://opds-spec.org/acquisition/open-access"
                  type="application/epub+zip" href="book.epub" />
          </entry>
          <entry><id>urn:navigation:one</id><title>Navigation</title>
            <link rel="subsection" type="application/atom+xml"
                  href="navigation.xml" />
          </entry>
        </feed>"""
        feed = PROVIDER.parse_feed(
            xml_text,
            "https://reader.example.test/find/results.xml",
        )
        self.assertIsNone(feed.total_results)
        self.assertEqual(len(feed.publications), 1)
        self.assertEqual(len(feed.navigation), 1)
        self.assertEqual(
            feed.next_url,
            "https://reader.example.test/find/results.xml?cursor=next",
        )


class OPDSRelatedCatalogTests(unittest.TestCase):
    def parse_related_fixture(self, source_id="source-related"):
        return parse_fixture(
            "search_results_related.xml",
            "https://reader.example.test/catalog/search/results.xml",
            source_id=source_id,
        )

    def test_a_related_atom_links_are_parsed_in_provider_order(self):
        related = self.parse_related_fixture().publications[0].related
        self.assertEqual(
            [(ref.title, ref.url) for ref in related],
            [
                (
                    "Books by Example Writer",
                    "https://reader.example.test/catalog/authors/example.xml",
                ),
                (
                    "Featured Collection",
                    "https://collections.example.test/featured.xml",
                ),
            ],
        )

    def test_b_profile_mime_and_multi_token_rel_are_supported(self):
        related = self.parse_related_fixture().publications[0].related
        self.assertEqual(related[1].title, "Featured Collection")
        self.assertEqual(
            related[1].url,
            "https://collections.example.test/featured.xml",
        )

    def test_c_only_safe_titled_atom_related_links_are_included(self):
        related = self.parse_related_fixture().publications[0].related
        self.assertEqual(len(related), 2)
        urls = {ref.url for ref in related}
        self.assertNotIn("https://www.example.test/book.html", urls)
        self.assertTrue(all(ref.title.strip() for ref in related))
        self.assertTrue(all(ref.url.startswith(("http://", "https://")) for ref in related))

    def test_d_source_id_and_generic_kind_are_preserved(self):
        related = self.parse_related_fixture("urn:source:opaque").publications[0].related
        self.assertTrue(related)
        self.assertTrue(all(ref.source_id == "urn:source:opaque" for ref in related))
        self.assertTrue(all(ref.kind == "related" for ref in related))

    def test_e_distinct_related_links_are_kept_and_exact_duplicate_is_removed(self):
        feed = self.parse_related_fixture()
        first_related = feed.publications[0].related
        second_related = feed.publications[1].related
        self.assertEqual(len(first_related), 2)
        self.assertEqual(
            [ref.title for ref in second_related],
            [
                "Books by Another Writer",
                "Example Saga",
                "Extended Example Universe",
            ],
        )

    def test_f_related_is_an_immutable_tuple_on_frozen_book_record(self):
        record = self.parse_related_fixture().publications[0]
        self.assertIsInstance(record.related, tuple)
        self.assertTrue(all(isinstance(ref, PROVIDER_MODULE.CatalogRef) for ref in record.related))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            record.related = ()

    def test_g_malformed_optional_links_do_not_drop_publications(self):
        feed = self.parse_related_fixture()
        self.assertEqual(len(feed.publications), 2)
        self.assertEqual(feed.publications[0].title, "The Glass Harbor")

    def test_h_opaque_publication_ids_are_unchanged(self):
        identifiers = [
            record.source_item_id
            for record in self.parse_related_fixture().publications
        ]
        self.assertEqual(
            identifiers,
            [
                "urn:uuid:0d508a30-073f-4028-b522-592a2acbdb98",
                "tag:catalog.example.test,2026:stone-lantern",
            ],
        )
        self.assertTrue(all(not identifier.isdigit() for identifier in identifiers))

    def test_i_parser_has_no_registration_or_relationship_classification(self):
        node = next(
            node
            for node in PROVIDER_MODULE.__source_tree__.body
            if getattr(node, "name", None) == "OPDS1Provider"
        )
        method = next(
            child
            for child in node.body
            if getattr(child, "name", None) == "_parse_related_catalogs"
        )
        source = (
            ast.get_source_segment(PROVIDER_MODULE.__source_text__, method) or ""
        ).lower()
        for forbidden in (
            "register_catalog_ref",
            "register_catalog_navigation",
            "get_current_catalog_ref",
            "sequencebooks",
            "/opds/author",
            "searchtype",
            "searchterm",
            "pagenumber",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)


class OPDSMixedFeedClassificationTests(unittest.TestCase):
    def parse_mixed_fixture(self):
        return parse_fixture(
            "mixed_publication_navigation.xml",
            "https://reader.example.test/catalog/mixed.xml",
        )

    def test_a_publication_keeps_acquisition_and_related_catalog(self):
        feed = self.parse_mixed_fixture()
        self.assertEqual(len(feed.publications), 1)
        publication = feed.publications[0]
        self.assertEqual(publication.title, "Example Publication")
        self.assertEqual(
            [link.href for link in publication.acquisition_links],
            ["https://reader.example.test/catalog/book.epub"],
        )
        self.assertEqual(
            [(ref.title, ref.url) for ref in publication.related],
            [
                (
                    "Books by Example Writer",
                    "https://reader.example.test/catalog/writer.xml",
                )
            ],
        )

    def test_b_publication_links_are_not_duplicated_in_navigation(self):
        navigation = self.parse_mixed_fixture().navigation
        self.assertNotIn("Example Publication", {ref.title for ref in navigation})
        self.assertNotIn(
            "https://reader.example.test/catalog/writer.xml",
            {ref.url for ref in navigation},
        )

    def test_c_navigation_entries_keep_subsection_and_generic_related(self):
        navigation = self.parse_mixed_fixture().navigation
        self.assertEqual(
            [(ref.title, ref.url, ref.kind) for ref in navigation],
            [
                (
                    "Example Collection",
                    "https://reader.example.test/catalog/collection.xml",
                    "navigation",
                ),
                (
                    "Related Catalog",
                    "https://reader.example.test/catalog/related.xml",
                    "related",
                ),
            ],
        )


if __name__ == "__main__":
    unittest.main()
