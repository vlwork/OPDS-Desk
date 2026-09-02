import ast
import dataclasses
import hashlib
import ipaddress
import json
import re
import sys
import types
import unicodedata
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = PROJECT_ROOT / "tests" / "fixtures" / "opds"


def load_adapter_module():
    """Загружает provider и compatibility adapter без runtime приложения."""
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
        "_catalog_acquisition_format",
        "book_record_to_catalog_book",
        "choose_catalog_book_format",
        "catalog_book_has_downloadable_acquisition",
        "parse_download_count",
        "technical_title_flags",
        "metadata_quality",
        "duplicate_id_tiebreak",
        "duplicate_score",
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
    module = types.ModuleType("isolated_catalog_book_adapter_test")
    sys.modules[module.__name__] = module
    module.__dict__.update(
        dataclass=dataclasses.dataclass,
        hashlib=hashlib,
        ipaddress=ipaddress,
        re=re,
        unicodedata=unicodedata,
        ET=ET,
        urljoin=urljoin,
        urlsplit=urlsplit,
        urlunsplit=urlunsplit,
    )
    exec(compile(ast.Module(body=body, type_ignores=[]), "app.py", "exec"), module.__dict__)
    module.__source_text__ = source
    module.__source_tree__ = tree
    return module


ADAPTER_MODULE = load_adapter_module()
PROVIDER = ADAPTER_MODULE.OPDS1Provider()


def parse_fixture(name, page_url, source_id="sha256:source-test"):
    xml_text = (FIXTURE_DIR / name).read_bytes()
    return PROVIDER.parse_feed(xml_text, page_url, source_id)


class CatalogBookAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.feed = parse_fixture(
            "publications_page_1.xml",
            "https://reader.example.test/catalog/pages/publications_page_1.xml",
        )
        cls.record = cls.feed.publications[0]
        cls.catalog_book = ADAPTER_MODULE.book_record_to_catalog_book(cls.record)

    def test_a_fixture_record_converts_to_catalog_book_dict(self):
        self.assertIsInstance(self.catalog_book, dict)
        for field in (
            "id",
            "title",
            "author",
            "language",
            "genres",
            "author_links",
            "series_links",
            "epub",
            "fb2",
            "cover_href",
            "exists_any",
            "duplicate_group",
        ):
            with self.subTest(field=field):
                self.assertIn(field, self.catalog_book)

    def test_b_uuid_and_uri_source_item_ids_remain_strings(self):
        books = [
            ADAPTER_MODULE.book_record_to_catalog_book(record)
            for record in self.feed.publications
        ]
        self.assertTrue(all(isinstance(book["id"], str) for book in books))
        self.assertEqual(books[0]["id"], self.feed.publications[0].source_item_id)
        self.assertEqual(books[1]["id"], self.feed.publications[1].source_item_id)
        self.assertFalse(books[0]["id"].isdigit())
        self.assertFalse(books[1]["id"].isdigit())

    def test_c_authors_title_language_and_categories_are_preserved(self):
        self.assertEqual(self.catalog_book["title"], self.record.title)
        self.assertEqual(self.catalog_book["authors"], list(self.record.authors))
        self.assertEqual(self.catalog_book["author"], ", ".join(self.record.authors))
        self.assertEqual(self.catalog_book["language"], self.record.language)
        self.assertEqual(self.catalog_book["categories"], list(self.record.categories))
        self.assertEqual(self.catalog_book["genres"], list(self.record.categories))

    def test_d_epub_acquisition_url_and_mime_type_are_preserved(self):
        self.assertEqual(
            self.catalog_book["epub_url"],
            "https://reader.example.test/catalog/pages/books/clockwork-garden.epub",
        )
        self.assertEqual(self.catalog_book["epub_mime_type"], "application/epub+zip")
        self.assertEqual(
            ADAPTER_MODULE.choose_catalog_book_format(self.catalog_book, "auto"),
            "epub",
        )

    def test_e_fb2_acquisition_url_and_mime_type_are_preserved(self):
        self.assertEqual(
            self.catalog_book["fb2_url"],
            "https://reader.example.test/catalog/pages/books/clockwork-garden.fb2.zip",
        )
        self.assertEqual(self.catalog_book["fb2_mime_type"], "application/fb2+zip")
        self.assertEqual(
            ADAPTER_MODULE.choose_catalog_book_format(self.catalog_book, "fb2"),
            "fb2",
        )

    def test_f_cover_thumbnail_and_web_urls_are_preserved(self):
        self.assertEqual(self.catalog_book["cover_url"], self.record.cover_url)
        self.assertEqual(self.catalog_book["thumbnail_url"], self.record.thumbnail_url)
        self.assertEqual(self.catalog_book["web_url"], self.record.web_url)
        self.assertEqual(self.catalog_book["cover_href"], "")

    def test_g_adapter_output_has_no_source_specific_paths(self):
        serialized = json.dumps(self.catalog_book, ensure_ascii=False).lower()
        for marker in (
            "/b/",
            "/i/",
            "/ia/",
            "/opds/author/",
            "/opds/sequencebooks/",
        ):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, serialized)

    def test_h_duplicate_score_supports_all_string_id_forms(self):
        identifiers = (
            "123",
            "8a79d4d4-d228-4f9c-b1b0-3f12fd6787f7",
            "urn:uuid:45b1d56f-8a99-4c77-a28a-13ec9b641c83",
            "sha256:" + "a" * 64,
        )
        scores = []
        for identifier in identifiers:
            book = dict(self.catalog_book, id=identifier)
            score = ADAPTER_MODULE.duplicate_score(book)
            self.assertEqual(score, ADAPTER_MODULE.duplicate_score(book))
            scores.append(score)
        self.assertEqual(len(scores), len(identifiers))
        numeric_two = ADAPTER_MODULE.duplicate_score(dict(self.catalog_book, id="2"))
        numeric_ten = ADAPTER_MODULE.duplicate_score(dict(self.catalog_book, id="10"))
        self.assertGreater(numeric_ten, numeric_two)

    def test_i_adapter_never_creates_a_fictitious_numeric_id(self):
        adapted = ADAPTER_MODULE.book_record_to_catalog_book(self.record)
        self.assertEqual(adapted["id"], self.record.source_item_id)
        self.assertNotEqual(adapted["id"], "0")
        self.assertFalse(adapted["id"].isdigit())

    def test_j_acquisition_links_are_json_safe_and_complete(self):
        links = self.catalog_book["acquisition_links"]
        self.assertEqual(len(links), len(self.record.acquisition_links))
        self.assertTrue(all(set(link) == {"href", "mime_type", "rel"} for link in links))
        json.dumps(self.catalog_book, ensure_ascii=False)

    def test_k_direct_fb2_uses_its_declared_mime_type(self):
        feed = parse_fixture(
            "direct_fb2.xml",
            "https://reader.example.test/catalog/direct_fb2.xml",
        )
        book = ADAPTER_MODULE.book_record_to_catalog_book(feed.publications[0])
        self.assertEqual(book["fb2_mime_type"], "application/fb2+xml")
        self.assertEqual(
            book["fb2_url"],
            "https://reader.example.test/catalog/downloads/plain-fictionbook",
        )
        self.assertEqual(ADAPTER_MODULE.choose_catalog_book_format(book, "auto"), "fb2")

    def test_l_related_catalog_refs_are_preserved_as_internal_tuple(self):
        feed = parse_fixture(
            "search_results_related.xml",
            "https://reader.example.test/catalog/search/results.xml",
        )
        record = feed.publications[1]
        book = ADAPTER_MODULE.book_record_to_catalog_book(record)
        self.assertIsInstance(book["related"], tuple)
        self.assertEqual(book["related"], record.related)
        self.assertEqual(len(book["related"]), 3)
        self.assertTrue(
            all(
                isinstance(ref, ADAPTER_MODULE.CatalogRef)
                for ref in book["related"]
            )
        )

    def test_m_downloadable_acquisition_uses_supported_runtime_urls(self):
        cases = (
            (
                "epub",
                {"epub_url": "https://files.example.test/book.epub"},
                True,
            ),
            (
                "fb2",
                {"fb2_url": "https://files.example.test/book.fb2"},
                True,
            ),
            (
                "unsupported acquisition",
                {
                    "acquisition_links": [
                        {
                            "href": "https://files.example.test/book.pdf",
                            "mime_type": "application/pdf",
                        }
                    ]
                },
                False,
            ),
            (
                "web only",
                {"web_url": "https://catalog.example.test/book"},
                False,
            ),
            ("empty", {}, False),
        )
        for label, book, expected in cases:
            with self.subTest(label=label):
                self.assertIs(
                    ADAPTER_MODULE.catalog_book_has_downloadable_acquisition(
                        book
                    ),
                    expected,
                )


if __name__ == "__main__":
    unittest.main()
