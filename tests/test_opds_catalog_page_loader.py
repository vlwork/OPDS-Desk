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


def load_catalog_page_module():
    """Загружает нейтральный page loader без runtime приложения."""
    source = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    wanted = {
        "SourceConfig",
        "normalize_app_config",
        "source_config_from_app_config",
        "normalize_opds_url",
        "resolve_opds_url",
        "AcquisitionLink",
        "BookRecord",
        "CatalogRef",
        "OPDSFeed",
        "OPDSCatalogPage",
        "OPDS1Provider",
        "_catalog_acquisition_format",
        "book_record_to_catalog_book",
        "HTTPFetchResult",
        "load_opds_catalog_page",
        "current_source_config",
        "load_current_opds_catalog_page",
    }
    body = []
    for node in tree.body:
        if getattr(node, "name", None) in wanted:
            body.append(node)
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name)
            and (
                target.id == "CONFIG_VERSION"
                or target.id.startswith("OPDS1_")
                or target.id == "OPENSEARCH_1_1"
            )
            for target in node.targets
        ):
            body.append(node)
    module = types.ModuleType("isolated_opds_catalog_page_loader_test")
    sys.modules[module.__name__] = module
    module.__dict__.update(
        dataclass=dataclasses.dataclass,
        hashlib=hashlib,
        ipaddress=ipaddress,
        ET=ET,
        DEFAULT_DESTINATION="test-default-library",
        APP_CONFIG={},
        urljoin=urljoin,
        urlsplit=urlsplit,
        urlunsplit=urlunsplit,
    )
    exec(compile(ast.Module(body=body, type_ignores=[]), "app.py", "exec"), module.__dict__)
    module.__source_text__ = source
    module.__source_tree__ = tree
    return module


PAGE_MODULE = load_catalog_page_module()


class FakeClient:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def fetch(self, url):
        self.calls.append(url)
        return self.result


def fixture_result(name, requested_url, final_url=None):
    return PAGE_MODULE.HTTPFetchResult(
        requested_url=requested_url,
        final_url=final_url or requested_url,
        content=(FIXTURE_DIR / name).read_bytes(),
        content_type="application/atom+xml",
    )


class OPDSCatalogPageLoaderTests(unittest.TestCase):
    def setUp(self):
        PAGE_MODULE.APP_CONFIG = {
            "config_version": PAGE_MODULE.CONFIG_VERSION,
            "opds_url": "",
            "source_id": "",
            "source_name": "",
            "library_path": "X:/Books",
            "setup_complete": True,
        }

    def test_a_first_publications_page_returns_adapted_books_and_next(self):
        page_url = "https://reader.example.test/catalog/pages/publications_page_1.xml"
        client = FakeClient(fixture_result("publications_page_1.xml", page_url))
        page = PAGE_MODULE.load_opds_catalog_page(
            page_url,
            source_id="sha256:source-test",
            client=client,
        )
        self.assertIsInstance(page, PAGE_MODULE.OPDSCatalogPage)
        self.assertGreaterEqual(len(page.books), 2)
        self.assertTrue(all(isinstance(book, dict) for book in page.books))
        required_fields = {
            "id",
            "source_id",
            "title",
            "author",
            "authors",
            "language",
            "genres",
            "acquisition_links",
            "epub_url",
            "fb2_url",
            "cover_url",
            "thumbnail_url",
            "web_url",
        }
        self.assertTrue(all(required_fields <= book.keys() for book in page.books))
        self.assertEqual(
            page.next_url,
            "https://reader.example.test/catalog/pages/publications_page_2.xml",
        )
        self.assertIsInstance(page.navigation, tuple)

    def test_b_relative_links_use_final_redirect_url(self):
        requested_url = "https://example.org/opds"
        final_url = "https://catalog.example.org/books/page1.xml"
        client = FakeClient(
            fixture_result(
                "publications_page_1.xml",
                requested_url,
                final_url,
            )
        )
        page = PAGE_MODULE.load_opds_catalog_page(requested_url, client=client)
        first = page.books[0]
        self.assertEqual(page.requested_url, requested_url)
        self.assertEqual(page.final_url, final_url)
        self.assertEqual(
            first["epub_url"],
            "https://catalog.example.org/books/books/clockwork-garden.epub",
        )
        self.assertEqual(
            first["cover_url"],
            "https://catalog.example.org/books/covers/clockwork-garden.jpg",
        )
        self.assertEqual(
            page.next_url,
            "https://catalog.example.org/books/publications_page_2.xml",
        )

    def test_c_source_id_reaches_every_catalog_book(self):
        page_url = "https://reader.example.test/catalog/publications.xml"
        client = FakeClient(fixture_result("publications_page_1.xml", page_url))
        page = PAGE_MODULE.load_opds_catalog_page(
            page_url,
            source_id="sha256:exact-source",
            client=client,
        )
        self.assertEqual(page.source_id, "sha256:exact-source")
        self.assertTrue(
            all(book["source_id"] == "sha256:exact-source" for book in page.books)
        )

    def test_d_uuid_and_uri_ids_remain_opaque_strings(self):
        page_url = "https://reader.example.test/catalog/publications.xml"
        client = FakeClient(fixture_result("publications_page_1.xml", page_url))
        page = PAGE_MODULE.load_opds_catalog_page(page_url, client=client)
        identifiers = [book["id"] for book in page.books]
        self.assertTrue(all(isinstance(identifier, str) for identifier in identifiers))
        self.assertTrue(all(not identifier.isdigit() for identifier in identifiers))
        self.assertTrue(any(identifier.startswith("urn:") for identifier in identifiers))
        self.assertTrue(any(identifier.startswith("tag:") for identifier in identifiers))

    def test_e_second_publications_page_has_no_next(self):
        page_url = "https://reader.example.test/catalog/publications_page_2.xml"
        client = FakeClient(fixture_result("publications_page_2.xml", page_url))
        page = PAGE_MODULE.load_opds_catalog_page(page_url, client=client)
        self.assertGreaterEqual(len(page.books), 1)
        self.assertEqual(page.next_url, "")

    def test_f_navigation_refs_remain_generic(self):
        page_url = "https://catalog.example.org/catalog/navigation.xml"
        client = FakeClient(fixture_result("navigation.xml", page_url))
        page = PAGE_MODULE.load_opds_catalog_page(page_url, client=client)
        self.assertEqual(page.books, ())
        self.assertGreaterEqual(len(page.navigation), 2)
        self.assertTrue(all(isinstance(ref, PAGE_MODULE.CatalogRef) for ref in page.navigation))
        self.assertTrue(all(isinstance(ref.url, str) for ref in page.navigation))
        self.assertTrue(
            all(
                ref.kind in {"acquisition", "navigation", "related", "unknown"}
                for ref in page.navigation
            )
        )

    def test_g_current_loader_rejects_empty_source(self):
        with self.assertRaisesRegex(ValueError, "не настроен"):
            PAGE_MODULE.load_current_opds_catalog_page(client=FakeClient(object()))

    def test_h_current_loader_uses_root_url_when_page_is_omitted(self):
        root_url = "https://catalog.example.org/root.xml"
        PAGE_MODULE.APP_CONFIG.update(
            opds_url=root_url,
            source_id="sha256:source-test",
            source_name="Example catalog",
        )
        client = FakeClient(fixture_result("navigation.xml", root_url))
        page = PAGE_MODULE.load_current_opds_catalog_page(client=client)
        self.assertEqual(client.calls, [root_url])
        self.assertEqual(page.source_id, "sha256:source-test")

    def test_i_current_loader_uses_explicit_cross_origin_page_url(self):
        PAGE_MODULE.APP_CONFIG.update(
            opds_url="https://catalog.example.org/root.xml",
            source_id="sha256:source-test",
        )
        page_url = "https://pages.example.net/next.xml"
        client = FakeClient(fixture_result("publications_page_2.xml", page_url))
        page = PAGE_MODULE.load_current_opds_catalog_page(page_url, client=client)
        self.assertEqual(client.calls, [page_url])
        self.assertEqual(page.final_url, page_url)

    def test_j_current_loader_does_not_mutate_app_config(self):
        root_url = "https://catalog.example.org/root.xml"
        PAGE_MODULE.APP_CONFIG.update(
            opds_url=root_url,
            source_id="sha256:source-test",
            custom_test=123,
        )
        snapshot = dict(PAGE_MODULE.APP_CONFIG)
        client = FakeClient(fixture_result("navigation.xml", root_url))
        PAGE_MODULE.load_current_opds_catalog_page(client=client)
        self.assertEqual(PAGE_MODULE.APP_CONFIG, snapshot)

    def test_k_new_loaders_have_no_legacy_url_assumptions(self):
        wanted = {"load_opds_catalog_page", "load_current_opds_catalog_page"}
        loader_source = "\n".join(
            ast.get_source_segment(PAGE_MODULE.__source_text__, node) or ""
            for node in PAGE_MODULE.__source_tree__.body
            if getattr(node, "name", None) in wanted
        ).lower()
        for marker in (
            "opds_base",
            "flibusta.is",
            "/opds/search",
            "/b/",
            "pagenumber",
            "searchtype",
        ):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, loader_source)

    def test_l_legacy_runtime_does_not_call_new_page_loaders(self):
        protected = {
            "catalog_start_url",
            "load_catalog_page",
            "collect_catalog",
            "save_epub",
            "save_fb2",
        }
        found = set()
        for node in PAGE_MODULE.__source_tree__.body:
            if getattr(node, "name", None) not in protected:
                continue
            found.add(node.name)
            called_names = {
                child.func.id
                for child in ast.walk(node)
                if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
            }
            self.assertNotIn("load_opds_catalog_page", called_names)
            self.assertNotIn("load_current_opds_catalog_page", called_names)
        self.assertEqual(found, protected)

    def test_m_total_results_is_transferred_to_catalog_page(self):
        page_url = "https://reader.example.test/find/results.xml"
        client = FakeClient(fixture_result("search_results_page_1.xml", page_url))
        page = PAGE_MODULE.load_opds_catalog_page(page_url, client=client)
        self.assertEqual(page.total_results, 57)

    def test_n_mixed_feed_keeps_publication_related_out_of_navigation(self):
        page_url = "https://reader.example.test/catalog/mixed.xml"
        client = FakeClient(
            fixture_result("mixed_publication_navigation.xml", page_url)
        )
        page = PAGE_MODULE.load_opds_catalog_page(page_url, client=client)
        self.assertEqual(
            [book["title"] for book in page.books],
            ["Example Publication"],
        )
        self.assertEqual(
            [ref.title for ref in page.books[0]["related"]],
            ["Books by Example Writer"],
        )
        self.assertEqual(
            [(ref.title, ref.url) for ref in page.navigation],
            [
                (
                    "Example Collection",
                    "https://reader.example.test/catalog/collection.xml",
                ),
                (
                    "Related Catalog",
                    "https://reader.example.test/catalog/related.xml",
                ),
            ],
        )


if __name__ == "__main__":
    unittest.main()
