import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


ATOM = "http://www.w3.org/2005/Atom"
ACQUISITION = "http://opds-spec.org/acquisition/open-access"
FIXTURE_DIR = Path(__file__).parent / "fixtures" / "opds"
FIXTURE_NAMES = (
    "navigation.xml",
    "publications_page_1.xml",
    "publications_page_2.xml",
    "no_search.xml",
    "relative_links.xml",
    "direct_fb2.xml",
    "search_navigation.xml",
    "direct_atom_search.xml",
    "search_results_page_1.xml",
    "search_results_page_2.xml",
    "search_results_related.xml",
    "mixed_publication_navigation.xml",
)
NS = {"atom": ATOM}


class OPDSFixtureTests(unittest.TestCase):
    def parse_fixture(self, name):
        return ET.parse(FIXTURE_DIR / name).getroot()

    def test_a_all_fixtures_are_valid_xml(self):
        for name in FIXTURE_NAMES:
            with self.subTest(fixture=name):
                self.assertIsNotNone(self.parse_fixture(name))

    def test_b_all_roots_are_atom_feeds(self):
        for name in FIXTURE_NAMES:
            with self.subTest(fixture=name):
                self.assertEqual(self.parse_fixture(name).tag, f"{{{ATOM}}}feed")

    def test_c_first_publication_page_has_generic_entries_and_next(self):
        root = self.parse_fixture("publications_page_1.xml")
        entries = root.findall("atom:entry", NS)
        self.assertGreaterEqual(len(entries), 2)
        self.assertTrue(
            any(link.get("rel") == "next" for link in root.findall("atom:link", NS))
        )
        for entry in entries:
            entry_id = (entry.findtext("atom:id", default="", namespaces=NS) or "").strip()
            self.assertTrue(entry_id)
            self.assertFalse(entry_id.isdigit())
            self.assertTrue(
                any(
                    link.get("rel") == ACQUISITION
                    for link in entry.findall("atom:link", NS)
                )
            )

    def test_d_second_publication_page_has_no_next(self):
        root = self.parse_fixture("publications_page_2.xml")
        self.assertFalse(
            any(link.get("rel") == "next" for link in root.findall("atom:link", NS))
        )

    def test_e_feed_without_search_has_no_search_capability(self):
        root = self.parse_fixture("no_search.xml")
        self.assertFalse(
            any(link.get("rel") == "search" for link in root.iterfind(".//atom:link", NS))
        )

    def test_f_relative_link_forms_are_present(self):
        root = self.parse_fixture("relative_links.xml")
        hrefs = {link.get("href") for link in root.iterfind(".//atom:link", NS)}
        self.assertTrue(
            {"book.epub", "./book.fb2", "../covers/cover.jpg", "?page=2", "/catalog/root"}
            <= hrefs
        )

    def test_g_fixtures_have_no_legacy_source_markers(self):
        forbidden = (
            "flibusta",
            "flibusta.is",
            "/b/",
            "/opds/author/",
            "/opds/sequencebooks/",
        )
        for name in FIXTURE_NAMES:
            text = (FIXTURE_DIR / name).read_text(encoding="utf-8").lower()
            with self.subTest(fixture=name):
                for marker in forbidden:
                    self.assertNotIn(marker, text)

    def test_h_related_fixture_has_multiple_generic_catalog_links(self):
        root = self.parse_fixture("search_results_related.xml")
        entries = root.findall("atom:entry", NS)
        related_counts = [
            sum(
                "related" in (link.get("rel") or "").split()
                for link in entry.findall("atom:link", NS)
            )
            for entry in entries
        ]
        self.assertEqual(len(entries), 2)
        self.assertGreaterEqual(related_counts[0], 2)
        self.assertGreaterEqual(related_counts[1], 3)

    def test_i_related_fixture_has_no_provider_specific_markers(self):
        text = (FIXTURE_DIR / "search_results_related.xml").read_text(
            encoding="utf-8"
        ).lower()
        for marker in (
            "flibusta",
            "sequencebooks",
            "/opds/author",
            "searchtype",
            "searchterm",
            "pagenumber",
        ):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, text)


if __name__ == "__main__":
    unittest.main()
