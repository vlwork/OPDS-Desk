"""OPDS Desk — локальное desktop-приложение для работы с OPDS-каталогами.

Модуль объединяет интерфейс Flask/pywebview, поиск и каталоги OPDS,
загрузку файлов, SQLite-очередь и планировщик фоновых заданий.
"""

from flask import Flask, request, render_template_string, redirect, url_for, flash, Response, jsonify, session
from dataclasses import dataclass
import copy
import html
import hashlib
import ipaddress
import io
import json
import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
import zipfile
import unicodedata
import xml.etree.ElementTree as ET
from urllib.parse import quote, urljoin, urlencode, urlsplit, urlunsplit
from datetime import datetime, timedelta, timezone
import webview
import requests
from requests.exceptions import RequestException

APP_VERSION = "1.0.0"


# ============================================================
# Конфигурация приложения
# ============================================================

LEGACY_OPDS_BASE = "https://flibusta.is"

# Локальные данные desktop-версии.
APP_DATA_BASE_DIR = os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
LEGACY_APP_DATA_DIR = os.path.join(APP_DATA_BASE_DIR, "FlibustaBridge")
NEUTRAL_APP_DATA_DIR = os.path.join(APP_DATA_BASE_DIR, "OPDSDesk")


def app_data_dir_has_state(path):
    """Проверяет наличие основных persisted artifacts без изменения данных."""
    return (
        os.path.isfile(os.path.join(path, "config.json"))
        or os.path.isfile(os.path.join(path, "queue.db"))
        or os.path.isfile(os.path.join(path, "jobs.json"))
        or os.path.isdir(os.path.join(path, "Library"))
    )


def resolve_app_data_dir(neutral_dir, legacy_dir):
    """Выбирает neutral root, сохраняя доступ к meaningful legacy state."""
    if app_data_dir_has_state(neutral_dir):
        return neutral_dir
    if app_data_dir_has_state(legacy_dir):
        return legacy_dir
    return neutral_dir


APP_DATA_DIR = resolve_app_data_dir(
    NEUTRAL_APP_DATA_DIR,
    LEGACY_APP_DATA_DIR,
)

CONFIG_FILE = os.path.join(APP_DATA_DIR, "config.json")
JOB_STATE_FILE = os.path.join(APP_DATA_DIR, "jobs.json")
QUEUE_DB_FILE = os.path.join(APP_DATA_DIR, "queue.db")

DEFAULT_DESTINATION = os.path.join(APP_DATA_DIR, "Library")

# Версия схемы позволяет поэтапно расширять config.json без сброса
# уже сохранённых пользовательских настроек.
CONFIG_VERSION = 2


@dataclass(frozen=True)
class SourceConfig:
    """Нейтральное описание будущего пользовательского OPDS-источника."""

    source_id: str
    root_url: str
    display_name: str


def normalize_app_config(config):
    """Дополняет старую конфигурацию полями подготовительного OPDS-слоя."""
    normalized = dict(config) if isinstance(config, dict) else {}
    config_version = normalized.get("config_version")
    if type(config_version) is not int or config_version < CONFIG_VERSION:
        normalized["config_version"] = CONFIG_VERSION
    normalized.setdefault("opds_url", "")
    normalized.setdefault("source_id", "")
    normalized.setdefault("source_name", "")
    normalized.setdefault("library_path", DEFAULT_DESTINATION)
    normalized.setdefault("setup_complete", False)
    return normalized


def source_config_from_app_config(config):
    """Строит SourceConfig, пока не подключая его к рабочему OPDS runtime."""
    normalized = normalize_app_config(config)
    root_url = str(normalized.get("opds_url") or "").strip()
    if not root_url:
        return SourceConfig(source_id="", root_url="", display_name="")
    return SourceConfig(
        source_id=str(normalized.get("source_id") or "").strip(),
        root_url=root_url,
        display_name=str(normalized.get("source_name") or "").strip(),
    )


def normalize_opds_url(url):
    """Нормализует абсолютный HTTP(S) URL без привязки к источнику."""
    if not isinstance(url, str) or not url.strip():
        raise ValueError("OPDS URL не указан")
    try:
        parsed = urlsplit(url.strip())
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Некорректный OPDS URL") from exc
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise ValueError("Разрешены только HTTP и HTTPS URL")
    if not hostname:
        raise ValueError("В OPDS URL отсутствует hostname")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Credentials в OPDS URL запрещены")

    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        try:
            normalized_host = hostname.encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise ValueError("Некорректный hostname в OPDS URL") from exc
        if not normalized_host or any(
            char.isspace() or ord(char) < 32 or char in "/\\?#@:"
            for char in normalized_host
        ):
            raise ValueError("Некорректный hostname в OPDS URL")
        netloc = normalized_host
    else:
        normalized_host = address.compressed.lower()
        netloc = f"[{normalized_host}]" if address.version == 6 else normalized_host

    if port is not None:
        netloc += f":{port}"
    return urlunsplit((scheme, netloc, parsed.path, parsed.query, ""))


def make_source_id(url):
    """Строит стабильный идентификатор из нормализованного OPDS URL."""
    normalized_url = normalize_opds_url(url)
    digest = hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def build_source_config(url, display_name=""):
    """Создаёт SourceConfig без сети и изменения конфигурации приложения."""
    normalized_url = normalize_opds_url(url)
    return SourceConfig(
        source_id=make_source_id(normalized_url),
        root_url=normalized_url,
        display_name=str(display_name or "").strip(),
    )


def source_namespace(source_id):
    """Возвращает безопасный стабильный namespace для одного источника."""
    if not isinstance(source_id, str):
        raise ValueError("source_id должен быть строкой")
    normalized_id = source_id.strip()
    if not normalized_id:
        return "legacy"
    digest = hashlib.sha256(normalized_id.encode("utf-8")).hexdigest()
    return f"source-{digest}"


def _opaque_key_part(value, field_name):
    """Кодирует opaque string без предположений о его формате."""
    if not isinstance(value, str):
        raise ValueError(f"{field_name} должен быть строкой")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def catalog_cache_key(source_id, kind, catalog_id):
    """Строит подготовительный source-aware ключ каталожного cache."""
    return ":".join(
        (
            "opds-catalog",
            source_namespace(source_id),
            _opaque_key_part(kind, "kind"),
            _opaque_key_part(catalog_id, "catalog_id"),
        )
    )


def catalog_selection_storage_key(source_id, kind, catalog_id):
    """Строит source-aware ключ незавершённого выбора пользователя."""
    return ":".join(
        (
            "opds-selection",
            source_namespace(source_id),
            _opaque_key_part(kind, "kind"),
            _opaque_key_part(catalog_id, "catalog_id"),
        )
    )


def resolve_opds_url(base_url, href):
    """Разрешает ссылку относительно URL текущей OPDS-страницы."""
    normalized_base = normalize_opds_url(base_url)
    if not isinstance(href, str) or not href.strip():
        raise ValueError("OPDS href не указан")
    return normalize_opds_url(urljoin(normalized_base, href.strip()))


def same_origin(url_a, url_b):
    """Сравнивает scheme, hostname и эффективный порт двух HTTP(S) URL."""
    try:
        parsed_a = urlsplit(normalize_opds_url(url_a))
        parsed_b = urlsplit(normalize_opds_url(url_b))
    except ValueError:
        return False

    def effective_port(parsed):
        return parsed.port or (443 if parsed.scheme == "https" else 80)

    return (
        parsed_a.scheme == parsed_b.scheme
        and parsed_a.hostname == parsed_b.hostname
        and effective_port(parsed_a) == effective_port(parsed_b)
    )


def is_safe_http_url(url):
    """Проверяет HTTP(S) URL, не выбрасывая исключения наружу."""
    # Private IP и localhost разрешены сознательно: desktop-клиент может
    # работать с OPDS-сервером в домашней или иной локальной сети.
    try:
        normalize_opds_url(url)
        return True
    except (TypeError, ValueError):
        return False


OPDS1_ATOM = "http://www.w3.org/2005/Atom"
OPDS1_DC = "http://purl.org/dc/terms/"
OPENSEARCH_1_1 = "http://a9.com/-/spec/opensearch/1.1/"
OPENSEARCH_TERMS_PLACEHOLDER = "{searchTerms}"
OPENSEARCH_START_PAGE_PLACEHOLDER = "{startPage}"
OPENSEARCH_OPTIONAL_START_PAGE_PLACEHOLDER = "{startPage?}"
OPDS1_ACQUISITION_PREFIX = "http://opds-spec.org/acquisition"
OPDS1_IMAGE_RELS = {
    "http://opds-spec.org/image",
    "http://opds-spec.org/cover",
}
OPDS1_THUMBNAIL_RELS = {
    "http://opds-spec.org/image/thumbnail",
    "http://opds-spec.org/thumbnail",
}
OPDS1_NS = {"atom": OPDS1_ATOM, "dc": OPDS1_DC}


@dataclass(frozen=True)
class AcquisitionLink:
    """Абсолютная ссылка на доступный формат публикации."""

    href: str
    mime_type: str
    rel: str


@dataclass(frozen=True)
class BookRecord:
    """Нормализованная публикация без предположений о формате source ID."""

    source_id: str
    source_item_id: str
    title: str
    authors: tuple[str, ...]
    language: str
    categories: tuple[str, ...]
    acquisition_links: tuple[AcquisitionLink, ...]
    cover_url: str
    thumbnail_url: str
    web_url: str
    related: tuple["CatalogRef", ...] = ()


@dataclass(frozen=True)
class CatalogRef:
    """Ссылка на другой каталог, полученная непосредственно из Atom feed."""

    source_id: str
    url: str
    title: str
    kind: str


@dataclass(frozen=True)
class OPDSSearchRef:
    """Объявленная в OPDS feed ссылка на поддерживаемый механизм поиска."""

    url: str
    mime_type: str


@dataclass(frozen=True)
class OPDSSearchDescriptor:
    """Разрешённый безопасный шаблон поиска из OPDS/OpenSearch metadata."""

    template: str
    mime_type: str
    page_offset: int = 1


@dataclass(frozen=True)
class RegisteredCatalogRef:
    """Безопасное представление CatalogRef для будущего route/UI."""

    token: str
    title: str
    kind: str


@dataclass(frozen=True)
class RegisteredCatalogBookView:
    """Неизменяемые read-only метаданные книги без внешних URL."""

    id: str
    title: str
    author: str
    authors: tuple[str, ...]
    language: str
    genres: tuple[str, ...]
    formats: tuple[str, ...]
    translator: str
    size: str
    has_cover: bool
    related: tuple[RegisteredCatalogRef, ...] = ()


@dataclass(frozen=True)
class RegisteredCatalogView:
    """Read-only представление зарегистрированного OPDS-каталога."""

    token: str
    title: str
    books: tuple[RegisteredCatalogBookView, ...]
    page: int
    pages: int
    has_previous: bool
    has_next: bool
    view_all: bool
    navigation: tuple[RegisteredCatalogRef, ...]


@dataclass(frozen=True)
class OPDSSearchView:
    """Безопасное read-only представление одной страницы OPDS search."""

    query: str
    books: tuple[RegisteredCatalogBookView, ...]
    page: int
    has_previous: bool
    has_next: bool
    title: str
    total_results: int | None = None


MAX_CATALOG_REF_REGISTRY = 4096
catalog_ref_registry = {}
catalog_ref_registry_lock = threading.Lock()
MAX_OPDS_SEARCH_BOOK_REGISTRY = 8192
opds_search_book_registry = {}
opds_search_book_registry_lock = threading.Lock()
MAX_OPDS_SEARCH_QUEUE_SELECTION = 1000


def make_catalog_ref_token(source_id, url):
    """Строит стабильный opaque token без раскрытия URL источника."""
    if not isinstance(source_id, str):
        raise ValueError("source_id должен быть строкой")
    normalized_url = normalize_opds_url(url)
    token_payload = json.dumps(
        [source_id, normalized_url],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(token_payload.encode("utf-8")).hexdigest()
    return f"catalog:{digest}"


def register_catalog_ref(ref):
    """Регистрирует нормализованный CatalogRef только в памяти процесса."""
    if not isinstance(ref, CatalogRef):
        raise TypeError("ref должен быть CatalogRef")
    normalized_ref = CatalogRef(
        source_id=ref.source_id,
        url=normalize_opds_url(ref.url),
        title=ref.title,
        kind=ref.kind,
    )
    token = make_catalog_ref_token(normalized_ref.source_id, normalized_ref.url)
    with catalog_ref_registry_lock:
        catalog_ref_registry.pop(token, None)
        catalog_ref_registry[token] = normalized_ref
        while len(catalog_ref_registry) > MAX_CATALOG_REF_REGISTRY:
            oldest_token = next(iter(catalog_ref_registry))
            del catalog_ref_registry[oldest_token]
    return token


def get_catalog_ref(token):
    """Возвращает зарегистрированный CatalogRef без сети и persistence."""
    with catalog_ref_registry_lock:
        return catalog_ref_registry.get(token)


def get_current_catalog_ref(token):
    """Возвращает CatalogRef только для текущего настроенного источника."""
    ref = get_catalog_ref(token)
    if ref is None:
        return None
    source = current_source_config()
    if not source.root_url or ref.source_id != source.source_id:
        return None
    return ref


def clear_catalog_ref_registry():
    """Очищает только временный registry ссылок на OPDS-каталоги."""
    with catalog_ref_registry_lock:
        catalog_ref_registry.clear()


def register_catalog_refs(refs):
    """Регистрирует CatalogRef и возвращает безопасные read-only структуры."""
    registered = []
    for ref in refs:
        token = register_catalog_ref(ref)
        registered.append(
            RegisteredCatalogRef(
                token=token,
                title=ref.title,
                kind=ref.kind,
            )
        )
    return tuple(registered)


def register_catalog_navigation(refs):
    """Регистрирует navigation refs через общий registry helper."""
    return register_catalog_refs(refs)


def normalize_catalog_semantic_title(value):
    """Нормализует catalog title для ограниченного semantic matching."""
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized.strip(" \t\r\n\"'«»„“”‘’()[]{}.,:;!?—–-")


def is_author_related_catalog_title(title):
    """Проверяет только точные high-confidence формы author catalog title."""
    normalized = normalize_catalog_semantic_title(title)
    prefixes = (
        "все книги автора ",
        "книги автора ",
        "all books by ",
        "books by ",
    )
    return any(
        normalized.startswith(prefix) and normalized[len(prefix):].strip()
        for prefix in prefixes
    )


def is_alphabetical_catalog_title(title):
    """Проверяет ограниченные semantic titles алфавитного списка книг."""
    return normalize_catalog_semantic_title(title) in {
        "книги по алфавиту",
        "по алфавиту",
        "books alphabetically",
        "alphabetical",
        "alphabetical books",
        "books by title",
    }


def is_all_books_catalog_title(title):
    """Проверяет ограниченные semantic titles полного списка книг."""
    return normalize_catalog_semantic_title(title) in {
        "все книги",
        "все книги автора",
        "all books",
    }


def is_recent_catalog_title(title):
    """Исключает явно временные сортировки из preferred child selection."""
    return normalize_catalog_semantic_title(title) in {
        "по дате",
        "книги по дате",
        "книги по дате поступления",
        "новые",
        "новые книги",
        "new",
        "new books",
        "recent",
        "recent books",
    }


def select_preferred_registered_catalog_child(navigation):
    """Выбирает только один однозначный acquisition/alphabetical child."""
    candidates = tuple(
        item
        for item in navigation
        if isinstance(item, RegisteredCatalogRef)
        and not is_recent_catalog_title(item.title)
    )
    acquisition = tuple(item for item in candidates if item.kind == "acquisition")
    if len(acquisition) == 1:
        return acquisition[0]
    if not acquisition:
        unknown_alphabetical = tuple(
            item
            for item in candidates
            if item.kind == "unknown"
            and is_alphabetical_catalog_title(item.title)
        )
        return (
            unknown_alphabetical[0]
            if len(unknown_alphabetical) == 1
            else None
        )

    alphabetical = tuple(
        item
        for item in acquisition
        if is_alphabetical_catalog_title(item.title)
    )
    if len(alphabetical) == 1:
        return alphabetical[0]
    if len(alphabetical) > 1:
        return None

    all_books = tuple(
        item
        for item in acquisition
        if is_all_books_catalog_title(item.title)
    )
    return all_books[0] if len(all_books) == 1 else None


@dataclass(frozen=True)
class OPDSFeed:
    """Нейтральный результат разбора одной OPDS 1.x страницы."""

    title: str
    publications: tuple[BookRecord, ...]
    navigation: tuple[CatalogRef, ...]
    next_url: str
    search: "OPDSSearchRef | None" = None
    total_results: int | None = None


@dataclass(frozen=True)
class OPDSCatalogPage:
    """Нейтральная OPDS-страница на compatibility boundary Stage 1."""

    source_id: str
    requested_url: str
    final_url: str
    title: str
    books: tuple[dict, ...]
    navigation: tuple[CatalogRef, ...]
    next_url: str
    total_results: int | None = None


class OPDS1Provider:
    """Разбирает локальный Atom/OPDS XML без сети и runtime-интеграции."""

    def parse_feed(self, xml_text, page_url, source_id=""):
        """Проверяет XML и возвращает нормализованное содержимое feed."""
        normalized_page_url = normalize_opds_url(page_url)
        if not isinstance(xml_text, (str, bytes)):
            raise ValueError("OPDS XML должен быть строкой или bytes")
        try:
            root = ET.fromstring(xml_text)
        except (ET.ParseError, TypeError, ValueError) as exc:
            raise ValueError("Некорректный OPDS XML") from exc
        if root.tag != f"{{{OPDS1_ATOM}}}feed":
            raise ValueError("Корневой элемент OPDS XML должен быть Atom feed")
        total_results = None
        total_results_node = root.find(f"{{{OPENSEARCH_1_1}}}totalResults")
        if total_results_node is not None:
            raw_total_results = (total_results_node.text or "").strip()
            if raw_total_results:
                try:
                    parsed_total_results = int(raw_total_results)
                except ValueError:
                    pass
                else:
                    if parsed_total_results >= 0:
                        total_results = parsed_total_results
        return OPDSFeed(
            title=self.get_feed_title(root),
            publications=self.parse_publications(root, normalized_page_url, source_id),
            navigation=self.parse_navigation(root, normalized_page_url, source_id),
            next_url=self.get_next_url(root, normalized_page_url),
            search=self.get_search_ref(root, normalized_page_url),
            total_results=total_results,
        )

    def parse_publications(self, root, page_url, source_id=""):
        """Преобразует acquisition entries в BookRecord."""
        normalized_page_url = normalize_opds_url(page_url)
        publications = []
        for entry in root.findall("atom:entry", OPDS1_NS):
            acquisition_links = self._parse_acquisition_links(entry, normalized_page_url)
            if not acquisition_links:
                continue
            title = self._element_text(entry, "atom:title")
            authors = self._unique_values(
                self._element_text(node, "atom:name")
                for node in entry.findall("atom:author", OPDS1_NS)
            )
            categories = self._unique_values(
                (node.get("term") or "").strip()
                for node in entry.findall("atom:category", OPDS1_NS)
            )
            language = self._element_text(entry, "dc:language")
            source_item_id = self._element_text(entry, "atom:id")
            if not source_item_id:
                source_item_id = self._fallback_source_item_id(
                    normalized_page_url,
                    title,
                    authors,
                    acquisition_links,
                )
            cover_url, thumbnail_url = self._parse_images(entry, normalized_page_url)
            publications.append(
                BookRecord(
                    source_id=str(source_id or ""),
                    source_item_id=source_item_id,
                    title=title,
                    authors=authors,
                    language=language,
                    categories=categories,
                    acquisition_links=acquisition_links,
                    cover_url=cover_url,
                    thumbnail_url=thumbnail_url,
                    web_url=self._parse_web_url(entry, normalized_page_url),
                    related=self._parse_related_catalogs(
                        entry,
                        normalized_page_url,
                        source_id,
                    ),
                )
            )
        return tuple(publications)

    def _parse_related_catalogs(self, entry, page_url, source_id=""):
        """Возвращает нейтральные связанные Atom-каталоги одной публикации."""
        refs = []
        seen = set()
        excluded_rels = {"alternate", "self", "next", "search"}
        for link in entry.findall("atom:link", OPDS1_NS):
            rels = (link.get("rel") or "").split()
            mime_type = (link.get("type") or "").strip()
            base_mime_type = mime_type.split(";", 1)[0].strip().lower()
            if (
                "related" not in rels
                or base_mime_type != "application/atom+xml"
                or excluded_rels.intersection(rels)
                or any(self._is_acquisition_rel(rel) for rel in rels)
                or any(rel in OPDS1_IMAGE_RELS for rel in rels)
                or any(rel in OPDS1_THUMBNAIL_RELS for rel in rels)
            ):
                continue
            title = (link.get("title") or "").strip()
            href = (link.get("href") or "").strip()
            if not title or not href:
                continue
            try:
                resolved_url = resolve_opds_url(page_url, href)
            except ValueError:
                continue
            ref = CatalogRef(
                source_id=str(source_id or ""),
                url=resolved_url,
                title=title,
                kind="related",
            )
            identity = (ref.url, ref.title, ref.kind)
            if identity not in seen:
                seen.add(identity)
                refs.append(ref)
        return tuple(refs)

    def parse_navigation(self, root, page_url, source_id=""):
        """Возвращает ссылки на другие Atom feeds без специальных путей."""
        normalized_page_url = normalize_opds_url(page_url)
        refs = []
        seen = set()
        for entry in root.findall("atom:entry", OPDS1_NS):
            if self._entry_has_acquisition_link(entry):
                continue
            entry_title = self._element_text(entry, "atom:title")
            for link in entry.findall("atom:link", OPDS1_NS):
                ref = self._navigation_ref(
                    link,
                    entry_title,
                    normalized_page_url,
                    source_id,
                )
                if ref and (ref.url, ref.title, ref.kind) not in seen:
                    seen.add((ref.url, ref.title, ref.kind))
                    refs.append(ref)
        return tuple(refs)

    def get_next_url(self, root, page_url):
        """Возвращает абсолютный URL реальной Atom-ссылки rel=next."""
        normalized_page_url = normalize_opds_url(page_url)
        for link in root.findall("atom:link", OPDS1_NS):
            rels = (link.get("rel") or "").split()
            href = (link.get("href") or "").strip()
            if "next" in rels and href:
                try:
                    return resolve_opds_url(normalized_page_url, href)
                except ValueError:
                    continue
        return ""

    def get_search_ref(self, root, page_url):
        """Находит поддерживаемую feed-level ссылку rel=search без её загрузки."""
        normalized_page_url = normalize_opds_url(page_url)
        candidates = []
        priorities = {
            "application/opensearchdescription+xml": 0,
            "application/atom+xml": 1,
        }
        for position, link in enumerate(root.findall("atom:link", OPDS1_NS)):
            rels = (link.get("rel") or "").split()
            href = (link.get("href") or "").strip()
            mime_type = (link.get("type") or "").strip()
            base_mime_type = mime_type.split(";", 1)[0].strip().lower()
            if "search" not in rels or not href or base_mime_type not in priorities:
                continue
            try:
                resolved = resolve_opds_url(normalized_page_url, href)
            except ValueError:
                continue
            candidates.append(
                (
                    priorities[base_mime_type],
                    position,
                    OPDSSearchRef(url=resolved, mime_type=mime_type),
                )
            )
        if not candidates:
            return None
        return min(candidates, key=lambda candidate: candidate[:2])[2]

    def get_feed_title(self, root):
        """Возвращает очищенный Atom title текущего feed."""
        return self._element_text(root, "atom:title")

    @staticmethod
    def _element_text(parent, path):
        node = parent.find(path, OPDS1_NS)
        return (node.text or "").strip() if node is not None else ""

    @staticmethod
    def _unique_values(values):
        result = []
        seen = set()
        for value in values:
            if value and value not in seen:
                seen.add(value)
                result.append(value)
        return tuple(result)

    @staticmethod
    def _is_acquisition_rel(rel):
        return rel.startswith(OPDS1_ACQUISITION_PREFIX)

    def _entry_has_acquisition_link(self, entry):
        return any(
            self._is_acquisition_rel(rel)
            for link in entry.findall("atom:link", OPDS1_NS)
            for rel in (link.get("rel") or "").split()
        )

    def _parse_acquisition_links(self, entry, page_url):
        result = []
        for link in entry.findall("atom:link", OPDS1_NS):
            rel = (link.get("rel") or "").strip()
            href = (link.get("href") or "").strip()
            if self._is_acquisition_rel(rel) and href:
                try:
                    resolved = resolve_opds_url(page_url, href)
                except ValueError:
                    continue
                result.append(
                    AcquisitionLink(
                        href=resolved,
                        mime_type=(link.get("type") or "").strip(),
                        rel=rel,
                    )
                )
        return tuple(result)

    @staticmethod
    def _fallback_source_item_id(page_url, title, authors, acquisition_links):
        payload = "\n".join(
            (page_url, title, *authors, *(link.href for link in acquisition_links))
        )
        return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _parse_images(self, entry, page_url):
        cover_url = ""
        thumbnail_url = ""
        for link in entry.findall("atom:link", OPDS1_NS):
            rel = (link.get("rel") or "").strip()
            href = (link.get("href") or "").strip()
            if not href:
                continue
            if not thumbnail_url and rel in OPDS1_THUMBNAIL_RELS:
                try:
                    thumbnail_url = resolve_opds_url(page_url, href)
                except ValueError:
                    continue
            elif not cover_url and rel in OPDS1_IMAGE_RELS:
                try:
                    cover_url = resolve_opds_url(page_url, href)
                except ValueError:
                    continue
        return cover_url, thumbnail_url

    def _parse_web_url(self, entry, page_url):
        fallback = ""
        for link in entry.findall("atom:link", OPDS1_NS):
            if (link.get("rel") or "").strip() != "alternate":
                continue
            href = (link.get("href") or "").strip()
            if not href:
                continue
            try:
                resolved = resolve_opds_url(page_url, href)
            except ValueError:
                continue
            if (link.get("type") or "").split(";", 1)[0].strip().lower() == "text/html":
                return resolved
            if not fallback:
                fallback = resolved
        return fallback

    @staticmethod
    def _parse_mime_parameters(value):
        """Разбирает минимальный набор case-insensitive MIME parameters."""
        parameters = {}
        for raw_parameter in str(value or "").split(";")[1:]:
            raw_name, separator, raw_value = raw_parameter.partition("=")
            if not separator:
                continue
            name = raw_name.strip().casefold()
            parameter_value = raw_value.strip()
            if (
                len(parameter_value) >= 2
                and parameter_value[0] == '"'
                and parameter_value[-1] == '"'
            ):
                parameter_value = parameter_value[1:-1]
            if name:
                parameters[name] = parameter_value.strip().casefold()
        return parameters

    def _navigation_ref(self, link, title, page_url, source_id):
        rel = (link.get("rel") or "").strip()
        href = (link.get("href") or "").strip()
        mime_type = link.get("type") or ""
        if not href or self._is_acquisition_rel(rel):
            return None
        is_atom_feed = (
            mime_type.split(";", 1)[0].strip().casefold()
            == "application/atom+xml"
        )
        if not is_atom_feed and rel not in {"subsection", "related", "start"}:
            return None
        if rel in {"self", "next", "search", "alternate"}:
            return None
        mime_parameters = self._parse_mime_parameters(mime_type)
        is_opds_catalog = mime_parameters.get("profile") == "opds-catalog"
        if is_opds_catalog and mime_parameters.get("kind") == "acquisition":
            kind = "acquisition"
        elif rel == "related":
            kind = "related"
        elif (
            is_opds_catalog and mime_parameters.get("kind") == "navigation"
        ) or rel in {"subsection", "start"}:
            kind = "navigation"
        else:
            kind = "unknown"
        try:
            resolved = resolve_opds_url(page_url, href)
        except ValueError:
            return None
        return CatalogRef(
            source_id=str(source_id or ""),
            url=resolved,
            title=title or (link.get("title") or "").strip(),
            kind=kind,
        )


def _atom_search_mime_priority(mime_type):
    """Возвращает приоритет поддерживаемого Atom MIME или None."""
    if not isinstance(mime_type, str):
        return None
    parts = [part.strip() for part in mime_type.split(";")]
    if not parts or parts[0].lower() != "application/atom+xml":
        return None
    parameters = {}
    for part in parts[1:]:
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        parameters[name.strip().lower()] = value.strip().strip('"').lower()
    profile = parameters.get("profile")
    if profile is None:
        return 1
    return 0 if profile == "opds-catalog" else None


def resolve_opensearch_template(base_url, template):
    """Разрешает HTTP(S) search template, сохраняя стандартный placeholder."""
    if not isinstance(template, str) or not template.strip():
        raise ValueError("OpenSearch template не указан")
    template = template.strip()
    if any(char.isspace() or ord(char) < 32 for char in template):
        raise ValueError("Некорректный OpenSearch template")

    placeholder_markers = (
        (OPENSEARCH_TERMS_PLACEHOLDER, "__opds_search_terms_placeholder__"),
        (
            OPENSEARCH_OPTIONAL_START_PAGE_PLACEHOLDER,
            "__opds_optional_start_page_placeholder__",
        ),
        (OPENSEARCH_START_PAGE_PLACEHOLDER, "__opds_start_page_placeholder__"),
    )
    marker_corpus = "\n".join((template, str(base_url))).lower()
    if any(marker in marker_corpus for _, marker in placeholder_markers):
        raise ValueError("Некорректный OpenSearch template")
    masked_base_url = str(base_url)
    masked_template = template
    for placeholder, marker in placeholder_markers:
        masked_base_url = masked_base_url.replace(placeholder, marker)
        masked_template = masked_template.replace(placeholder, marker)
    resolved = resolve_opds_url(masked_base_url, masked_template)
    parsed = urlsplit(resolved)
    if any(marker in parsed.netloc.lower() for _, marker in placeholder_markers):
        raise ValueError("OpenSearch placeholder запрещён в hostname")
    for placeholder, marker in placeholder_markers:
        if placeholder in template and marker not in resolved:
            raise ValueError("Некорректный OpenSearch template")
        resolved = resolved.replace(marker, placeholder)
    return resolved


def parse_opensearch_description(xml_content, descriptor_url):
    """Разбирает OpenSearch 1.1 XML и возвращает поддерживаемый Atom template."""
    if not isinstance(xml_content, (str, bytes)):
        raise ValueError("OpenSearch XML должен быть строкой или bytes")
    try:
        root = ET.fromstring(xml_content)
    except (ET.ParseError, TypeError, ValueError) as exc:
        raise ValueError("Некорректный OpenSearch XML") from exc
    if root.tag != f"{{{OPENSEARCH_1_1}}}OpenSearchDescription":
        raise ValueError("Некорректный OpenSearch descriptor")

    candidates = []
    for position, node in enumerate(root.findall(f"{{{OPENSEARCH_1_1}}}Url")):
        mime_type = (node.get("type") or "").strip()
        priority = _atom_search_mime_priority(mime_type)
        template = (node.get("template") or "").strip()
        if priority is None or OPENSEARCH_TERMS_PLACEHOLDER not in template:
            continue
        try:
            raw_page_offset = node.get("pageOffset")
            page_offset = 1 if raw_page_offset is None else int(raw_page_offset.strip())
            resolved_template = resolve_opensearch_template(descriptor_url, template)
        except (AttributeError, TypeError, ValueError):
            continue
        candidates.append(
            (
                priority,
                position,
                OPDSSearchDescriptor(
                    template=resolved_template,
                    mime_type=mime_type,
                    page_offset=page_offset,
                ),
            )
        )
    if not candidates:
        raise ValueError(
            "OpenSearch descriptor не содержит поддерживаемого OPDS search template"
        )
    return min(candidates, key=lambda candidate: candidate[:2])[2]


def resolve_direct_atom_search(search_ref):
    """Проверяет direct Atom search reference без HTTP и подстановки запроса."""
    if not isinstance(search_ref, OPDSSearchRef):
        raise ValueError("Ожидается OPDSSearchRef")
    if _atom_search_mime_priority(search_ref.mime_type) is None:
        raise ValueError("Неподдерживаемый MIME direct Atom search")
    if OPENSEARCH_TERMS_PLACEHOLDER not in search_ref.url:
        raise ValueError("Direct Atom search не содержит {searchTerms}")
    return OPDSSearchDescriptor(
        template=resolve_opensearch_template(search_ref.url, search_ref.url),
        mime_type=search_ref.mime_type,
        page_offset=1,
    )


def normalize_opds_search_query(query):
    """Проверяет пользовательский запрос без source-specific преобразований."""
    if not isinstance(query, str) or not query.strip():
        raise ValueError("Поисковый запрос не указан")
    return query.strip()


def make_opds_search_book_token(source_id, query, source_item_id):
    """Строит opaque token полного snapshot книги в контексте OPDS search."""
    if not isinstance(source_id, str):
        raise ValueError("source_id должен быть строкой")
    normalized_query = normalize_opds_search_query(query)
    source_item_id = str(source_item_id or "")
    token_payload = json.dumps(
        [source_id, normalized_query, source_item_id],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(token_payload.encode("utf-8")).hexdigest()
    return f"search-book:{digest}"


def register_opds_search_book(source_id, query, book):
    """Сохраняет полную server-side копию книги для search context."""
    if not isinstance(book, dict):
        raise ValueError("Ожидается словарь книги")
    source_item_id = str(book.get("id") or "")
    if not source_item_id:
        raise ValueError("У книги отсутствует source item ID")
    token = make_opds_search_book_token(source_id, query, source_item_id)
    snapshot = copy.deepcopy(book)
    snapshot["source_id"] = str(source_id)
    snapshot["id"] = source_item_id
    with opds_search_book_registry_lock:
        opds_search_book_registry.pop(token, None)
        opds_search_book_registry[token] = snapshot
        while len(opds_search_book_registry) > MAX_OPDS_SEARCH_BOOK_REGISTRY:
            oldest_token = next(iter(opds_search_book_registry))
            del opds_search_book_registry[oldest_token]
    return token


def get_opds_search_book(token):
    """Возвращает глубокую копию зарегистрированного search book snapshot."""
    with opds_search_book_registry_lock:
        snapshot = opds_search_book_registry.get(token)
        return copy.deepcopy(snapshot) if snapshot is not None else None


def resolve_opds_search_book(source_id, query, source_item_id):
    """Разрешает полный snapshot только в исходном search context."""
    source_item_id = str(source_item_id or "")
    token = make_opds_search_book_token(source_id, query, source_item_id)
    snapshot = get_opds_search_book(token)
    if snapshot is None:
        return None
    if (
        snapshot.get("source_id") != source_id
        or snapshot.get("id") != source_item_id
    ):
        return None
    return snapshot


def clear_opds_search_book_registry():
    """Явно очищает только in-memory registry search book snapshots."""
    with opds_search_book_registry_lock:
        opds_search_book_registry.clear()


def unique_opaque_ids(values):
    """Удаляет пустые и повторные opaque IDs, сохраняя исходный порядок."""
    result = []
    seen = set()
    for value in values:
        source_item_id = str(value)
        if not source_item_id or source_item_id in seen:
            continue
        seen.add(source_item_id)
        result.append(source_item_id)
    return tuple(result)


def resolve_opds_search_selection(source_id, query, source_item_ids):
    """Атомарно восстанавливает полные snapshots выбранного search context."""
    normalized_query = normalize_opds_search_query(query)
    resolved = []
    for source_item_id in unique_opaque_ids(source_item_ids):
        book = resolve_opds_search_book(
            source_id,
            normalized_query,
            source_item_id,
        )
        if book is None:
            raise ValueError(
                "Выбранные книги устарели. Обновите результаты поиска и повторите выбор."
            )
        resolved.append(book)
    return resolved


def expand_opds_search_template(descriptor, query):
    """Подставляет query и initial page offset в поддерживаемые placeholders."""
    if not isinstance(descriptor, OPDSSearchDescriptor):
        raise ValueError("Ожидается OPDSSearchDescriptor")
    if _atom_search_mime_priority(descriptor.mime_type) is None:
        raise ValueError("Неподдерживаемый MIME search descriptor")
    if (
        not isinstance(descriptor.template, str)
        or OPENSEARCH_TERMS_PLACEHOLDER not in descriptor.template
    ):
        raise ValueError("Search template не содержит {searchTerms}")
    if type(descriptor.page_offset) is not int:
        raise ValueError("Некорректный OpenSearch pageOffset")

    normalized_query = normalize_opds_search_query(query)
    encoded_query = quote(normalized_query, safe="")
    safe_template = resolve_opensearch_template(
        descriptor.template,
        descriptor.template,
    )
    expanded_url = safe_template.replace(
        OPENSEARCH_TERMS_PLACEHOLDER,
        encoded_query,
    )
    initial_page = str(descriptor.page_offset)
    expanded_url = expanded_url.replace(
        OPENSEARCH_OPTIONAL_START_PAGE_PLACEHOLDER,
        initial_page,
    ).replace(
        OPENSEARCH_START_PAGE_PLACEHOLDER,
        initial_page,
    )
    if "{" in expanded_url or "}" in expanded_url:
        raise ValueError("Search template содержит неподдерживаемые placeholders")
    return normalize_opds_url(expanded_url)


def _catalog_acquisition_format(link):
    """Определяет поддерживаемый формат только по acquisition MIME type."""
    mime_type = (link.mime_type or "").split(";", 1)[0].strip().lower()
    if mime_type == "application/epub+zip":
        return "epub"
    if "fb2" in mime_type or "fictionbook" in mime_type:
        return "fb2"
    return ""


def book_record_to_catalog_book(book):
    """Преобразует BookRecord в совместимый словарь каталожной книги."""
    if not isinstance(book, BookRecord):
        raise ValueError("Ожидается BookRecord")

    authors = [str(name) for name in book.authors]
    acquisition_links = []
    format_links = {}
    format_mime_types = {}
    for link in book.acquisition_links:
        acquisition_links.append(
            {
                "href": link.href,
                "mime_type": link.mime_type,
                "rel": link.rel,
            }
        )
        file_format = _catalog_acquisition_format(link)
        if file_format and file_format not in format_links:
            format_links[file_format] = link.href
            format_mime_types[file_format] = link.mime_type

    return {
        "source_id": str(book.source_id or ""),
        "id": str(book.source_item_id or ""),
        "title": str(book.title or "Без названия"),
        "authors": authors,
        "author": ", ".join(authors) if authors else "Неизвестный автор",
        "language": str(book.language or ""),
        "categories": list(book.categories),
        "genres": list(book.categories),
        "related": tuple(book.related),
        "author_links": [],
        "series_links": [],
        "translator": "",
        "size": "",
        "size_bytes": 0,
        "downloads": "",
        "acquisition_links": acquisition_links,
        "epub": bool(format_links.get("epub")),
        "fb2": bool(format_links.get("fb2")),
        "epub_url": format_links.get("epub", ""),
        "fb2_url": format_links.get("fb2", ""),
        "epub_mime_type": format_mime_types.get("epub", ""),
        "fb2_mime_type": format_mime_types.get("fb2", ""),
        "cover_url": str(book.cover_url or ""),
        "thumbnail_url": str(book.thumbnail_url or ""),
        "web_url": str(book.web_url or ""),
        "cover_href": "",
        "exists_epub": False,
        "exists_fb2": False,
        "exists_any": False,
        "duplicate_count": 1,
        "duplicate_preferred": True,
        "duplicate_group": str(book.source_item_id or ""),
        "duplicate_exists_epub": False,
        "duplicate_exists_fb2": False,
        "duplicate_exists_any": False,
    }


def choose_catalog_book_format(book, mode):
    """Выбирает EPUB/FB2 только при наличии фактического acquisition URL."""
    available = {
        "epub": bool(book.get("epub_url")),
        "fb2": bool(book.get("fb2_url")),
    }
    if mode in {"epub", "fb2"}:
        return mode if available[mode] else None
    if available["epub"]:
        return "epub"
    if available["fb2"]:
        return "fb2"
    return None


def catalog_book_has_downloadable_acquisition(book):
    """Отличает реально скачиваемую клиентом publication от формальной acquisition."""
    return choose_catalog_book_format(book, "auto") is not None


@dataclass(frozen=True)
class HTTPFetchResult:
    """Нейтральный результат загрузки одной HTTP(S)-страницы."""

    requested_url: str
    final_url: str
    content: bytes
    content_type: str


@dataclass(frozen=True)
class SourceValidationResult:
    """Результат безопасной проверки пользовательского OPDS URL."""

    valid: bool
    normalized_url: str
    final_url: str
    title: str
    error: str


def apply_validated_source(config, validation_result):
    """Возвращает копию config с успешно проверенным OPDS-источником."""
    if (
        not isinstance(validation_result, SourceValidationResult)
        or validation_result.valid is not True
    ):
        raise ValueError("Нельзя сохранить непроверенный OPDS-источник")
    canonical_url = validation_result.final_url or validation_result.normalized_url
    source = build_source_config(canonical_url, validation_result.title)
    updated = normalize_app_config(config)
    updated.update(
        config_version=updated["config_version"],
        opds_url=source.root_url,
        source_id=source.source_id,
        source_name=source.display_name,
    )
    return updated


class OPDSHTTPClient:
    """Загружает OPDS 1.x feed без привязки к конкретному источнику."""

    USER_AGENT = "OPDS-Desktop-Client/1.0"
    RETRYABLE_STATUS_CODES = frozenset({500, 502, 503, 504})
    RETRYABLE_TRANSPORT_ERRORS = (
        requests.exceptions.ReadTimeout,
        requests.exceptions.ConnectTimeout,
        requests.exceptions.ConnectionError,
    )
    MAX_ATTEMPTS = 4
    RETRY_DELAYS = (0.5, 1.0, 2.0)

    def __init__(self, session=None, timeout=15, max_response_bytes=10 * 1024 * 1024):
        if type(max_response_bytes) is not int or max_response_bytes <= 0:
            raise ValueError("Лимит размера OPDS-ответа должен быть положительным")
        if session is None:
            session = requests.Session()
        self.session = session
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes

    def fetch(self, url):
        """Выполняет ограниченный по размеру HTTP GET и возвращает bytes."""
        # Локальные и private-адреса разрешены сознательно: desktop-клиент
        # может работать с OPDS-сервером в пользовательской локальной сети.
        requested_url = normalize_opds_url(url)
        for attempt in range(self.MAX_ATTEMPTS):
            try:
                response = self.session.get(
                    requested_url,
                    timeout=self.timeout,
                    stream=True,
                    allow_redirects=True,
                    headers={"User-Agent": self.USER_AGENT},
                )
            except self.RETRYABLE_TRANSPORT_ERRORS:
                if attempt >= self.MAX_ATTEMPTS - 1:
                    raise
                time.sleep(self.RETRY_DELAYS[attempt])
                continue
            retry_delay = None
            try:
                if (
                    response.status_code in self.RETRYABLE_STATUS_CODES
                    and attempt < self.MAX_ATTEMPTS - 1
                ):
                    retry_delay = self.RETRY_DELAYS[attempt]
                else:
                    response.raise_for_status()
                    final_url = normalize_opds_url(response.url)
                    content_length = response.headers.get("Content-Length")
                    if content_length:
                        try:
                            declared_size = int(content_length)
                        except (TypeError, ValueError):
                            declared_size = None
                        if (
                            declared_size is not None
                            and declared_size > self.max_response_bytes
                        ):
                            raise ValueError(
                                "Ответ OPDS-сервера превышает допустимый размер"
                            )

                    content = bytearray()
                    for chunk in response.iter_content(chunk_size=64 * 1024):
                        if not chunk:
                            continue
                        content.extend(chunk)
                        if len(content) > self.max_response_bytes:
                            raise ValueError(
                                "Ответ OPDS-сервера превышает допустимый размер"
                            )
                    return HTTPFetchResult(
                        requested_url=requested_url,
                        final_url=final_url,
                        content=bytes(content),
                        content_type=(
                            response.headers.get("Content-Type") or ""
                        ).strip(),
                    )
            finally:
                response.close()
            time.sleep(retry_delay)

    def fetch_feed(self, url, source_id=""):
        """Загружает и разбирает feed относительно URL после redirect."""
        result = self.fetch(url)
        return OPDS1Provider().parse_feed(
            result.content,
            result.final_url,
            source_id,
        )


def resolve_opds_search_descriptor(search_ref, client=None):
    """Разрешает обнаруженную search capability без выполнения самого поиска."""
    if not isinstance(search_ref, OPDSSearchRef):
        raise ValueError("Ожидается OPDSSearchRef")
    base_mime_type = search_ref.mime_type.split(";", 1)[0].strip().lower()
    if base_mime_type == "application/atom+xml":
        return resolve_direct_atom_search(search_ref)
    if base_mime_type != "application/opensearchdescription+xml":
        raise ValueError("Неподдерживаемый MIME search descriptor")

    http_client = client if client is not None else OPDSHTTPClient()
    result = http_client.fetch(search_ref.url)
    return parse_opensearch_description(result.content, result.final_url)


def load_opds_catalog_page(page_url, source_id="", client=None):
    """Загружает одну OPDS-страницу и адаптирует её публикации для каталога."""
    normalized_page_url = normalize_opds_url(page_url)
    http_client = client if client is not None else OPDSHTTPClient()
    result = http_client.fetch(normalized_page_url)
    feed = OPDS1Provider().parse_feed(
        result.content,
        result.final_url,
        source_id=source_id,
    )
    return OPDSCatalogPage(
        source_id=str(source_id or ""),
        requested_url=result.requested_url,
        final_url=result.final_url,
        title=feed.title,
        books=tuple(
            book_record_to_catalog_book(book)
            for book in feed.publications
        ),
        navigation=feed.navigation,
        next_url=feed.next_url,
        total_results=feed.total_results,
    )


def load_opds_search_page(descriptor, query, source_id="", client=None):
    """Загружает одну страницу search results через neutral catalog pipeline."""
    search_url = expand_opds_search_template(descriptor, query)
    return load_opds_catalog_page(
        search_url,
        source_id=source_id,
        client=client,
    )


def _validate_opds_search_page_number(page):
    """Проверяет нулевой индекс страницы neutral OPDS search chain."""
    if type(page) is not int or page < 0 or page >= MAX_CATALOG_PAGES:
        raise ValueError("Некорректный номер страницы OPDS search")
    return page


def opds_search_cache_identity(source_id, descriptor, query):
    """Строит стабильную identity source/query/descriptor без Python hash()."""
    if not isinstance(source_id, str):
        raise ValueError("source_id должен быть строкой")
    if not isinstance(descriptor, OPDSSearchDescriptor):
        raise ValueError("Ожидается OPDSSearchDescriptor")
    if (
        not isinstance(descriptor.template, str)
        or not isinstance(descriptor.mime_type, str)
        or type(descriptor.page_offset) is not int
    ):
        raise ValueError("Некорректный OPDSSearchDescriptor")
    normalized_query = normalize_opds_search_query(query)
    payload = json.dumps(
        [
            source_id,
            descriptor.template,
            descriptor.mime_type,
            descriptor.page_offset,
            normalized_query,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"opds-search:{digest}"


def opds_search_page_cache_key(source_id, descriptor, query, page):
    """Возвращает source-aware cache key одной страницы search results."""
    page = _validate_opds_search_page_number(page)
    return (
        opds_search_cache_identity(source_id, descriptor, query),
        page,
    )


def _cached_opds_search_page(cache_key):
    """Возвращает независимую копию свежей search page из памяти."""
    with opds_search_page_cache_lock:
        cached = opds_search_page_cache.get(cache_key)
        if not cached:
            return None
        if time.time() - cached["time"] >= SEARCH_CACHE_TTL:
            opds_search_page_cache.pop(cache_key, None)
            return None
        return copy.deepcopy(cached["page"])


def _store_opds_search_page(chain_identity, page, result):
    """Сохраняет страницу и удаляет зависящий от неё хвост той же chain."""
    cache_key = (chain_identity, page)
    with opds_search_page_cache_lock:
        downstream = [
            key
            for key in opds_search_page_cache
            if isinstance(key, tuple)
            and len(key) == 2
            and key[0] == chain_identity
            and key[1] > page
        ]
        for key in downstream:
            opds_search_page_cache.pop(key, None)
        opds_search_page_cache[cache_key] = {
            "time": time.time(),
            "page": copy.deepcopy(result),
        }


def load_cached_opds_search_page(
    descriptor,
    query,
    source_id="",
    page=0,
    force=False,
    client=None,
):
    """Загружает страницу по cached chain реальных OPDS rel=next URL."""
    page = _validate_opds_search_page_number(page)
    chain_identity = opds_search_cache_identity(source_id, descriptor, query)
    normalized_query = normalize_opds_search_query(query)
    seen_urls = set()
    previous_page = None

    for current_page in range(page + 1):
        expected_url = ""
        if current_page > 0:
            expected_url = previous_page.next_url
            if not expected_url:
                raise ValueError("В OPDS search results отсутствует следующая страница")
            expected_url = normalize_opds_url(expected_url)
            if expected_url in seen_urls:
                raise ValueError("Обнаружен цикл в OPDS search pagination")

        cache_key = (chain_identity, current_page)
        use_cache = not (force and current_page == page)
        result = _cached_opds_search_page(cache_key) if use_cache else None
        if result is not None and expected_url and result.requested_url != expected_url:
            result = None

        if result is None:
            if current_page == 0:
                result = load_opds_search_page(
                    descriptor,
                    normalized_query,
                    source_id=source_id,
                    client=client,
                )
            else:
                result = load_opds_catalog_page(
                    expected_url,
                    source_id=source_id,
                    client=client,
                )
            if not isinstance(result, OPDSCatalogPage):
                raise ValueError("Некорректная страница OPDS search results")
            if expected_url and result.requested_url != expected_url:
                raise ValueError("Нарушена цепочка OPDS search pagination")
            _store_opds_search_page(chain_identity, current_page, result)

        page_urls = {
            normalize_opds_url(result.requested_url),
            normalize_opds_url(result.final_url),
        }
        if seen_urls.intersection(page_urls):
            raise ValueError("Обнаружен цикл в OPDS search pagination")
        seen_urls.update(page_urls)
        previous_page = result

    return previous_page


def validate_opds_source(url, client=None):
    """Проверяет пользовательский URL без traceback для ожидаемых ошибок."""
    normalized_url = ""
    final_url = ""
    try:
        normalized_url = normalize_opds_url(url)
        http_client = client if client is not None else OPDSHTTPClient()
        result = http_client.fetch(normalized_url)
        final_url = result.final_url
        feed = OPDS1Provider().parse_feed(result.content, final_url)
        return SourceValidationResult(
            valid=True,
            normalized_url=normalized_url,
            final_url=final_url,
            title=feed.title,
            error="",
        )
    except requests.Timeout:
        error = "Истекло время ожидания ответа OPDS-сервера"
    except requests.ConnectionError:
        error = "Не удалось подключиться к OPDS-серверу"
    except requests.HTTPError:
        error = "OPDS-сервер вернул HTTP-ошибку"
    except requests.RequestException:
        error = "Ошибка HTTP-запроса к OPDS-серверу"
    except ValueError as exc:
        error = str(exc) or "Некорректный OPDS-источник"
    return SourceValidationResult(
        valid=False,
        normalized_url=normalized_url,
        final_url=final_url,
        title="",
        error=error,
    )


def load_app_config():
    """Загружает каталог библиотеки и состояние первого запуска."""
    os.makedirs(APP_DATA_DIR, exist_ok=True)

    config = {
        "library_path": DEFAULT_DESTINATION,
        "setup_complete": False,
    }

    if os.path.isfile(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)

            if isinstance(saved, dict):
                config.update(saved)
                library_path = str(saved.get("library_path") or "").strip()
                if library_path:
                    config["library_path"] = library_path
                else:
                    config["library_path"] = DEFAULT_DESTINATION
                config["setup_complete"] = bool(
                    saved.get("setup_complete", False)
                )
        except (OSError, ValueError, TypeError):
            pass

    return normalize_app_config(config)


def save_app_config(config):
    """Атомарно сохраняет пользовательскую конфигурацию в JSON."""
    os.makedirs(APP_DATA_DIR, exist_ok=True)

    tmp_file = CONFIG_FILE + ".tmp"
    normalized = normalize_app_config(config)

    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(normalized, f, ensure_ascii=False, indent=2)

    os.replace(tmp_file, CONFIG_FILE)

def set_library_path(path):
    """Проверяет выбранный каталог и назначает его библиотекой."""
    global DESTINATION, APP_CONFIG

    path = str(path or "").strip()
    if not path:
        raise ValueError("Путь к библиотеке не указан")

    path = os.path.abspath(os.path.expanduser(path))

    if not os.path.isdir(path):
        raise ValueError("Выбранная папка не существует")

    # Проверяем реальную возможность записи.
    test_file = os.path.join(
        path,
        f".opds-desk-write-test-{uuid.uuid4().hex}.tmp",
    )

    try:
        with open(test_file, "w", encoding="utf-8") as f:
            f.write("test")
    except OSError as exc:
        raise ValueError(
            f"Нет доступа на запись в выбранную папку: {exc}"
        ) from exc
    finally:
        try:
            if os.path.exists(test_file):
                os.remove(test_file)
        except OSError:
            pass

    APP_CONFIG["library_path"] = path
    save_app_config(APP_CONFIG)

    DESTINATION = path

    # Health-панель должна сразу проверить новый каталог.
    with health_cache_lock:
        health_cache["time"] = 0.0
        health_cache["data"] = None

    return path
APP_CONFIG = load_app_config()
DESTINATION = APP_CONFIG["library_path"]


def current_source_config():
    """Возвращает сохранённый SourceConfig без изменения APP_CONFIG."""
    config = APP_CONFIG if isinstance(APP_CONFIG, dict) else {}
    return source_config_from_app_config(config)


def current_root_catalog_ref():
    """Строит корневой CatalogRef настроенного источника без сети."""
    source = current_source_config()
    if not source.root_url:
        return None
    return CatalogRef(
        source_id=source.source_id,
        url=source.root_url,
        title=source.display_name or "OPDS",
        kind="navigation",
    )


def register_current_root_catalog():
    """Регистрирует корневой каталог и возвращает его opaque token."""
    ref = current_root_catalog_ref()
    return register_catalog_ref(ref) if ref is not None else None


def current_source_id():
    """Возвращает сохранённый source_id без генерации и изменения config."""
    return current_source_config().source_id


def has_configured_opds_source():
    """Проверяет наличие сохранённого пользовательского OPDS URL."""
    return bool(current_source_config().root_url)


def load_current_opds_feed(client=None):
    """Загружает feed только явно настроенного OPDS-источника."""
    source = current_source_config()
    if not source.root_url:
        raise ValueError("OPDS-источник не настроен")
    http_client = client if client is not None else OPDSHTTPClient()
    return http_client.fetch_feed(
        source.root_url,
        source_id=source.source_id,
    )


def resolve_current_opds_search_descriptor(client=None):
    """Обнаруживает search descriptor явно настроенного OPDS-источника."""
    http_client = client if client is not None else OPDSHTTPClient()
    feed = load_current_opds_feed(client=http_client)
    if feed.search is None:
        raise ValueError("Этот OPDS-источник не предоставляет поиск")
    return resolve_opds_search_descriptor(
        feed.search,
        client=http_client,
    )


def load_current_opds_search_page(
    query,
    page=0,
    force=False,
    client=None,
):
    """Загружает cached search page явно настроенного OPDS-источника."""
    http_client = client if client is not None else OPDSHTTPClient()
    source = current_source_config()
    descriptor = resolve_current_opds_search_descriptor(client=http_client)
    return load_cached_opds_search_page(
        descriptor,
        query,
        source_id=source.source_id,
        page=page,
        force=force,
        client=http_client,
    )


def load_current_opds_catalog_page(page_url=None, client=None):
    """Загружает одну страницу явно настроенного текущего источника."""
    source = current_source_config()
    if not source.root_url:
        raise ValueError("OPDS-источник не настроен")
    target_url = source.root_url if page_url is None else normalize_opds_url(page_url)
    return load_opds_catalog_page(
        target_url,
        source_id=source.source_id,
        client=client,
    )


def validate_user_opds_url(url, client=None):
    """Проверяет пользовательский OPDS URL без изменения конфигурации."""
    return validate_opds_source(url, client=client)


def _save_and_replace_app_config(updated_config):
    """Сохраняет копию config и лишь затем обновляет APP_CONFIG на месте."""
    normalized_config = normalize_app_config(updated_config)
    save_app_config(normalized_config)
    APP_CONFIG.clear()
    APP_CONFIG.update(normalized_config)
    return normalized_config


def save_validated_opds_source(validation_result):
    """Сохраняет только успешно проверенный пользовательский источник."""
    updated_config = apply_validated_source(APP_CONFIG, validation_result)
    return _save_and_replace_app_config(updated_config)


def configure_opds_source(url, client=None):
    """Проверяет источник и сохраняет его только при успешном результате."""
    validation = validate_user_opds_url(url, client=client)
    if validation.valid is not True:
        return validation
    save_validated_opds_source(validation)
    return validation


def clear_configured_opds_source():
    """Очищает только поля OPDS-источника, сохраняя остальной config."""
    updated_config = normalize_app_config(APP_CONFIG)
    updated_config.update(
        opds_url="",
        source_id="",
        source_name="",
    )
    return _save_and_replace_app_config(updated_config)


# ============================================================
# Desktop API / pywebview
# ============================================================

class DesktopApi:
    """Предоставляет JavaScript безопасные операции desktop-окна."""

    def choose_library_folder(self):
        """Открывает системный диалог выбора каталога библиотеки."""
        window = webview.active_window()

        if window is None:
            return {
                "ok": False,
                "message": "Окно приложения недоступно",
            }

        result = window.create_file_dialog(
            webview.FileDialog.FOLDER,
            directory=DESTINATION,
            allow_multiple=False,
        )

        if not result:
            return {
                "ok": False,
                "cancelled": True,
            }

        selected_path = result[0]

        try:
            saved_path = set_library_path(selected_path)

            return {
                "ok": True,
                "path": saved_path,
            }

        except Exception as exc:
            return {
                "ok": False,
                "message": str(exc),
            }

    def complete_setup(self):
        """Отмечает мастер первого запуска как завершённый."""
        try:
            APP_CONFIG["setup_complete"] = True
            save_app_config(APP_CONFIG)

            return {
                "ok": True,
            }

        except Exception as exc:
            return {
                "ok": False,
                "message": str(exc),
            }


desktop_api = DesktopApi()

# ============================================================
# Константы, состояние приложения и шаблоны интерфейса
# ============================================================

TIMEOUT = 45
RETRY_ATTEMPTS = 3
RETRY_DELAY = 3
# Загрузка книг использует отдельный ОДИН цикл повторов.
# Это устраняет прежние вложенные retry: save_* -> legacy_opds_get -> retry.
DOWNLOAD_CONNECT_TIMEOUT = 10
DOWNLOAD_READ_TIMEOUT = 30
DOWNLOAD_RETRY_ATTEMPTS = 3
DOWNLOAD_RETRY_DELAY = 2
LEGACY_QUEUE_SOURCE_ID = "legacy-v1"
BULK_DELAY = 1.0
MAX_DOWNLOAD_SIZE = 100 * 1024 * 1024
MAX_IMAGE_SIZE = 10 * 1024 * 1024
MAX_CATALOG_PAGES = 500
CATALOG_CACHE_TTL = 15 * 60
SEARCH_CACHE_TTL = 30 * 60
MAX_AUTHOR_COMPONENT_BYTES = 180
MAX_TITLE_COMPONENT_BYTES = 220
QUEUE_SCHEDULER_INTERVAL = 20
QUEUE_DEFAULT_TIME = "02:00"
QUEUE_DEFAULT_TZ_OFFSET = "+03:00"
QUEUE_DEFAULT_MIN_FREE_GB = 10
HEALTH_CACHE_TTL = 45

ATOM = "http://www.w3.org/2005/Atom"
DC = "http://purl.org/dc/terms/"
OPDS_ACQUISITION = "http://opds-spec.org/acquisition/open-access"
NS = {"atom": ATOM, "dc": DC}

app = Flask(__name__)
if "OPDS_DESK_SECRET" in os.environ:
    app.secret_key = os.environ["OPDS_DESK_SECRET"]
else:
    app.secret_key = os.environ.get("FLIBUSTA_BRIDGE_SECRET", "booklore-flibusta-local-v20")
app.jinja_env.globals["app_version"] = APP_VERSION

catalog_cache = {}
catalog_lock = threading.Lock()
catalog_page_cache = {}
catalog_page_cache_lock = threading.Lock()
opds_search_page_cache = {}
opds_search_page_cache_lock = threading.Lock()
jobs = {}
jobs_lock = threading.Lock()
queue_db_lock = threading.RLock()
queue_worker_lock = threading.Lock()
queue_worker_thread = None
queue_scheduler_thread = None
download_serial_lock = threading.Lock()
health_cache = {"time": 0.0, "data": None}
health_cache_lock = threading.Lock()

COMMON_CSS = r"""
:root{--bg:#111418;--panel:#191d22;--panel2:#20252b;--border:#30363d;--text:#f0f2f4;--muted:#9aa4ae;--accent:#37d67a;--accent2:#25af61;--blue:#58a6ff;--danger:#e06767}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:Inter,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}.container{width:min(1180px,calc(100% - 32px));margin:0 auto;padding:30px 0 60px}a{color:var(--blue)}button,input{font:inherit}.topbar{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:20px}.nav{display:flex;flex-wrap:wrap;gap:8px}.nav a,.button-link{display:inline-block;padding:8px 11px;border:1px solid var(--border);border-radius:6px;background:var(--panel2);color:var(--text);text-decoration:none}.nav a:hover,.button-link:hover{border-color:var(--blue)}h1{margin:0 0 6px}.subtitle,.muted{color:var(--muted)}.flash{margin:0 0 15px;padding:11px 13px;border:1px solid var(--border);border-left:4px solid var(--accent);border-radius:7px;background:var(--panel)}.badge{display:inline-block;padding:3px 6px;border:1px solid var(--border);border-radius:4px;background:var(--panel2);color:#cbd2d9;font-size:11px}.exists{color:var(--accent);font-weight:650}.danger{color:var(--danger)!important;border-color:var(--danger)!important}.primary{background:var(--accent)!important;color:#07130c!important;border-color:var(--accent)!important;font-weight:700}.card{border:1px solid var(--border);border-radius:9px;background:var(--panel)}
@media(max-width:650px){.container{width:calc(100% - 16px);padding-top:18px}.topbar{align-items:flex-start;flex-direction:column}}
"""

ERROR_HTML = r"""
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{{ title }}</title>
  <style>{{ css|safe }}
  .error-card{max-width:720px;padding:22px}.message{margin:14px 0;color:var(--text)}.actions{margin-top:20px}.actions a{display:inline-block;padding:8px 11px;border:1px solid var(--border);border-radius:6px;background:var(--panel2);color:var(--text);text-decoration:none}
  </style>
</head>
<body>
  <div class="container">
    <div class="error-card card">
      <h1>{{ title }}</h1>
      <div class="message">{{ message }}</div>
      <div class="muted">HTTP {{ status_code }}</div>
      <div class="actions">
        <a href="{{ url_for('index') }}">← На главный экран</a>
      </div>
    </div>
  </div>
</body>
</html>
"""

NEUTRAL_HOME_HTML = r"""
<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Локальный OPDS-клиент</title>
<style>
:root{--bg:#111418;--panel:#191d22;--panel2:#20252b;--line:#30363d;--text:#f0f2f4;--muted:#9aa4ae;--accent:#37d67a;--blue:#58a6ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:system-ui,-apple-system,"Segoe UI",sans-serif}.container{width:min(760px,calc(100% - 32px));margin:0 auto;padding:44px 0 60px}.card{padding:24px;border:1px solid var(--line);border-radius:10px;background:var(--panel)}h1{margin:0 0 9px}.description{color:var(--muted);line-height:1.55}.status,.search-panel{margin-top:20px;padding:13px 15px;border:1px solid var(--line);border-radius:7px;background:var(--panel2)}.status strong,.search-panel strong{display:block}.source-name{margin-top:5px;color:var(--muted)}.search-form{display:flex;gap:9px;margin-top:10px}.search-form input{min-width:0;flex:1;padding:10px 12px;border:1px solid var(--line);border-radius:7px;background:var(--panel);color:var(--text)}.search-form button{padding:10px 15px;border:1px solid var(--accent);border-radius:7px;background:var(--accent);color:#07130c;font-weight:700;cursor:pointer}.search-form button:disabled{opacity:.6;cursor:wait}.actions{display:flex;flex-wrap:wrap;gap:9px;margin-top:20px}.actions a{display:inline-block;padding:10px 13px;border:1px solid var(--line);border-radius:7px;background:var(--panel2);color:var(--text);text-decoration:none}.actions a.primary{border-color:var(--accent);background:var(--accent);color:#07130c;font-weight:700}.opds-loading-overlay{position:fixed;inset:0;z-index:9999;display:none;align-items:center;justify-content:center;background:rgba(10,12,15,.88);cursor:wait}.opds-loading-overlay.visible{display:flex}.opds-loading-card{min-width:min(420px,calc(100% - 32px));padding:24px;border:1px solid var(--line);border-radius:10px;background:var(--panel);text-align:center;box-shadow:0 18px 60px rgba(0,0,0,.45)}.opds-loading-spinner{width:34px;height:34px;margin:0 auto 14px;border:4px solid #384149;border-top-color:var(--accent);border-radius:50%;animation:opds-spin .85s linear infinite}.opds-loading-title{font-size:18px;font-weight:750}.opds-loading-note{margin-top:7px;color:var(--muted);font-size:13px}@keyframes opds-spin{to{transform:rotate(360deg)}}@media(max-width:600px){.container{width:calc(100% - 16px);padding-top:20px}.search-form{flex-direction:column}.search-form button,.actions a{width:100%;text-align:center}}
</style>
</head>
<body><main class="container"><section class="card">
  <h1>Локальный OPDS-клиент</h1>
  <div class="description">Работа с выбранными пользователем OPDS-каталогами и локальной библиотекой.</div>
  <div class="status">
    {% if configured %}
      <strong>OPDS-источник настроен</strong>
      {% if source_name %}<div class="source-name">{{ source_name }}</div>{% endif %}
    {% else %}
      <strong>OPDS-источник не настроен</strong>
    {% endif %}
  </div>
  {% if configured %}
  <section class="search-panel">
    <strong>Поиск по OPDS</strong>
    <form id="opdsSearchForm" class="search-form" method="get" action="{{ url_for('opds_search_page') }}">
      <input type="search" name="q" placeholder="Название книги или автор" aria-label="Поисковый запрос">
      <button id="opdsSearchSubmit" type="submit">Найти</button>
    </form>
  </section>
  {% endif %}
  <div class="actions">
    {% if configured %}<a id="openOpdsCatalogLink" class="primary" href="{{ url_for('open_current_opds_catalog') }}">Открыть OPDS-каталог</a>{% endif %}
    <a href="{{ url_for('opds_settings_page') }}">Настроить OPDS</a>
    <a href="{{ url_for('settings_page') }}">Настройки библиотеки</a>
  </div>
</section></main>
<div id="opdsSearchLoadingOverlay" class="opds-loading-overlay" aria-hidden="true">
  <div class="opds-loading-card" role="status" aria-live="polite">
    <div class="opds-loading-spinner" aria-hidden="true"></div>
    <div id="opdsHomeLoadingTitle" class="opds-loading-title">Поиск книг...</div>
    <div id="opdsHomeLoadingNote" class="opds-loading-note">Получение данных из OPDS-каталога</div>
  </div>
</div>
<script id="opdsSearchLoadingScript">
let opdsSearchLoading = false;
const opdsSearchForm = document.getElementById('opdsSearchForm');
const opdsSearchSubmit = document.getElementById('opdsSearchSubmit');
const openOpdsCatalogLink = document.getElementById('openOpdsCatalogLink');

function beginOpdsHomeLoading(event, title, note) {
  if (opdsSearchLoading) {
    event.preventDefault();
    return;
  }
  opdsSearchLoading = true;
  const overlay = document.getElementById('opdsSearchLoadingOverlay');
  document.getElementById('opdsHomeLoadingTitle').textContent = title;
  document.getElementById('opdsHomeLoadingNote').textContent = note;
  overlay.classList.add('visible');
  overlay.setAttribute('aria-hidden', 'false');
  if (opdsSearchSubmit) opdsSearchSubmit.disabled = true;
}

function showOpdsSearchLoading(event) {
  beginOpdsHomeLoading(
    event,
    'Поиск книг...',
    'Получение данных из OPDS-каталога'
  );
}

function showOpenOpdsCatalogLoading(event) {
  beginOpdsHomeLoading(
    event,
    'Загрузка каталога...',
    'Получение данных из OPDS-каталога'
  );
}

function resetOpdsSearchLoading() {
  opdsSearchLoading = false;
  const overlay = document.getElementById('opdsSearchLoadingOverlay');
  overlay.classList.remove('visible');
  overlay.setAttribute('aria-hidden', 'true');
  if (opdsSearchSubmit) opdsSearchSubmit.disabled = false;
}

if (opdsSearchForm) {
  opdsSearchForm.addEventListener('submit', showOpdsSearchLoading);
}
if (openOpdsCatalogLink) {
  openOpdsCatalogLink.addEventListener('click', showOpenOpdsCatalogLoading);
}
window.addEventListener('pageshow', resetOpdsSearchLoading);
</script>
</body>
</html>
"""

SETUP_HTML = r"""
<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">

<title>Первоначальная настройка</title>

<style>
{{ css|safe }}

.setup-wrap{
    max-width:760px;
    margin:45px auto;
}

.setup-card{
    padding:24px;
}

.setup-title{
    font-size:28px;
    font-weight:800;
    margin-bottom:8px;
}

.setup-section{
    margin-top:22px;
    padding-top:20px;
    border-top:1px solid var(--border);
}

.path-box{
    margin-top:10px;
    padding:12px 14px;
    border:1px solid var(--border);
    border-radius:7px;
    background:var(--panel2);
    word-break:break-all;
    font-family:Consolas,monospace;
}

.setup-actions{
    display:flex;
    flex-wrap:wrap;
    gap:10px;
    margin-top:14px;
}

.setup-actions button{
    padding:10px 14px;
    border:1px solid var(--border);
    border-radius:7px;
    background:var(--panel2);
    color:var(--text);
    cursor:pointer;
}

.setup-actions button:disabled{
    opacity:.45;
    cursor:not-allowed;
}

.finish-button{
    background:var(--accent)!important;
    border-color:var(--accent)!important;
    color:#07130c!important;
    font-weight:750;
}

.status{
    margin-top:12px;
    color:var(--muted);
}

.status.ok{
    color:var(--accent);
}

.status.error{
    color:var(--danger);
}
</style>
</head>

<body>

<div class="container">
<div class="setup-wrap">
<div class="setup-card card">

<div class="setup-title">
    Настройка приложения
</div>

<div class="muted">
    Первоначальная настройка программы
</div>

<div class="setup-section">

    <strong>1. О приложении</strong>

    <div class="muted" style="margin-top:7px">
        Приложение позволяет работать с OPDS-каталогами и локальной
        библиотекой. OPDS-источник можно настроить после первоначальной
        настройки.
    </div>

</div>


<div class="setup-section">

    <strong>2. Папка библиотеки</strong>

    <div class="muted" style="margin-top:7px">
        Все загруженные книги будут сохраняться в выбранный каталог.
    </div>

    <div
        class="path-box"
        id="currentLibraryPath"
    >
        {{ destination }}
    </div>

    <div class="setup-actions">
        <button
            type="button"
            id="chooseLibraryButton"
        >
            Выбрать папку…
        </button>
    </div>

    <div
        class="status"
        id="libraryStatus"
    >
        Текущий каталог можно изменить.
    </div>

</div>


<div class="setup-section">

    <strong>3. Завершение настройки</strong>

    <div class="setup-actions">
        <button
            class="finish-button"
            type="button"
            id="finishSetupButton"
            disabled
        >
            Завершить настройку
        </button>
    </div>

    <div
        class="status"
        id="finishStatus"
    >
        Сначала выберите папку локальной библиотеки.
    </div>

</div>

</div>
</div>
</div>


<script>

const chooseButton =
    document.getElementById('chooseLibraryButton');

const pathBox =
    document.getElementById('currentLibraryPath');

const libraryStatus =
    document.getElementById('libraryStatus');

const finishButton =
    document.getElementById('finishSetupButton');

const finishStatus =
    document.getElementById('finishStatus');


let librarySelected = false;


chooseButton.addEventListener('click', async () => {

    if (
        !window.pywebview ||
        !window.pywebview.api ||
        !window.pywebview.api.choose_library_folder
    ) {

        libraryStatus.className =
            'status error';

        libraryStatus.textContent =
            'Выбор папки доступен только в desktop-версии.';

        return;
    }

    chooseButton.disabled = true;
    chooseButton.textContent =
        'Выбор папки…';

    try {

        const result =
            await window.pywebview.api.choose_library_folder();

        if (result && result.ok) {

            librarySelected = true;
            finishButton.disabled = false;

            pathBox.textContent =
                result.path;

            libraryStatus.className =
                'status ok';

            libraryStatus.textContent =
                'Папка библиотеки выбрана.';

            finishStatus.className =
                'status ok';

            finishStatus.textContent =
                'Локальная библиотека выбрана. OPDS-источник необязателен на этом этапе и может быть настроен на следующем экране.';

        } else if (result && result.cancelled) {

            finishButton.disabled = !librarySelected;

            libraryStatus.className =
                'status';

            libraryStatus.textContent =
                'Выбор папки отменён.';

        } else {

            finishButton.disabled = !librarySelected;

            libraryStatus.className =
                'status error';

            libraryStatus.textContent =
                'Ошибка: ' +
                (
                    result &&
                    result.message
                        ? result.message
                        : 'не удалось изменить библиотеку'
                );

        }

    } catch (error) {

        finishButton.disabled = !librarySelected;

        libraryStatus.className =
            'status error';

        libraryStatus.textContent =
            'Ошибка при выборе папки: ' +
            error;

    } finally {

        chooseButton.disabled = false;
        chooseButton.textContent =
            'Выбрать папку…';

    }

});


finishButton.addEventListener('click', async () => {

    if (!librarySelected) {
        return;
    }

    if (
        !window.pywebview ||
        !window.pywebview.api ||
        !window.pywebview.api.complete_setup
    ) {

        finishStatus.className =
            'status error';

        finishStatus.textContent =
            'Не удалось завершить настройку приложения.';

        return;
    }

    finishButton.disabled = true;
    finishButton.textContent =
        'Сохранение…';

    try {

        const result =
            await window.pywebview.api.complete_setup();

        if (result && result.ok) {

            finishStatus.className =
                'status ok';

            finishStatus.textContent =
                'Настройка завершена.';

            window.location.href = '/settings/opds';

        } else {

            finishStatus.className =
                'status error';

            finishStatus.textContent =
                'Ошибка: ' +
                (
                    result &&
                    result.message
                        ? result.message
                        : 'не удалось сохранить настройки'
                );

            finishButton.disabled = false;
            finishButton.textContent =
                'Завершить настройку';

        }

    } catch (error) {

        finishStatus.className =
            'status error';

        finishStatus.textContent =
            'Ошибка при завершении настройки: ' +
            error;

        finishButton.disabled = false;
        finishButton.textContent =
            'Завершить настройку';

    }

});

</script>

</body>
</html>
"""
SETTINGS_HTML = r"""
<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">

<title>Настройки — OPDS Desk</title>

<style>
{{ css|safe }}

.settings-card{
    padding:20px;
    max-width:850px;
}

.setting-title{
    font-size:18px;
    font-weight:750;
    margin-bottom:8px;
}

.path-box{
    margin-top:10px;
    padding:12px 14px;
    border:1px solid var(--border);
    border-radius:7px;
    background:var(--panel2);
    word-break:break-all;
    font-family:Consolas,monospace;
}

.setting-note{
    margin-top:9px;
    color:var(--muted);
    font-size:13px;
    line-height:1.5;
}
</style>
</head>

<body>

<div class="container">

<div class="topbar">

    <div>
        <h1>Настройки</h1>
        <div class="subtitle">
            OPDS Desk {{ app_version }}
        </div>
    </div>

    <div class="nav">
        <a href="{{ url_for('opds_settings_page') }}">
            Настройка OPDS
        </a>
        <a href="{{ url_for('index') }}">
            ← На главный экран
        </a>
    </div>

</div>


<div class="settings-card card">

    <div class="setting-title">
        Библиотека
    </div>

    <div class="muted">
        Все загруженные книги сохраняются в этот каталог:
    </div>

    <div
        class="path-box"
        id="currentLibraryPath"
    >
        {{ destination }}
    </div>

    <div style="margin-top:12px">

        <button
            class="button-link primary"
            type="button"
            id="chooseLibraryButton"
        >
            Выбрать папку…
        </button>

    </div>

    <div
        class="setting-note"
        id="libraryMessage"
    >
        Путь сохраняется в настройках программы и будет использоваться
        после следующих запусков OPDS Desk.
    </div>

</div>

</div>


<script>

const chooseButton =
    document.getElementById('chooseLibraryButton');

const pathBox =
    document.getElementById('currentLibraryPath');

const messageBox =
    document.getElementById('libraryMessage');


chooseButton.addEventListener('click', async () => {

    if (
        !window.pywebview ||
        !window.pywebview.api ||
        !window.pywebview.api.choose_library_folder
    ) {

        messageBox.textContent =
            'Выбор папки доступен только в desktop-версии OPDS Desk.';

        return;
    }

    chooseButton.disabled = true;
    chooseButton.textContent =
        'Выбор папки…';

    try {

        const result =
            await window.pywebview.api.choose_library_folder();

        if (result && result.ok) {

            pathBox.textContent =
                result.path;

            messageBox.textContent =
                'Библиотека изменена. Новые книги будут сохраняться в выбранную папку.';

        } else if (result && result.cancelled) {

            messageBox.textContent =
                'Выбор папки отменён.';

        } else {

            messageBox.textContent =
                'Ошибка: ' +
                (
                    result &&
                    result.message
                        ? result.message
                        : 'не удалось изменить библиотеку'
                );

        }

    } catch (error) {

        messageBox.textContent =
            'Ошибка при выборе папки: ' +
            error;

    } finally {

        chooseButton.disabled = false;
        chooseButton.textContent =
            'Выбрать папку…';

    }

});

</script>

</body>
</html>
"""

OPDS_SETTINGS_HTML = r"""
<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Настройка OPDS</title>
<style>
:root{--bg:#111418;--panel:#191d22;--panel2:#20252b;--border:#30363d;--text:#f0f2f4;--muted:#9aa4ae;--accent:#37d67a;--blue:#58a6ff;--danger:#e06767}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:system-ui,-apple-system,"Segoe UI",sans-serif}.container{width:min(760px,calc(100% - 32px));margin:0 auto;padding:32px 0 60px}.card{padding:20px;border:1px solid var(--border);border-radius:9px;background:var(--panel)}h1{margin:0 0 8px}.muted{color:var(--muted);line-height:1.5}.field{margin-top:18px}label{display:block;margin-bottom:7px;font-weight:700}input[type=url]{width:100%;padding:11px 12px;border:1px solid var(--border);border-radius:7px;background:var(--panel2);color:var(--text)}.actions{display:flex;flex-wrap:wrap;gap:9px;margin-top:14px}button,.button-link{display:inline-block;padding:9px 12px;border:1px solid var(--border);border-radius:6px;background:var(--panel2);color:var(--text);text-decoration:none;cursor:pointer}.primary{border-color:var(--accent);background:var(--accent);color:#07130c;font-weight:700}.danger{border-color:var(--danger);color:#ffb1b1}.message,.error,.source{margin-top:16px;padding:12px 14px;border:1px solid var(--border);border-radius:7px;background:var(--panel2)}.message{border-left:4px solid var(--accent)}.error{border-left:4px solid var(--danger)}.source-name{font-weight:700}.source-url{margin-top:6px;color:var(--muted);overflow-wrap:anywhere}@media(max-width:600px){.container{width:calc(100% - 16px);padding-top:18px}}
</style>
</head>
<body><main class="container">
  <h1>Настройка OPDS</h1>
  <p class="muted">Укажите адрес OPDS-каталога, который вы хотите использовать. Доступность и права на использование содержимого каталога определяются его владельцем и применимыми условиями.</p>
  {% if message %}<div class="message">{{ message }}</div>{% endif %}
  {% if error %}<div class="error">{{ error }}</div>{% endif %}
  <section class="card">
    <form method="post" action="{{ url_for('opds_settings_page') }}">
      <input type="hidden" name="action" value="save">
      <div class="field">
        <label for="opdsUrl">Адрес OPDS-каталога</label>
        <input id="opdsUrl" type="url" name="opds_url" value="{{ opds_url }}" autocomplete="url">
      </div>
      <div class="actions"><button class="primary" type="submit">Проверить и сохранить</button></div>
    </form>
    {% if configured %}
      <div class="source">
        <div class="source-name">{{ source_name or 'OPDS-источник' }}</div>
        <div class="source-url">{{ opds_url }}</div>
        <div class="actions">
          <a class="button-link" href="{{ url_for('open_current_opds_catalog') }}">Открыть OPDS-каталог</a>
          <form method="post" action="{{ url_for('opds_settings_page') }}">
            <input type="hidden" name="action" value="clear">
            <button class="danger" type="submit">Удалить источник</button>
          </form>
        </div>
      </div>
    {% endif %}
  </section>
  <div class="actions">
    <a class="button-link" href="{{ url_for('index') }}">← На главный экран</a>
    <a class="button-link" href="{{ url_for('settings_page') }}">← Назад в настройки</a>
  </div>
</main></body>
</html>
"""

CATALOG_HTML = r"""
<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{{ title }}</title><style>{{ css|safe }}
.summary{color:var(--muted);margin:8px 0 18px}.toolbar{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:18px}.toolbar button{padding:8px 11px;border:1px solid var(--border);border-radius:6px;background:var(--panel2);color:var(--text);cursor:pointer}.options{padding:15px;margin-bottom:12px}.options label{display:block;margin:7px 0}.duplicate-option{margin-top:14px!important;padding-top:13px;border-top:1px solid var(--border)}.duplicate-option input{width:18px;height:18px;vertical-align:-3px;margin-right:7px}.option-note{display:block;margin:5px 0 0 28px;color:var(--muted);font-size:12px;line-height:1.4}.selection-summary{display:flex;flex-wrap:wrap;gap:18px;padding:13px 15px;margin-bottom:18px;color:var(--muted)}.selection-summary strong{color:var(--text);font-size:17px}.selection-summary .queue strong{color:var(--accent)}.book{display:grid;grid-template-columns:32px 72px 1fr;gap:12px;align-items:start;padding:12px;margin-bottom:9px}.check{padding-top:8px}.check input{width:18px;height:18px}.cover{width:72px;height:106px;border-radius:5px;overflow:hidden;background:var(--panel2);display:flex;align-items:center;justify-content:center;color:var(--muted);font-size:11px;text-align:center}.cover img{width:100%;height:100%;object-fit:cover}.title{font-size:17px;font-weight:700;margin-bottom:4px}.author{color:#d2d7dc;margin-bottom:7px}.meta{display:flex;flex-wrap:wrap;gap:5px;margin-bottom:7px}.start{position:sticky;bottom:12px;margin-top:20px;padding:12px;background:rgba(25,29,34,.97);display:grid;grid-template-columns:1fr 1fr;gap:10px}.start button{width:100%;padding:13px;border:0;border-radius:7px;font-weight:750;cursor:pointer}.start .now{background:var(--panel2);color:var(--text);border:1px solid var(--border)}.start .queue-btn{background:var(--accent);color:#07130c}.start .all-books{grid-column:1/-1;background:#d9a441;color:#1b1203}.dup-info{margin-top:6px;display:flex;flex-wrap:wrap;gap:6px;align-items:center}.preferred-badge{border-color:#2d8b55;color:#76e5a7}.secondary-dup{color:var(--muted)}.series-panel{padding:14px 15px;margin-bottom:14px}.series-panel-title{font-weight:750;margin-bottom:9px}.series-list{display:flex;flex-wrap:wrap;gap:7px}.series-filter{padding:7px 10px;border:1px solid var(--border);border-radius:6px;background:var(--panel2);color:var(--text);cursor:pointer}.series-filter.active{border-color:var(--accent);color:var(--accent)}.series-open{display:inline-block;padding:7px 9px;border:1px solid var(--border);border-radius:6px;text-decoration:none;background:var(--panel2);color:var(--blue)}.series-group{display:flex;gap:4px}.series-note{margin-top:9px;color:var(--muted);font-size:12px}.book-series{display:flex;flex-wrap:wrap;gap:5px;margin:6px 0}.book-series a{text-decoration:none}.series-visible{margin-top:8px;color:var(--muted);font-size:12px}.page-loading-overlay{position:fixed;inset:0;z-index:1000;display:none;align-items:center;justify-content:center;background:rgba(8,11,14,.82);backdrop-filter:blur(2px)}.page-loading-overlay.visible{display:flex}.page-loading-card{min-width:260px;padding:28px 32px;border:1px solid var(--border);border-radius:10px;background:var(--panel);text-align:center;box-shadow:0 18px 60px rgba(0,0,0,.45)}.page-loading-spinner{width:34px;height:34px;margin:0 auto 15px;border:4px solid var(--border);border-top-color:var(--accent);border-radius:50%;animation:page-loading-spin .8s linear infinite}.page-loading-title{font-size:19px;font-weight:750}.page-loading-note{margin-top:7px;color:var(--muted);font-size:13px}.catalog-page-link.loading-disabled,.catalog-full-view-link.loading-disabled{pointer-events:none;opacity:.55}@keyframes page-loading-spin{to{transform:rotate(360deg)}}@media(max-width:650px){.book{grid-template-columns:28px 55px 1fr}.cover{width:55px;height:82px}.selection-summary{gap:10px 18px}.start{grid-template-columns:1fr}}
</style></head><body><div class="container">
<div class="topbar"><div><div class="nav"><a href="{{ url_for('index') }}">← На главный экран</a><a href="{{ url_for('search_return') }}">← Вернуться к поиску</a><a href="{{ url_for('queue_page') }}">Очередь{% if queue_pending_count %} ({{ queue_pending_count }}){% endif %}</a><a href="{{ url_for('queue_history') }}">История</a><a href="{{ url_for('notifications_page') }}">Уведомления{% if unread_notifications %} ({{ unread_notifications }}){% endif %}</a><a href="{{ url_for('jobs_page') }}">Загрузки</a></div><h1 style="margin-top:18px">{{ title }}</h1><div class="summary">{% if view_all %}Найдено{% else %}На странице{% endif %}: <strong>{{ total }}</strong> · Уже в библиотеке: <strong>{{ existing }}</strong> · Не найдено локально: <strong>{{ total-existing }}</strong>{% if duplicate_groups %} · Групп дублей: <strong>{{ duplicate_groups }}</strong> · Альтернативных изданий: <strong>{{ duplicate_extra }}</strong>{% endif %}</div></div></div>
{% with messages=get_flashed_messages() %}{% for message in messages %}<div class="flash">{{ message }}</div>{% endfor %}{% endwith %}
<div class="card" style="padding:12px 14px;margin:0 0 16px;display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;">
  {% if view_all %}
  <div class="muted">Полный каталог</div>
  <div style="display:flex;gap:8px;flex-wrap:wrap;">
    {% if kind == 'author' %}
      <a class="button-link" href="{{ url_for('author_catalog', author_id=catalog_id, name=name, page=0) }}">Постраничный режим</a>
    {% else %}
      <a class="button-link" href="{{ url_for('series_catalog', series_id=catalog_id, name=name, page=0) }}">Постраничный режим</a>
    {% endif %}
  </div>
  {% else %}
  <div class="muted">Страница {{ page + 1 }}</div>
  <div style="display:flex;gap:8px;flex-wrap:wrap;">
    {% if kind == 'author' %}
      {% if page > 0 %}<a class="button-link catalog-page-link" href="{{ url_for('author_catalog', author_id=catalog_id, name=name, page=page-1) }}">← Назад</a>{% endif %}
      {% if has_next %}<a class="button-link catalog-page-link" href="{{ url_for('author_catalog', author_id=catalog_id, name=name, page=page+1) }}">Далее →</a>{% endif %}
      <a class="button-link catalog-full-view-link" href="{{ url_for('author_catalog', author_id=catalog_id, name=name, view='all') }}">Все книги одним списком</a>
    {% else %}
      {% if page > 0 %}<a class="button-link catalog-page-link" href="{{ url_for('series_catalog', series_id=catalog_id, name=name, page=page-1) }}">← Назад</a>{% endif %}
      {% if has_next %}<a class="button-link catalog-page-link" href="{{ url_for('series_catalog', series_id=catalog_id, name=name, page=page+1) }}">Далее →</a>{% endif %}
      <a class="button-link catalog-full-view-link" href="{{ url_for('series_catalog', series_id=catalog_id, name=name, view='all') }}">Все книги одним списком</a>
    {% endif %}
  </div>
  {% endif %}
</div>
{% if kind == 'author' and series_groups %}
<div class="series-panel card">
  <div class="series-panel-title">{% if view_all %}Серии автора{% else %}Серии на этой странице{% endif %}</div>
  <div class="series-list">
    <button class="series-filter active" type="button" data-filter="all" onclick="setSeriesFilter('all',this)">{% if view_all %}Все книги{% else %}Все на странице{% endif %} ({{ total }})</button>
    {% for s in series_groups %}
      <div class="series-group"><button class="series-filter" type="button" data-filter="{{ s.id }}" onclick="setSeriesFilter('{{ s.id }}',this)">{{ s.name }} ({{ s.count }})</button><a class="series-open" href="{{ url_for('series_catalog',series_id=s.id,name=s.name) }}" title="Открыть отдельную страницу серии">↗</a></div>
    {% endfor %}
    {% if no_series_count %}<button class="series-filter" type="button" data-filter="none" onclick="setSeriesFilter('none',this)">Вне серий ({{ no_series_count }})</button>{% endif %}
  </div>
  <div class="series-visible">Показано книг: <strong id="visibleBookCount">{{ total }}</strong>. Фильтр ничего не выбирает автоматически.</div>
</div>
{% endif %}
<form method="post" action="{{ url_for('bulk_start') }}" id="bulkForm"><input type="hidden" name="kind" value="{{ kind }}"><input type="hidden" name="catalog_id" value="{{ catalog_id }}"><input type="hidden" name="catalog_name" value="{{ title }}"><input type="hidden" name="origin_page" value="{{ page }}"><input type="hidden" name="origin_view" value="{{ 'all' if view_all else '' }}">
<div class="toolbar"><button type="button" onclick="selectAll()">Выбрать все</button><button type="button" onclick="selectNone()">Снять все</button><button type="button" onclick="excludeSuspicious()">Исключить компиляции/фейки</button><button type="button" onclick="selectMissing()">Только отсутствующие</button>{% if kind == 'author' and series_groups %}<button type="button" onclick="selectVisibleMissing()">Отсутствующие в выбранной серии</button>{% endif %}</div>
<div class="options card"><strong>Формат</strong><label><input type="radio" name="format_mode" value="auto" checked> EPUB, если его нет — FB2</label><label><input type="radio" name="format_mode" value="epub"> Только EPUB</label><label><input type="radio" name="format_mode" value="fb2"> Только FB2</label><label class="duplicate-option"><input type="checkbox" name="download_duplicates" value="1" id="downloadDuplicates"> <strong>Скачивать дубли</strong><span class="option-note">Выключено: из каждой группы берётся только предпочтительное издание. Включено: все отмеченные издания сохраняются отдельно с ID записи источника при совпадении имён.</span></label></div>
<div class="selection-summary card"><span>Выбрано: <strong id="selectedCount">0</strong></span><span style="flex-basis:100%;font-size:12px">Выбор сохраняется при переходах между страницами этого каталога. Точное количество определяется после проверки библиотеки и дублей.</span></div>
{% for book in books %}<div class="book card" data-title="{{ book.title|lower }}" data-existing="{{ '1' if book.exists_any else '0' }}" data-duplicate-existing="{{ '1' if book.duplicate_exists_any else '0' }}" data-preferred="{{ '1' if book.duplicate_preferred else '0' }}" data-group="{{ book.duplicate_group }}" data-series="{{ book.series_ids }}"><div class="check"><input class="book-check" type="checkbox" name="book_id" value="{{ book.id }}" {% if book.id in queued_ids %}disabled{% endif %}></div><div class="cover">{% if book.cover_href %}<img src="{{ url_for('image_proxy',href=book.cover_href) }}" loading="lazy" alt="">{% else %}Нет<br>обложки{% endif %}</div><div><div class="title">{{ book.title }}</div><div class="author">{{ book.author }}</div><div class="meta">{% if book.language %}<span class="badge">{{ book.language|upper }}</span>{% endif %}{% if book.epub %}<span class="badge">EPUB</span>{% endif %}{% if book.fb2 %}<span class="badge">FB2</span>{% endif %}{% for genre in book.genres[:3] %}<span class="badge">{{ genre }}</span>{% endfor %}</div>{% if book.series_links %}<div class="book-series">{% for s in book.series_links %}<a class="badge" href="{{ url_for('series_catalog',series_id=s.id,name=s.name) }}">Серия: {{ s.name }}</a>{% endfor %}</div>{% endif %}{% if book.exists_any %}<div class="exists">Уже есть в библиотеке</div>{% endif %}{% if book.id in queued_ids %}<div class="exists" style="color:var(--blue)">Уже в очереди</div>{% endif %}{% if book.duplicate_count > 1 %}<div class="dup-info">{% if book.duplicate_preferred %}<span class="badge preferred-badge">Дубль ×{{ book.duplicate_count }} · предпочтительное издание</span>{% else %}<span class="badge secondary-dup">Дубль ×{{ book.duplicate_count }} · альтернативное издание</span>{% endif %}{% if book.size %}<span class="badge">{{ book.size }}</span>{% endif %}{% if book.downloads %}<span class="badge">Скачиваний: {{ book.downloads }}</span>{% endif %}</div>{% endif %}</div></div>{% endfor %}
<div class="start card"><button class="now" type="submit" formaction="{{ url_for('bulk_start') }}">Скачать выбранные сейчас</button><button class="queue-btn" type="submit" data-queue-submit="1" formaction="{{ url_for('queue_add_bulk') }}">Добавить выбранные в очередь</button><button class="all-books" type="submit" name="all_books" value="1" data-all-books-submit="1" formaction="{{ url_for('bulk_start') }}">{% if kind == 'author' %}Скачать все книги автора{% else %}Скачать всю серию{% endif %}</button></div></form></div>
<div id="pageLoadingOverlay" class="page-loading-overlay" aria-hidden="true"><div class="page-loading-card" role="status" aria-live="polite"><div class="page-loading-spinner"></div><div id="pageLoadingTitle" class="page-loading-title">Загрузка страницы...</div><div id="pageLoadingNote" class="page-loading-note">Получение данных из OPDS-каталога</div></div></div>
<script>
const bulkForm=document.getElementById('bulkForm');
const selectionStorageKey={{ selection_storage_key|tojson }};
const rows=()=>Array.from(document.querySelectorAll('.book'));
const allowDuplicates=()=>document.getElementById('downloadDuplicates').checked;

function loadStoredSelection(){
  try{
    const value=JSON.parse(sessionStorage.getItem(selectionStorageKey)||'[]');
    return new Set(Array.isArray(value)?value.map(String):[]);
  }catch(error){
    return new Set();
  }
}

let selectedBookIds=loadStoredSelection();

function saveStoredSelection(){
  try{sessionStorage.setItem(selectionStorageKey,JSON.stringify(Array.from(selectedBookIds)))}catch(error){}
}

function rowMissing(r){return (allowDuplicates()?r.dataset.duplicateExisting:r.dataset.existing)!=='1'}
function rowAllowed(r){return allowDuplicates()||r.dataset.preferred==='1'}
function rowVisible(r){return r.style.display!=='none'}
function visibleRows(){return rows().filter(rowVisible)}
function checkedRows(){return rows().filter(r=>r.querySelector('.book-check').checked)}
function canCheck(r){const cb=r.querySelector('.book-check');return Boolean(cb&&!cb.disabled)}
function canStoreRow(r){return canCheck(r)&&r.dataset.existing!=='1'}
function effectiveRows(){const checked=checkedRows();if(allowDuplicates())return checked;const byGroup=new Map();checked.forEach(r=>{if(!byGroup.has(r.dataset.group)){const p=rows().find(x=>x.dataset.group===r.dataset.group&&x.dataset.preferred==='1');byGroup.set(r.dataset.group,p||r)}});return Array.from(byGroup.values())}

function updateSelectionSummary(){
  document.getElementById('selectedCount').textContent=selectedBookIds.size;
  const vc=document.getElementById('visibleBookCount');
  if(vc)vc.textContent=visibleRows().length;
}

function syncCurrentPageSelection(){
  rows().forEach(r=>{
    const cb=r.querySelector('.book-check');
    const id=String(cb.value);
    if(!canCheck(r)){
      cb.checked=false;
      selectedBookIds.delete(id);
    }else if(cb.checked&&canStoreRow(r))selectedBookIds.add(id);
    else selectedBookIds.delete(id);
  });
  saveStoredSelection();
  updateSelectionSummary();
}

function restoreStoredSelection(){
  rows().forEach(r=>{
    const cb=r.querySelector('.book-check');
    const id=String(cb.value);
    if(!canStoreRow(r)){
      cb.checked=false;
      selectedBookIds.delete(id);
    }else cb.checked=selectedBookIds.has(id);
  });
  saveStoredSelection();
  updateSelectionSummary();
}

function checkboxChanged(event){
  const cb=event.currentTarget;
  const r=cb.closest('.book');
  const id=String(cb.value);
  if(!canCheck(r)){
    cb.checked=false;
    selectedBookIds.delete(id);
  }else if(cb.checked&&canStoreRow(r))selectedBookIds.add(id);
  else selectedBookIds.delete(id);
  saveStoredSelection();
  updateSelectionSummary();
}

function selectAll(){rows().forEach(r=>{const cb=r.querySelector('.book-check');cb.checked=canCheck(r)&&rowAllowed(r)});syncCurrentPageSelection()}
function selectNone(){selectedBookIds.clear();document.querySelectorAll('.book-check').forEach(cb=>cb.checked=false);saveStoredSelection();updateSelectionSummary()}
function selectMissing(){rows().forEach(r=>{const cb=r.querySelector('.book-check');cb.checked=canStoreRow(r)&&rowAllowed(r)&&rowMissing(r)});syncCurrentPageSelection()}
function selectVisibleMissing(){visibleRows().forEach(r=>{const cb=r.querySelector('.book-check');cb.checked=canStoreRow(r)&&rowAllowed(r)&&rowMissing(r)});syncCurrentPageSelection()}
function excludeSuspicious(){const bad=['компиляц','фейк'];rows().forEach(r=>{if(bad.some(w=>r.dataset.title.includes(w)))r.querySelector('.book-check').checked=false});syncCurrentPageSelection()}
function setSeriesFilter(value,button){rows().forEach(r=>{const ids=(r.dataset.series||'').split(',').filter(Boolean);const show=value==='all'||(value==='none'&&ids.length===0)||(value!=='none'&&ids.includes(value));r.style.display=show?'':'none'});document.querySelectorAll('.series-filter').forEach(b=>b.classList.remove('active'));if(button)button.classList.add('active');updateSelectionSummary()}
function duplicateModeChanged(){if(!allowDuplicates()){rows().forEach(r=>{const cb=r.querySelector('.book-check');if(cb&&r.dataset.preferred!=='1')cb.checked=false})}syncCurrentPageSelection()}

function appendStoredSelectionToForm(){
  syncCurrentPageSelection();
  bulkForm.querySelectorAll('.stored-selection-input').forEach(input=>input.remove());
  const checkedIds=new Set(Array.from(bulkForm.querySelectorAll('.book-check:checked')).map(cb=>String(cb.value)));
  selectedBookIds.forEach(id=>{
    if(checkedIds.has(id))return;
    const input=document.createElement('input');
    input.type='hidden';
    input.name='book_id';
    input.value=id;
    input.className='stored-selection-input';
    bulkForm.appendChild(input);
  });
}

let pageLoading=false;

function beginPageLoading(event,title,note){
  if(pageLoading){
    event.preventDefault();
    return false;
  }
  pageLoading=true;
  const overlay=document.getElementById('pageLoadingOverlay');
  document.getElementById('pageLoadingTitle').textContent=title;
  document.getElementById('pageLoadingNote').textContent=note;
  overlay.classList.add('visible');
  overlay.setAttribute('aria-hidden','false');
  document.querySelectorAll('.catalog-page-link,.catalog-full-view-link').forEach(link=>{
    link.classList.add('loading-disabled');
    link.setAttribute('aria-disabled','true');
  });
  return true;
}

function showPageLoading(event){
  beginPageLoading(event,'Загрузка страницы...','Получение данных из OPDS-каталога');
}

function showFullCatalogLoading(event){
  beginPageLoading(event,'Загрузка полного каталога...','Для большого автора это может занять некоторое время');
}

function prepareBulkSubmit(event){
  const allBooks=event.submitter&&event.submitter.dataset.allBooksSubmit==='1';
  const queueSubmit=event.submitter&&event.submitter.dataset.queueSubmit==='1';
  if(!allBooks){
    appendStoredSelectionToForm();
    if(queueSubmit){
      beginPageLoading(event,'Добавление в очередь...','Обновляем каталог и состояние очереди');
    }
    return;
  }
  if(!beginPageLoading(event,'Подготовка полного каталога...','Проверяем книги, дубли и локальную библиотеку'))return;
  bulkForm.querySelectorAll('.stored-selection-input').forEach(input=>input.remove());
  document.querySelectorAll('.book-check').forEach(cb=>cb.removeAttribute('name'));
}

document.getElementById('downloadDuplicates').addEventListener('change',duplicateModeChanged);
document.querySelectorAll('.book-check').forEach(cb=>cb.addEventListener('change',checkboxChanged));
bulkForm.addEventListener('submit',prepareBulkSubmit);
document.querySelectorAll('.catalog-page-link').forEach(link=>link.addEventListener('click',showPageLoading));
document.querySelectorAll('.catalog-full-view-link').forEach(link=>link.addEventListener('click',showFullCatalogLoading));
if({{ clear_selection|tojson }}){
  selectedBookIds.clear();
  saveStoredSelection();
}
restoreStoredSelection();
</script></body></html>
"""

REGISTERED_CATALOG_HTML = r"""
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{% if view %}{{ view.title }}{% else %}OPDS-каталог{% endif %}</title>
  <style>
    :root{--bg:#111418;--panel:#191d22;--panel2:#20252b;--border:#30363d;--text:#f0f2f4;--muted:#9aa4ae;--accent:#58a6ff}
    *{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:system-ui,-apple-system,"Segoe UI",sans-serif}
    .container{width:min(980px,calc(100% - 32px));margin:0 auto;padding:30px 0 60px}h1{margin:0 0 8px}.muted{color:var(--muted)}
    .book,.message,.navigation{margin-top:12px;padding:15px;border:1px solid var(--border);border-radius:9px;background:var(--panel)}
    .book-title{font-size:17px;font-weight:700}.author{margin-top:4px;color:#d2d7dc}.meta{display:flex;flex-wrap:wrap;gap:6px;margin-top:9px}
    .badge{padding:3px 7px;border:1px solid var(--border);border-radius:5px;background:var(--panel2);font-size:12px}.actions{display:flex;flex-wrap:wrap;gap:8px;margin:18px 0}
    .actions a,.navigation a{display:inline-block;padding:8px 11px;border:1px solid var(--border);border-radius:6px;background:var(--panel2);color:var(--accent);text-decoration:none}
    .navigation-links{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}.registered-catalog-loading-link.loading-disabled{pointer-events:none;opacity:.55}.registered-catalog-loading-overlay{position:fixed;inset:0;z-index:9999;display:none;align-items:center;justify-content:center;background:rgba(10,12,15,.88);cursor:wait}.registered-catalog-loading-overlay.visible{display:flex}.registered-catalog-loading-card{min-width:min(420px,calc(100% - 32px));padding:24px;border:1px solid var(--border);border-radius:10px;background:var(--panel);text-align:center;box-shadow:0 18px 60px rgba(0,0,0,.45)}.registered-catalog-loading-spinner{width:34px;height:34px;margin:0 auto 14px;border:4px solid #384149;border-top-color:var(--accent);border-radius:50%;animation:registered-catalog-spin .85s linear infinite}.registered-catalog-loading-title{font-size:18px;font-weight:750}.registered-catalog-loading-note{margin-top:7px;color:var(--muted);font-size:13px}@keyframes registered-catalog-spin{to{transform:rotate(360deg)}}@media(max-width:650px){.container{width:calc(100% - 16px);padding-top:18px}}
  </style>
</head>
<body><main class="container">
{% if error_message %}
  <h1>OPDS-каталог</h1>
  <div class="message">{{ error_message }}</div>
  <div class="actions">
    <a href="{{ url_for('index') }}">← На главный экран</a>
  </div>
{% else %}
  <h1>{{ view.title }}</h1>
  <div class="muted">{% if view.view_all %}Все книги · страниц: {{ view.pages }}{% else %}Страница {{ view.page + 1 }}{% endif %}</div>
  <div class="actions">
    <a href="{{ url_for('index') }}">← На главный экран</a>
    {% if view.view_all %}
      <a class="registered-catalog-loading-link" href="{{ url_for('registered_catalog_page', token=view.token, page=0) }}">Вернуться к первой странице</a>
    {% else %}
      {% if view.has_previous %}<a class="registered-catalog-loading-link" href="{{ url_for('registered_catalog_page', token=view.token, page=view.page-1) }}">← Назад</a>{% endif %}
      {% if view.has_next %}<a class="registered-catalog-loading-link" href="{{ url_for('registered_catalog_page', token=view.token, page=view.page+1) }}">Далее →</a>{% endif %}
      {% if view.books %}<a class="registered-catalog-loading-link" href="{{ url_for('registered_catalog_page', token=view.token, view='all') }}">Показать всё</a>{% endif %}
    {% endif %}
  </div>
  {% if view.navigation %}
    <section class="navigation"><strong>Разделы каталога</strong><div class="navigation-links">
      {% for item in view.navigation %}<a class="registered-catalog-loading-link" href="{{ url_for('registered_catalog_page', token=item.token) }}">{{ item.title }}</a>{% endfor %}
    </div></section>
  {% endif %}
  {% if not view.books %}<div class="message muted">На этой странице нет книг.</div>{% endif %}
  {% for book in view.books %}
    <article class="book">
      <div class="book-title">{{ book.title }}</div>
      <div class="author">{{ book.author }}</div>
      <div class="meta">
        {% if book.language %}<span class="badge">{{ book.language|upper }}</span>{% endif %}
        {% if book.genres %}<span class="badge">{{ book.genres|join(', ') }}</span>{% endif %}
        {% if book.formats %}<span class="badge">{{ book.formats|join(', ') }}</span>{% endif %}
        {% if book.translator %}<span class="badge">Перевод: {{ book.translator }}</span>{% endif %}
        {% if book.size %}<span class="badge">Размер: {{ book.size }}</span>{% endif %}
      </div>
    </article>
  {% endfor %}
{% endif %}
</main>
<div id="registeredCatalogLoadingOverlay" class="registered-catalog-loading-overlay" aria-hidden="true">
  <div class="registered-catalog-loading-card" role="status" aria-live="polite">
    <div class="registered-catalog-loading-spinner" aria-hidden="true"></div>
    <div class="registered-catalog-loading-title">Загрузка каталога...</div>
    <div class="registered-catalog-loading-note">Получение данных из OPDS-каталога</div>
  </div>
</div>
<script id="registeredCatalogLoadingScript">
let registeredCatalogLoading = false;

function showRegisteredCatalogLoading(event) {
  if (registeredCatalogLoading) {
    event.preventDefault();
    return;
  }
  registeredCatalogLoading = true;
  const overlay = document.getElementById('registeredCatalogLoadingOverlay');
  overlay.classList.add('visible');
  overlay.setAttribute('aria-hidden', 'false');
  document.querySelectorAll('.registered-catalog-loading-link').forEach((link) => {
    link.classList.add('loading-disabled');
    link.setAttribute('aria-disabled', 'true');
  });
}

function resetRegisteredCatalogLoading() {
  registeredCatalogLoading = false;
  const overlay = document.getElementById('registeredCatalogLoadingOverlay');
  overlay.classList.remove('visible');
  overlay.setAttribute('aria-hidden', 'true');
  document.querySelectorAll('.registered-catalog-loading-link').forEach((link) => {
    link.classList.remove('loading-disabled');
    link.removeAttribute('aria-disabled');
  });
}

document.querySelectorAll('.registered-catalog-loading-link').forEach((link) => {
  link.addEventListener('click', showRegisteredCatalogLoading);
});
window.addEventListener('pageshow', resetRegisteredCatalogLoading);
</script>
</body>
</html>
"""

OPDS_SEARCH_HTML = r"""
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{% if view %}{{ view.title }}{% else %}OPDS-поиск{% endif %}</title>
  <style>
    :root{--bg:#111418;--panel:#191d22;--panel2:#20252b;--border:#30363d;--text:#f0f2f4;--muted:#9aa4ae;--accent:#58a6ff}
    *{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:system-ui,-apple-system,"Segoe UI",sans-serif}
    .container{width:min(900px,calc(100% - 32px));margin:0 auto;padding:30px 0 60px}h1{margin:0 0 8px}.muted{color:var(--muted)}
    .book,.message{margin-top:12px;padding:15px;border:1px solid var(--border);border-radius:9px;background:var(--panel)}
    .book-title{font-size:17px;font-weight:700}.author{margin-top:4px;color:#d2d7dc}.meta{display:flex;flex-wrap:wrap;gap:6px;margin-top:9px}
    .badge{padding:3px 7px;border:1px solid var(--border);border-radius:5px;background:var(--panel2);font-size:12px}.actions{display:flex;gap:8px;margin:18px 0}
    .actions a,.message a,.related-actions a{display:inline-block;padding:8px 11px;border:1px solid var(--border);border-radius:6px;background:var(--panel2);color:var(--accent);text-decoration:none}
    .selection-toolbar{display:flex;flex-wrap:wrap;align-items:center;gap:12px;margin:12px 0;padding:12px;border:1px solid var(--border);border-radius:7px;background:var(--panel2)}
    .format-controls{display:flex;flex-wrap:wrap;gap:10px}.selection-toolbar button{padding:8px 11px;border:1px solid var(--border);border-radius:6px;background:var(--accent);color:#fff;cursor:pointer}.selection-toolbar button:disabled{opacity:.55;cursor:default}
    .book-selector{display:flex;align-items:center;gap:7px;margin-bottom:10px;color:var(--muted);font-size:13px}
    .related-actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}
    .opds-page-link.loading-disabled,.opds-catalog-link.loading-disabled{pointer-events:none;opacity:.55}.opds-page-loading-overlay{position:fixed;inset:0;z-index:9999;display:none;align-items:center;justify-content:center;background:rgba(10,12,15,.88);cursor:wait}.opds-page-loading-overlay.visible{display:flex}.opds-page-loading-card{min-width:min(420px,calc(100% - 32px));padding:24px;border:1px solid var(--border);border-radius:10px;background:var(--panel);text-align:center;box-shadow:0 18px 60px rgba(0,0,0,.45)}.opds-page-loading-spinner{width:34px;height:34px;margin:0 auto 14px;border:4px solid #384149;border-top-color:var(--accent);border-radius:50%;animation:opds-page-spin .85s linear infinite}.opds-page-loading-title{font-size:18px;font-weight:750}.opds-page-loading-note{margin-top:7px;color:var(--muted);font-size:13px}@keyframes opds-page-spin{to{transform:rotate(360deg)}}
    @media(max-width:650px){.container{width:calc(100% - 16px);padding-top:18px}}
  </style>
</head>
<body><main class="container">
{% with messages=get_flashed_messages() %}
  {% for message in messages %}<div class="message">{{ message }}</div>{% endfor %}
{% endwith %}
<div class="actions">
  <a href="{{ url_for('index') }}">← На главный экран</a>
</div>
{% if error_message %}
  <h1>OPDS-поиск</h1>
  <div class="message">{{ error_message }}</div>
  {% if show_settings_link %}<div class="message"><a href="{{ url_for('opds_settings_page') }}">Настроить OPDS</a></div>{% endif %}
{% else %}
  <h1>{{ view.title }}</h1>
  <div class="muted">Запрос: {{ view.query }}</div>
  {% if view.total_results is not none %}<div class="muted">Всего найдено: {{ view.total_results }}</div>{% endif %}
  <div class="muted">Книг на странице: {{ view.books|length }}</div>
  <div class="actions">
    <a href="{{ url_for('queue_page') }}">Очередь</a>
    {% if view.has_previous %}<a class="opds-page-link" href="{{ url_for('opds_search_page', q=view.query, page=view.page-1) }}">← Назад</a>{% endif %}
    {% if view.has_next %}<a class="opds-page-link" href="{{ url_for('opds_search_page', q=view.query, page=view.page+1) }}">Далее →</a>{% endif %}
  </div>
  {% if not view.books %}<div class="message muted">По вашему запросу ничего не найдено.</div>{% endif %}
  {% if view.books %}
    <form id="opdsQueueForm" class="selection-toolbar" method="post" action="{{ url_for('opds_search_queue_add') }}">
      <input type="hidden" name="q" value="{{ view.query }}">
      <span>Выбрано: <strong id="selectedCount">0</strong></span>
      <span class="format-controls" role="group" aria-label="Формат">
        <label><input type="radio" name="format_mode" value="auto" checked> EPUB, если доступен — иначе FB2</label>
        <label><input type="radio" name="format_mode" value="epub"> Только EPUB</label>
        <label><input type="radio" name="format_mode" value="fb2"> Только FB2</label>
      </span>
      <button id="opdsQueueSubmit" type="submit" disabled>Добавить выбранные в очередь</button>
    </form>
  {% endif %}
  {% for book in view.books %}
    <article class="book">
      <label class="book-selector"><input class="book-check" type="checkbox" value="{{ book.id }}"> Выбрать книгу</label>
      <div class="book-title">{{ book.title }}</div>
      {% if book.author %}<div class="author">{{ book.author }}</div>{% endif %}
      <div class="meta">
        {% if book.language %}<span class="badge">{{ book.language }}</span>{% endif %}
        {% if book.genres %}<span class="badge">{{ book.genres|join(', ') }}</span>{% endif %}
        {% if book.formats %}<span class="badge">{{ book.formats|join(', ') }}</span>{% endif %}
        {% if book.translator %}<span class="badge">Перевод: {{ book.translator }}</span>{% endif %}
        {% if book.size %}<span class="badge">Размер: {{ book.size }}</span>{% endif %}
      </div>
      {% if book.related %}
        <div class="related-actions">
          {% for related in book.related %}<a class="opds-catalog-link" href="{{ url_for('registered_catalog_page', token=related.token) }}">{{ related.title }}</a>{% endfor %}
        </div>
      {% endif %}
    </article>
  {% endfor %}
  <script>
      const selectionStorageKey = {{ selection_storage_key|tojson }};

      function loadStoredSelection() {
        try {
          const stored = JSON.parse(sessionStorage.getItem(selectionStorageKey) || '[]');
          return new Set(Array.isArray(stored) ? stored.map((id) => String(id)) : []);
        } catch (error) {
          return new Set();
        }
      }

      const selectedBookIds = loadStoredSelection();

      function saveStoredSelection() {
        try {
          sessionStorage.setItem(
            selectionStorageKey,
            JSON.stringify(Array.from(selectedBookIds))
          );
        } catch (error) {
          return;
        }
      }

      if ({{ clear_selection|tojson }}) {
        selectedBookIds.clear();
        saveStoredSelection();
      }

      function updateSelectedCount() {
        const selectedCount = document.getElementById('selectedCount');
        if (selectedCount) selectedCount.textContent = String(selectedBookIds.size);
        const submitButton = document.getElementById('opdsQueueSubmit');
        if (submitButton) submitButton.disabled = selectedBookIds.size === 0;
      }

      function syncCurrentPageSelection() {
        document.querySelectorAll('.book-check').forEach((checkbox) => {
          const id = String(checkbox.value);
          if (checkbox.checked) selectedBookIds.add(id);
          else selectedBookIds.delete(id);
        });
        saveStoredSelection();
      }

      function appendStoredSelectionToOpdsQueueForm() {
        const form = document.getElementById('opdsQueueForm');
        if (!form) return;
        form.querySelectorAll('.stored-selection-input').forEach((input) => input.remove());
        selectedBookIds.forEach((id) => {
          const input = document.createElement('input');
          input.type = 'hidden';
          input.name = 'book_id';
          input.value = String(id);
          input.className = 'stored-selection-input';
          form.appendChild(input);
        });
      }

      document.querySelectorAll('.book-check').forEach((checkbox) => {
        const id = String(checkbox.value);
        checkbox.checked = selectedBookIds.has(id);
        checkbox.addEventListener('change', () => {
          if (checkbox.checked) {
            selectedBookIds.add(id);
          } else {
            selectedBookIds.delete(id);
          }
          saveStoredSelection();
          updateSelectedCount();
        });
      });
      const opdsQueueForm = document.getElementById('opdsQueueForm');
      if (opdsQueueForm) {
        opdsQueueForm.addEventListener('submit', (event) => {
          syncCurrentPageSelection();
          updateSelectedCount();
          if (selectedBookIds.size === 0) {
            event.preventDefault();
            return;
          }
          appendStoredSelectionToOpdsQueueForm();
        });
      }
      updateSelectedCount();
  </script>
{% endif %}
</main>
<div id="opdsPageLoadingOverlay" class="opds-page-loading-overlay" aria-hidden="true">
  <div class="opds-page-loading-card" role="status" aria-live="polite">
    <div class="opds-page-loading-spinner" aria-hidden="true"></div>
    <div id="opdsLoadingTitle" class="opds-page-loading-title">Загрузка страницы...</div>
    <div id="opdsLoadingNote" class="opds-page-loading-note">Получение данных из OPDS-каталога</div>
  </div>
</div>
<script id="opdsPageLoadingScript">
let opdsPageLoading = false;

function beginOpdsLoading(event, title, note) {
  if (opdsPageLoading) {
    event.preventDefault();
    return;
  }
  opdsPageLoading = true;
  const overlay = document.getElementById('opdsPageLoadingOverlay');
  document.getElementById('opdsLoadingTitle').textContent = title;
  document.getElementById('opdsLoadingNote').textContent = note;
  overlay.classList.add('visible');
  overlay.setAttribute('aria-hidden', 'false');
  document.querySelectorAll('.opds-page-link,.opds-catalog-link').forEach((link) => {
    link.classList.add('loading-disabled');
    link.setAttribute('aria-disabled', 'true');
  });
}

function showOpdsPageLoading(event) {
  beginOpdsLoading(
    event,
    'Загрузка страницы...',
    'Получение данных из OPDS-каталога'
  );
}

function showOpdsCatalogLoading(event) {
  beginOpdsLoading(
    event,
    'Загрузка каталога...',
    'Получение данных из OPDS-каталога'
  );
}

function resetOpdsPageLoading() {
  opdsPageLoading = false;
  const overlay = document.getElementById('opdsPageLoadingOverlay');
  overlay.classList.remove('visible');
  overlay.setAttribute('aria-hidden', 'true');
  document.querySelectorAll('.opds-page-link,.opds-catalog-link').forEach((link) => {
    link.classList.remove('loading-disabled');
    link.removeAttribute('aria-disabled');
  });
}

document.querySelectorAll('.opds-page-link').forEach((link) => {
  link.addEventListener('click', showOpdsPageLoading);
});
document.querySelectorAll('.opds-catalog-link').forEach((link) => {
  link.addEventListener('click', showOpdsCatalogLoading);
});
window.addEventListener('pageshow', resetOpdsPageLoading);
</script>
</body>
</html>
"""

JOB_HTML = r"""
<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Массовая загрузка</title><style>{{ css|safe }}
.container{width:min(820px,calc(100% - 30px))}.jobcard{padding:22px}.status{color:var(--muted);margin:6px 0 15px}.preflight{color:var(--muted);font-size:13px;margin-top:7px}.progress-wrap{position:relative;width:100%;height:24px;border-radius:20px;overflow:hidden;background:#252b31;margin-bottom:18px}.progress{position:absolute;inset:0 auto 0 0;width:0;background:var(--accent);transition:width .3s}.progress-text{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:#fff;font-weight:700;font-size:12px;text-shadow:0 1px 2px #000;z-index:2}.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:18px}.stat{padding:12px;border:1px solid var(--border);border-radius:7px;text-align:center}.stat strong{display:block;font-size:22px}.current{min-height:66px;padding:12px;margin-bottom:15px}.current-main{font-weight:650}.current-detail{margin-top:6px;color:var(--muted);font-size:13px;line-height:1.4}.actions{display:flex;flex-wrap:wrap;gap:8px}.actions form{margin:0}.actions a,.actions button{display:inline-block;padding:9px 12px;border:1px solid var(--border);border-radius:6px;background:var(--panel2);color:var(--text);text-decoration:none;cursor:pointer}.errors{margin-top:20px;color:#e8b4b4;font-size:13px}@media(max-width:650px){.stats{grid-template-columns:repeat(2,1fr)}}
</style></head><body><div class="container"><div class="jobcard card"><div class="nav"><a href="{{ url_for('index') }}">← На главный экран</a><a href="{{ url_for('jobs_page') }}">Все загрузки</a><a href="{{ url_for('queue_history') }}">История</a><a href="{{ url_for('notifications_page') }}">Уведомления{% if unread_notifications %} ({{ unread_notifications }}){% endif %}</a></div><h1 style="margin-top:18px">Массовая загрузка</h1><div>{{ job.title }}</div><div class="preflight">Перед запуском: выбрано {{ job.selected_count }} · уже было в библиотеке {{ job.existing_filtered }} · в очередь добавлено {{ job.total }}</div>{% if job.download_duplicates %}<div class="muted" style="margin-top:6px">Режим дублей: загружать все отмеченные издания</div>{% elif job.duplicates_filtered %}<div class="muted" style="margin-top:6px">Альтернативных изданий исключено перед загрузкой: {{ job.duplicates_filtered }}</div>{% endif %}<div class="status" id="status">{{ job.status_text }}</div><div class="preflight">Сеть: до {{ download_attempts }} попыток на книгу · таймаут подключения {{ connect_timeout }} сек · ожидание данных {{ read_timeout }} сек</div>
<div class="progress-wrap"><div class="progress" id="progress"></div><div class="progress-text" id="progressText">{{ job.processed }} / {{ job.total }}</div></div>
<div class="stats"><div class="stat"><strong id="processed">{{ job.processed }}</strong>Обработано</div><div class="stat"><strong id="downloaded">{{ job.downloaded }}</strong>Добавлено</div><div class="stat"><strong id="skipped">{{ job.skipped }}</strong>Пропущено</div><div class="stat"><strong id="errorCount">{{ job.error_count }}</strong>Ошибок</div></div>
<div class="current card"><div class="current-main" id="current">{{ job.current or '—' }}</div><div class="current-detail" id="currentDetail">{{ job.current_detail or '' }}</div></div><div class="actions"><a href="{{ job.return_url }}">{{ job.return_label }}</a><a href="{{ url_for('search_return') }}">Вернуться к поиску</a>
<form id="actionForm" method="post" action="{{ url_for('cancel_job',job_id=job.id) }}"><button id="actionButton" class="danger" type="submit">Остановить</button></form>
<form id="retryForm" method="post" action="{{ url_for('retry_job',job_id=job.id) }}" {% if not job.failed_books %}style="display:none"{% endif %}><button type="submit">Повторить ошибки</button></form></div><div class="errors" id="errors">{% for item in job.errors[-10:] %}<div>{{ item }}</div>{% endfor %}</div></div></div>
<script>
const jobId={{ job.id|tojson }};const returnUrl={{ job.return_url|tojson }};
function terminal(s){return ['finished','cancelled','interrupted'].includes(s)}
function setAction(data){const f=document.getElementById('actionForm'),b=document.getElementById('actionButton');if(terminal(data.status)){f.onsubmit=(e)=>{e.preventDefault();location.href=returnUrl};b.textContent='ОК';b.classList.remove('danger')}else{f.onsubmit=null;b.textContent='Остановить';b.classList.add('danger')}}
async function refreshJob(){try{const r=await fetch('/api/job/'+jobId,{cache:'no-store'});if(!r.ok)return;const d=await r.json();document.getElementById('status').textContent=d.status_text;document.getElementById('processed').textContent=d.processed;document.getElementById('downloaded').textContent=d.downloaded;document.getElementById('skipped').textContent=d.skipped;document.getElementById('errorCount').textContent=d.error_count;document.getElementById('current').textContent=d.current||'—';document.getElementById('currentDetail').textContent=d.current_detail||'';const p=d.total?Math.round(d.processed*100/d.total):0;document.getElementById('progress').style.width=p+'%';document.getElementById('progressText').textContent=d.processed+' / '+d.total+' ('+p+'%)';const e=document.getElementById('errors');e.innerHTML='';d.errors.slice(-10).forEach(t=>{const x=document.createElement('div');x.textContent=t;e.appendChild(x)});document.getElementById('retryForm').style.display=d.failed_books.length?'block':'none';setAction(d);if(!terminal(d.status))setTimeout(refreshJob,1500)}catch(e){setTimeout(refreshJob,3000)}}
setAction({status:{{ job.status|tojson }}});refreshJob();
</script></body></html>
"""

QUEUE_HTML = r"""
<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Очередь загрузки</title><style>{{ css|safe }}
.queue-grid{display:grid;grid-template-columns:1fr 340px;gap:16px}.settings{padding:16px;position:sticky;top:16px;align-self:start}.settings h3{margin-top:0}.settings label{display:block;margin:10px 0}.settings input[type=time],.settings input[type=number],.settings select{width:100%;padding:9px;border:1px solid var(--border);border-radius:6px;background:var(--panel2);color:var(--text)}.controls{display:flex;flex-wrap:wrap;gap:8px;margin:13px 0}.controls form{margin:0}.controls button{padding:9px 12px;border:1px solid var(--border);border-radius:6px;background:var(--panel2);color:var(--text);cursor:pointer}.qstats{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin-bottom:14px}.qstat{padding:11px;text-align:center}.qstat strong{display:block;font-size:21px}.summary-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:14px}.summary-card{padding:13px}.summary-card strong{display:block;font-size:18px;margin-bottom:4px}.summary-card .small{font-size:12px;color:var(--muted)}.progress-shell{height:22px;background:#272d33;border-radius:12px;overflow:hidden;position:relative;margin-top:9px}.progress-fill{height:100%;background:var(--accent);width:0;transition:width .3s}.progress-label{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:750}.item{padding:13px;margin-bottom:9px}.item-head{display:flex;justify-content:space-between;gap:12px}.item-title{font-weight:750}.item-author{color:var(--muted);margin-top:3px}.item-meta{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}.item-detail{margin-top:7px;color:var(--muted);font-size:12px}.item-actions{display:flex;gap:6px;align-items:flex-start}.item-actions form{margin:0}.item-actions button{padding:6px 9px;border:1px solid var(--border);border-radius:5px;background:var(--panel2);color:var(--text);cursor:pointer}.item-actions .remove{border-color:var(--danger);background:transparent;color:var(--danger)}.status-pending{color:#d9c47d}.status-downloading{color:var(--blue)}.status-done{color:var(--accent)}.status-error{color:var(--danger)}.status-skipped{color:var(--muted)}.priority-high{color:#ffcf66;border-color:#80652a}.priority-low{color:var(--muted)}.empty{padding:28px;text-align:center;color:var(--muted)}.schedule-note{font-size:12px;color:var(--muted);line-height:1.45;margin-top:10px}.worker{padding:12px;margin-bottom:14px;border-left:4px solid var(--accent)}.worker.paused{border-left-color:#d9c47d}.disk-danger{color:var(--danger)!important}.disk-ok{color:var(--accent)!important}@media(max-width:850px){.queue-grid{grid-template-columns:1fr}.settings{position:static}.qstats{grid-template-columns:repeat(2,1fr)}.summary-grid{grid-template-columns:1fr}}
</style></head><body><div class="container"><div class="topbar"><div><h1>Очередь загрузки</h1><div class="subtitle">Автоматическая загрузка выполняется по расписанию, пока OPDS Desk запущен.</div></div><div class="nav"><a href="{{ url_for('index') }}">← На главный экран</a><a href="{{ url_for('context_return') }}">← Назад</a><a href="{{ url_for('queue_runs_page') }}">Запуски</a><a href="{{ url_for('queue_history') }}">История</a><a href="{{ url_for('notifications_page') }}">Уведомления{% if unread_notifications %} ({{ unread_notifications }}){% endif %}</a><a href="{{ url_for('jobs_page') }}">Загрузки</a></div></div>
{% with messages=get_flashed_messages() %}{% for message in messages %}<div class="flash">{{ message }}</div>{% endfor %}{% endwith %}

<div class="qstats">
<div class="qstat card"><strong id="countPending">{{ counts.pending }}</strong>Ожидает</div>
<div class="qstat card"><strong id="countDownloading">{{ counts.downloading }}</strong>Загружается</div>
<div class="qstat card"><strong id="countDone">{{ counts.done }}</strong>Готово</div>
<div class="qstat card"><strong id="countSkipped">{{ counts.skipped }}</strong>Пропущено</div>
<div class="qstat card"><strong id="countError">{{ counts.error }}</strong>Ошибок</div>
</div>

<div class="summary-grid">
<div class="summary-card card"><strong id="futureSize">{{ size_summary.text }}</strong><div class="small">Примерный объём ожидающих{% if size_summary.unknown %} · без размера: {{ size_summary.unknown }}{% endif %}</div></div>
<div class="summary-card card"><strong id="diskFree" class="{% if disk.low %}disk-danger{% else %}disk-ok{% endif %}">{{ disk.free_text }}</strong><div class="small" id="diskNote">Свободно на {{ destination }} из {{ disk.total_text }} · резерв {{ min_free_gb }} ГБ</div></div>
<div class="summary-card card"><strong id="runTitle">{{ run.processed }} / {{ run.total }}</strong><div class="small" id="runNote">{% if run.started %}Текущий запуск · загружено {{ run.downloaded_text }} · {{ run.elapsed_text }}{% else %}Запуск ещё не выполнялся{% endif %}</div><div class="progress-shell"><div class="progress-fill" id="runProgress" style="width:{{ run.percent }}%"></div><div class="progress-label" id="runProgressLabel">{{ run.processed }} / {{ run.total }} ({{ run.percent }}%)</div></div></div>
</div>

<div id="workerBox" class="worker card{% if paused %} paused{% endif %}" {% if not worker_active and not paused %}style="display:none"{% endif %}>
<strong id="workerTitle">{% if worker_active %}Очередь выполняется{% elif paused %}Очередь на паузе{% endif %}</strong>
<div id="workerCurrent" class="muted" style="margin-top:5px">{% if current_item %}{{ current_item.author }} / {{ current_item.title }}{% elif paused and pause_reason %}{{ pause_reason }}{% endif %}</div>
<div id="workerDetail" class="item-detail">{% if current_item %}{{ current_item.detail or 'Подготовка…' }}{% elif paused %}Следующая книга не начнётся до снятия паузы.{% endif %}</div>
</div>

<div class="queue-grid"><main>
{% if not items %}<div class="empty card">Очередь пуста. Добавляйте книги из поиска, автора или серии. Завершённые записи находятся в разделе «История».</div>{% endif %}
{% for item in items %}<div class="item card" data-item-id="{{ item.id }}"><div class="item-head"><div><div class="item-title">{{ item.title }}</div><div class="item-author">{{ item.author }}</div></div>
<div class="item-actions">
{% if item.status == 'pending' %}
<form method="post" action="{{ url_for('queue_priority',item_id=item.id,direction='up') }}"><button type="submit" title="Повысить приоритет">↑</button></form>
<form method="post" action="{{ url_for('queue_priority',item_id=item.id,direction='down') }}"><button type="submit" title="Понизить приоритет">↓</button></form>
{% endif %}
{% if item.status in ['pending','error','skipped'] %}<form method="post" action="{{ url_for('queue_remove',item_id=item.id) }}"><button class="remove" type="submit">Удалить</button></form>{% endif %}
</div></div>
<div class="item-meta"><span class="badge status-{{ item.status }}">{{ item.status_text }}</span><span class="badge">{{ item.format_text }}</span><span class="badge priority-{{ item.priority_class }}">Приоритет: {{ item.priority_text }}</span>{% if item.size_text %}<span class="badge">≈ {{ item.size_text }}</span>{% endif %}{% if item.download_duplicates %}<span class="badge">Дубли: да</span>{% endif %}<span class="badge">Добавлено {{ item.added_text }}</span></div>{% if item.detail %}<div class="item-detail">{{ item.detail }}</div>{% endif %}{% if item.error_category %}<div class="item-detail status-error">Категория: {{ item.error_category }}</div>{% endif %}{% if item.error %}<div class="item-detail status-error">{{ item.error }}</div>{% endif %}</div>{% endfor %}
</main><aside class="settings card"><h3>Ночной запуск</h3><form method="post" action="{{ url_for('queue_settings') }}"><label><input type="checkbox" name="auto_enabled" value="1" {% if auto_enabled %}checked{% endif %}> Запускать очередь автоматически каждый день</label><label>Время запуска<input type="time" name="auto_time" value="{{ auto_time }}" required></label><label>Часовой пояс<select name="tz_offset">{% for tz in tz_options %}<option value="{{ tz }}" {% if tz == tz_offset %}selected{% endif %}>UTC{{ tz }}</option>{% endfor %}</select></label><label>Минимальный свободный остаток, ГБ<input type="number" name="min_free_gb" value="{{ min_free_gb }}" min="1" max="500" step="1" required></label><button class="button-link primary" type="submit">Сохранить настройки</button></form><div class="schedule-note">Перед каждой книгой проверяется свободное место на {{ destination }}. Если останется меньше указанного резерва, очередь автоматически встанет на паузу.</div>
<div class="controls"><form method="post" action="{{ url_for('queue_start_now') }}"><button class="primary" type="submit">Запустить сейчас</button></form>{% if paused %}<form method="post" action="{{ url_for('queue_resume') }}"><button type="submit">Продолжить</button></form>{% else %}<form method="post" action="{{ url_for('queue_pause') }}"><button type="submit">Пауза</button></form>{% endif %}</div>
<div class="controls"><form method="post" action="{{ url_for('queue_retry_errors') }}"><button type="submit">Повторить ошибки</button></form><form method="post" action="{{ url_for('queue_clear_pending') }}" onsubmit="return confirm('Удалить все ожидающие элементы очереди?')"><button class="danger" type="submit">Очистить ожидающие</button></form></div>
<div class="schedule-note">Скачивание последовательное: одна книга за раз. Очередь обрабатывает сначала высокий, затем обычный и низкий приоритет. Размер будущей загрузки берётся из метаданных OPDS и является приблизительным.</div></aside></div></div>
<script>
let wasWorkerActive={{ worker_active|tojson }};
const notificationStorageKey='opdsDeskLastNotificationId';
const legacyNotificationStorageKey='flibustaLastNotificationId';
let storedNotificationId=localStorage.getItem(notificationStorageKey);
if(storedNotificationId===null){
  const legacyNotificationId=localStorage.getItem(legacyNotificationStorageKey);
  if(legacyNotificationId!==null){
    storedNotificationId=legacyNotificationId;
    localStorage.setItem(notificationStorageKey,legacyNotificationId);
  }
}
let lastNotificationId=Number(storedNotificationId||0);
async function pollBridgeNotification(){
  try{
    const r=await fetch('{{ url_for("notifications_latest_api") }}',{cache:'no-store'});
    if(r.ok){
      const n=await r.json();
      if(n.id&&n.id>lastNotificationId){
        lastNotificationId=n.id;localStorage.setItem(notificationStorageKey,String(n.id));
        if('Notification' in window&&Notification.permission==='granted'){
          new Notification(n.title,{body:n.message});
        }
      }
    }
  }catch(e){}
  setTimeout(pollBridgeNotification,5000);
}
setTimeout(pollBridgeNotification,3000);
function setText(id,value){const el=document.getElementById(id);if(el)el.textContent=value}
async function refreshQueue(){
  try{
    const r=await fetch('{{ url_for("queue_state_api") }}',{cache:'no-store'});
    if(!r.ok)throw new Error('HTTP '+r.status);
    const d=await r.json();
    setText('countPending',d.counts.pending);setText('countDownloading',d.counts.downloading);setText('countDone',d.counts.done);setText('countSkipped',d.counts.skipped);setText('countError',d.counts.error);
    setText('futureSize',d.size_summary.text);
    const df=document.getElementById('diskFree');if(df){df.textContent=d.disk.free_text;df.className=d.disk.low?'disk-danger':'disk-ok'}
    setText('diskNote','Свободно на {{ destination }} из '+d.disk.total_text+' · резерв '+d.min_free_gb+' ГБ');
    setText('runTitle',d.run.processed+' / '+d.run.total);
    setText('runNote',d.run.started?'Текущий запуск · загружено '+d.run.downloaded_text+' · '+d.run.elapsed_text:'Запуск ещё не выполнялся');
    const pf=document.getElementById('runProgress');if(pf)pf.style.width=d.run.percent+'%';
    setText('runProgressLabel',d.run.processed+' / '+d.run.total+' ('+d.run.percent+'%)');
    const box=document.getElementById('workerBox');
    if(box){
      if(d.worker_active||d.paused){box.style.display='block';box.classList.toggle('paused',d.paused&&!d.worker_active);setText('workerTitle',d.worker_active?'Очередь выполняется':'Очередь на паузе');setText('workerCurrent',d.current_item?(d.current_item.author+' / '+d.current_item.title):(d.pause_reason||''));setText('workerDetail',d.current_item?(d.current_item.detail||'Подготовка…'):(d.paused?'Следующая книга не начнётся до снятия паузы.':''));}
      else box.style.display='none';
    }
    if(wasWorkerActive&&!d.worker_active){setTimeout(()=>location.reload(),700);return}
    wasWorkerActive=d.worker_active;
  }catch(e){}
  setTimeout(refreshQueue,2000);
}
setTimeout(refreshQueue,2000);
</script></body></html>
"""
JOBS_HTML = r"""
<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Загрузки</title><style>{{ css|safe }}
.job{padding:14px;margin-bottom:10px}.job-head{display:flex;justify-content:space-between;gap:10px;align-items:flex-start}.job-title{font-weight:700}.job-meta{color:var(--muted);font-size:13px;margin:6px 0}.job-stats{display:flex;flex-wrap:wrap;gap:6px}.job a{text-decoration:none}.empty{padding:25px;text-align:center;color:var(--muted)}
</style></head><body><div class="container"><div class="topbar"><div><h1>Загрузки</h1><div class="subtitle">Текущие и последние задания OPDS Desk</div></div><div class="nav"><a href="{{ url_for('index') }}">← На главный экран</a><a href="{{ url_for('context_return') }}">← Назад</a><a href="{{ url_for('queue_page') }}">Очередь</a><a href="{{ url_for('queue_runs_page') }}">Запуски</a><a href="{{ url_for('queue_history') }}">История</a><a href="{{ url_for('notifications_page') }}">Уведомления{% if unread_notifications %} ({{ unread_notifications }}){% endif %}</a></div></div>
{% if not job_list %}<div class="empty card">Заданий пока нет.</div>{% endif %}{% for job in job_list %}<div class="job card"><div class="job-head"><div><div class="job-title"><a href="{{ url_for('job_page',job_id=job.id) }}">{{ job.title }}</a></div><div class="job-meta">{{ job.created_text }} · {{ job.status_text }}</div></div><a class="button-link" href="{{ url_for('job_page',job_id=job.id) }}">Открыть</a></div><div class="job-stats"><span class="badge">{{ job.processed }}/{{ job.total }}</span><span class="badge exists">Добавлено: {{ job.downloaded }}</span><span class="badge">Пропущено: {{ job.skipped }}</span>{% if job.error_count %}<span class="badge danger">Ошибок: {{ job.error_count }}</span>{% endif %}</div></div>{% endfor %}</div></body></html>
"""



HISTORY_HTML = r"""
<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>История очереди</title><style>{{ css|safe }}
.filters{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:16px}.filters a{padding:8px 11px;border:1px solid var(--border);border-radius:6px;background:var(--panel2);color:var(--text);text-decoration:none}.filters a.active{background:var(--accent);border-color:var(--accent);color:#07130c;font-weight:700}.history-search{display:flex;gap:8px;margin-bottom:16px}.history-search input{flex:1;padding:10px 12px;border:1px solid var(--border);border-radius:6px;background:var(--panel);color:var(--text)}.history-search button{padding:10px 14px;border:1px solid var(--accent);border-radius:6px;background:var(--accent);color:#07130c;font-weight:700}.hist{padding:14px;margin-bottom:9px}.hist-head{display:flex;justify-content:space-between;gap:12px}.hist-title{font-weight:750}.hist-author{margin-top:4px;color:var(--muted)}.hist-meta{display:flex;flex-wrap:wrap;gap:6px;margin-top:9px}.hist-detail{margin-top:8px;color:var(--muted);font-size:12px}.hist-error{margin-top:8px;color:#e8b4b4;font-size:12px}.empty{padding:28px;text-align:center;color:var(--muted)}.summary{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:16px}.summary .card{padding:10px 13px}.actions{display:flex;flex-wrap:wrap;gap:8px;margin:14px 0}.actions form{margin:0}.actions button{padding:8px 11px;border:1px solid var(--border);border-radius:6px;background:var(--panel2);color:var(--text);cursor:pointer}
</style></head><body><div class="container">
<div class="topbar"><div><h1>История очереди</h1><div class="subtitle">Завершённые, пропущенные и ошибочные загрузки</div></div><div class="nav"><a href="{{ url_for('index') }}">← На главный экран</a><a href="{{ url_for('context_return') }}">← Назад</a><a href="{{ url_for('queue_page') }}">Очередь</a><a href="{{ url_for('queue_runs_page') }}">Запуски</a><a href="{{ url_for('notifications_page') }}">Уведомления{% if unread_notifications %} ({{ unread_notifications }}){% endif %}</a></div></div>
<div class="summary"><div class="card">Готово: <strong>{{ counts.done }}</strong></div><div class="card">Пропущено: <strong>{{ counts.skipped }}</strong></div><div class="card">Ошибок: <strong>{{ counts.error }}</strong></div></div>
<form class="history-search" method="get" action="{{ url_for('queue_history') }}"><input type="hidden" name="status" value="{{ status_filter }}"><input type="text" name="q" value="{{ search_query }}" placeholder="Поиск по названию или автору"><button type="submit">Найти</button></form>
<div class="filters">
<a class="{% if status_filter=='all' %}active{% endif %}" href="{{ url_for('queue_history',status='all',q=search_query) }}">Все</a>
<a class="{% if status_filter=='done' %}active{% endif %}" href="{{ url_for('queue_history',status='done',q=search_query) }}">Готово</a>
<a class="{% if status_filter=='skipped' %}active{% endif %}" href="{{ url_for('queue_history',status='skipped',q=search_query) }}">Пропущено</a>
<a class="{% if status_filter=='error' %}active{% endif %}" href="{{ url_for('queue_history',status='error',q=search_query) }}">Ошибки</a>
</div>
<div class="actions">{% if counts.error %}<form method="post" action="{{ url_for('queue_retry_errors') }}"><button type="submit">Повторить все ошибки</button></form>{% endif %}<form method="post" action="{{ url_for('queue_clear_history') }}" onsubmit="return confirm('Удалить из истории успешные и пропущенные записи?')"><button class="danger" type="submit">Очистить успешную историю</button></form></div>
{% if not items %}<div class="empty card">По выбранному фильтру записей нет.</div>{% endif %}
{% for item in items %}<div class="hist card"><div class="hist-head"><div><div class="hist-title">{{ item.title }}</div><div class="hist-author">{{ item.author }}</div></div><span class="badge {% if item.status=='done' %}exists{% elif item.status=='error' %}danger{% endif %}">{{ item.status_text }}</span></div>
<div class="hist-meta"><span class="badge">{{ item.format_text }}</span><span class="badge">Приоритет: {{ item.priority_text }}</span>{% if item.error_category %}<span class="badge status-error">{{ item.error_category }}</span>{% endif %}{% if item.run_id %}<a class="badge" href="{{ url_for('queue_run_detail',run_id=item.run_id) }}">Запуск {{ item.run_id[:8] }}</a>{% endif %}<span class="badge">Добавлено {{ item.added_text }}</span>{% if item.started_text %}<span class="badge">Старт {{ item.started_text }}</span>{% endif %}{% if item.finished_text %}<span class="badge">Завершено {{ item.finished_text }}</span>{% endif %}{% if item.duration_text %}<span class="badge">{{ item.duration_text }}</span>{% endif %}</div>
{% if item.detail %}<div class="hist-detail">{{ item.detail }}</div>{% endif %}{% if item.error %}<div class="hist-error">{{ item.error }}</div>{% endif %}</div>{% endfor %}
</div></body></html>
"""


RUNS_HTML = r"""
<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Запуски очереди</title><style>{{ css|safe }}
.run{padding:14px;margin-bottom:10px}.run-head{display:flex;justify-content:space-between;gap:12px}.run-title{font-weight:750}.run-meta{display:flex;flex-wrap:wrap;gap:6px;margin-top:9px}.run-ok{color:var(--accent)}.run-error{color:var(--danger)}.empty{padding:28px;text-align:center;color:var(--muted)}
</style></head><body><div class="container">
<div class="topbar"><div><h1>Запуски очереди</h1><div class="subtitle">Каждый запуск хранится отдельно по run_id</div></div><div class="nav"><a href="{{ url_for('index') }}">← На главный экран</a><a href="{{ url_for('context_return') }}">← Назад</a><a href="{{ url_for('queue_page') }}">Очередь</a><a href="{{ url_for('queue_history') }}">История</a><a href="{{ url_for('notifications_page') }}">Уведомления{% if unread_notifications %} ({{ unread_notifications }}){% endif %}</a></div></div>
{% if not runs %}<div class="empty card">Запусков пока нет.</div>{% endif %}
{% for run in runs %}
<div class="run card"><div class="run-head"><div><div class="run-title">{{ run.started_text }} · {{ run.status_text }}</div><div class="muted" style="margin-top:4px">run_id: {{ run.run_id }} · {{ run.trigger_text }}</div></div><a class="button-link" href="{{ url_for('queue_run_detail',run_id=run.run_id) }}">Открыть</a></div>
<div class="run-meta"><span class="badge">Всего: {{ run.total }}</span><span class="badge run-ok">Готово: {{ run.counts.done }}</span><span class="badge">Пропущено: {{ run.counts.skipped }}</span><span class="badge{% if run.counts.error %} run-error{% endif %}">Ошибок: {{ run.counts.error }}</span><span class="badge">{{ run.downloaded_text }}</span><span class="badge">{{ run.elapsed_text }}</span>{% if run.recovered_count %}<span class="badge">Восстановлено после сбоя: {{ run.recovered_count }}</span>{% endif %}</div></div>
{% endfor %}
</div></body></html>
"""


RUN_DETAIL_HTML = r"""
<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Запуск {{ run.short_id }}</title><style>{{ css|safe }}
.summary{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin:14px 0}.sum{padding:12px}.sum strong{display:block;font-size:19px}.item{padding:13px;margin-bottom:9px}.item-title{font-weight:750}.item-author{color:var(--muted);margin-top:3px}.meta{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}.error{margin-top:8px;color:#e8b4b4;font-size:12px}.detail{margin-top:7px;color:var(--muted);font-size:12px}.actions{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0}.actions form{margin:0}.status-error{color:var(--danger)}@media(max-width:700px){.summary{grid-template-columns:repeat(2,1fr)}}
</style></head><body><div class="container">
<div class="topbar"><div><h1>Запуск {{ run.short_id }}</h1><div class="subtitle">{{ run.started_text }} · {{ run.status_text }} · {{ run.trigger_text }}</div></div><div class="nav"><a href="{{ url_for('index') }}">← На главный экран</a><a href="{{ url_for('queue_runs_page') }}">← Запуски</a><a href="{{ url_for('queue_page') }}">Очередь</a><a href="{{ url_for('queue_history') }}">История</a></div></div>
<div class="summary"><div class="sum card"><strong>{{ run.counts.done }}</strong>Готово</div><div class="sum card"><strong>{{ run.counts.skipped }}</strong>Пропущено</div><div class="sum card"><strong>{{ run.counts.error }}</strong>Ошибок</div><div class="sum card"><strong>{{ run.downloaded_text }}</strong>Загружено</div></div>
<div class="muted">run_id: {{ run.run_id }} · время: {{ run.elapsed_text }}{% if run.recovered_count %} · восстановлено после аварии: {{ run.recovered_count }}{% endif %}</div>
{% if run.counts.error %}<div class="actions"><form method="post" action="{{ url_for('queue_retry_run_errors',run_id=run.run_id) }}"><button class="button-link" type="submit">Повторить ошибки этого запуска</button></form></div>{% endif %}
{% for item in items %}<div class="item card"><div class="item-title">{{ item.title }}</div><div class="item-author">{{ item.author }}</div><div class="meta"><span class="badge">{{ item.status_text }}</span>{% if item.error_category %}<span class="badge status-error">{{ item.error_category }}</span>{% endif %}<span class="badge">Добавлено {{ item.added_text }}</span>{% if item.started_text %}<span class="badge">Старт {{ item.started_text }}</span>{% endif %}{% if item.finished_text %}<span class="badge">Завершено {{ item.finished_text }}</span>{% endif %}{% if item.downloaded_bytes %}<span class="badge">{{ item.downloaded_text }}</span>{% endif %}</div>{% if item.detail %}<div class="detail">{{ item.detail }}</div>{% endif %}{% if item.error %}<div class="error">{{ item.error }}</div>{% endif %}</div>{% endfor %}
</div></body></html>
"""


NOTIFICATIONS_HTML = r"""
<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Уведомления</title><style>{{ css|safe }}
.notice{padding:14px;margin-bottom:10px;border-left:4px solid var(--blue)}.notice.success{border-left-color:var(--accent)}.notice.error{border-left-color:var(--danger)}.notice.warning{border-left-color:#d9c47d}.notice-title{font-weight:750}.notice-message{margin-top:6px;color:#d2d7dc;line-height:1.45}.notice-time{margin-top:7px;color:var(--muted);font-size:12px}.empty{padding:28px;text-align:center;color:var(--muted)}.notify-controls{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:16px}.notify-controls button{padding:8px 11px;border:1px solid var(--border);border-radius:6px;background:var(--panel2);color:var(--text);cursor:pointer}
</style></head><body><div class="container">
<div class="topbar"><div><h1>Уведомления</h1><div class="subtitle">Завершение очереди и результаты с ошибками сохраняются здесь</div></div><div class="nav"><a href="{{ url_for('index') }}">← На главный экран</a><a href="{{ url_for('context_return') }}">← Назад</a><a href="{{ url_for('queue_page') }}">Очередь</a><a href="{{ url_for('queue_history') }}">История</a></div></div>
<div class="notify-controls"><button id="browserNotifyButton" type="button">Разрешить уведомления браузера</button><form method="post" action="{{ url_for('notifications_clear') }}" onsubmit="return confirm('Очистить все уведомления?')"><button class="danger" type="submit">Очистить уведомления</button></form></div>
<div class="muted" style="margin-bottom:15px">Внутренние уведомления сохраняются в SQLite и будут видны после открытия Bridge. Системное уведомление браузера работает только пока страница Bridge открыта.</div>
{% if not notices %}<div class="empty card">Уведомлений пока нет.</div>{% endif %}
{% for n in notices %}<div class="notice card {{ n.kind }}"><div class="notice-title">{{ n.title }}</div><div class="notice-message">{{ n.message }}</div><div class="notice-time">{{ n.created_text }}</div>{% if n.target_url %}<div style="margin-top:10px"><a class="button-link" href="{{ n.target_url }}">{% if n.kind=='error' %}Открыть ошибки{% else %}Открыть запуск{% endif %}</a></div>{% endif %}</div>{% endfor %}
</div>
<script>
const b=document.getElementById('browserNotifyButton');
function refreshButton(){if(!('Notification' in window)){b.disabled=true;b.textContent='Браузерные уведомления не поддерживаются';return}if(Notification.permission==='granted')b.textContent='Уведомления браузера разрешены';else if(Notification.permission==='denied')b.textContent='Уведомления браузера запрещены';else b.textContent='Разрешить уведомления браузера'}
b.addEventListener('click',async()=>{if('Notification' in window){await Notification.requestPermission();refreshButton()}});
refreshButton();
</script></body></html>
"""


# ============================================================
# Имена файлов и обработка дублей
# ============================================================

def truncate_utf8(value, max_bytes):
    """Обрезает имя по байтам UTF-8, а не по символам, сохраняя стабильный хэш."""
    raw = value.encode("utf-8")
    if len(raw) <= max_bytes:
        return value
    suffix = "~" + hashlib.sha1(raw).hexdigest()[:10]
    suffix_bytes = suffix.encode("ascii")
    budget = max(1, max_bytes - len(suffix_bytes))
    prefix = raw[:budget]
    while prefix:
        try:
            text = prefix.decode("utf-8")
            break
        except UnicodeDecodeError:
            prefix = prefix[:-1]
    else:
        text = ""
    text = text.rstrip(" ._")
    return (text or "item") + suffix


def clean_name(value, max_bytes=MAX_TITLE_COMPONENT_BYTES):
    """Готовит безопасный компонент пути с ограничением длины."""
    value = (value or "").strip()
    value = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    value = value or "Неизвестно"
    return truncate_utf8(value, max_bytes)


def parse_size_bytes(value):
    """Преобразует текстовый размер книги в количество байт."""
    text = unicodedata.normalize("NFKC", value or "").lower().replace("\xa0", " ")
    text = text.replace(",", ".")
    m = re.search(r"(\d[\d ]*(?:\.\d+)?)\s*(байт(?:а|ов)?|bytes?|b|кб|kb|kib|мб|mb|mib|гб|gb|gib)?", text)
    if not m:
        return 0
    try:
        number = float(m.group(1).replace(" ", ""))
    except ValueError:
        return 0
    unit = (m.group(2) or "b").lower()
    multipliers = {
        "b": 1, "byte": 1, "bytes": 1, "байт": 1, "байта": 1, "байтов": 1,
        "кб": 1024, "kb": 1024, "kib": 1024,
        "мб": 1024**2, "mb": 1024**2, "mib": 1024**2,
        "гб": 1024**3, "gb": 1024**3, "gib": 1024**3,
    }
    return int(number * multipliers.get(unit, 1))


def parse_download_count(value):
    """Извлекает числовой счётчик скачиваний из текста OPDS."""
    digits = re.sub(r"\D", "", value or "")
    return int(digits) if digits else 0


def normalize_duplicate_title(value):
    """Убирает только известные технические пометки издания, не меняя смысловое название."""
    value = unicodedata.normalize("NFKC", value or "")
    value = re.sub(r"\[[^\]]*(?:litres|литрес|компиляц|фейк)[^\]]*\]", " ", value, flags=re.I)
    return value


def duplicate_key(book):
    """Строит нормализованный ключ для группировки изданий."""
    def norm(value):
        value = unicodedata.normalize("NFKC", value or "").casefold().replace("ё", "е")
        return re.sub(r"[^\w]+", "", value, flags=re.UNICODE)
    authors = [norm(x) for x in re.split(r"\s*,\s*", book.get("author", "")) if x.strip()]
    authors.sort()
    return (
        norm(normalize_duplicate_title(book.get("title", ""))),
        tuple(authors),
        norm(book.get("language", "")),
        norm(book.get("translator", "")),
    )


def technical_title_flags(title):
    """Определяет технические и нежелательные пометки названия."""
    t = unicodedata.normalize("NFKC", title or "").casefold()
    hard_bad = bool(re.search(r"\b(?:компиляц\w*|фейк\w*)\b", t))
    technical = bool(re.search(r"(?:litres|литрес|компиляц|фейк)", t))
    return hard_bad, technical


def metadata_quality(book):
    """Оценивает полноту метаданных для выбора лучшего издания."""
    score = 0
    author = (book.get("author") or "").casefold()
    if author and "неизвест" not in author: score += 3
    if book.get("language"): score += 2
    if book.get("translator"): score += 1
    score += min(3, len(book.get("genres") or []))
    if book.get("series_links"): score += 1
    if book.get("cover_href"): score += 2
    if book.get("epub") and book.get("fb2"): score += 1
    return score


def duplicate_score(book):
    """Рейтинг предпочтительного издания. Размер — только последний значимый критерий."""
    hard_bad, technical = technical_title_flags(book.get("title", ""))
    return (
        0 if hard_bad else 1,
        0 if technical else 1,
        metadata_quality(book),
        1 if book.get("cover_href") else 0,
        parse_download_count(book.get("downloads", "")),
        int(bool(book.get("epub"))) + int(bool(book.get("fb2"))),
        book.get("size_bytes", 0),
        duplicate_id_tiebreak(book.get("id", "")),
    )


def duplicate_id_tiebreak(book_id):
    """Возвращает стабильный строковый tie-break для любого source item ID."""
    value = str(book_id or "")
    if value.isdecimal():
        normalized_digits = value.lstrip("0") or "0"
        return (1, len(normalized_digits), normalized_digits, value)
    return (0, 0, "", value)


def download_filename_identity_marker(source_id, source_item_id):
    """Возвращает стабильный source-aware marker без raw identity в имени файла."""
    payload = json.dumps(
        [str(source_id or ""), str(source_item_id or "")],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
    return f"[opds-{digest}]"


def legacy_duplicate_storage_title(book):
    """Воспроизводит прежнее имя альтернативного дубля только для lookup."""
    if book.get("duplicate_count", 1) > 1 and not book.get("duplicate_preferred", True):
        return f"{book.get('title','Без названия')} [flibusta-{book.get('id','unknown')}]"
    return book.get("title", "Без названия")


def duplicate_storage_title(book):
    """Для альтернативного дубля добавляет нейтральный source-aware marker."""
    if book.get("duplicate_count", 1) > 1 and not book.get("duplicate_preferred", True):
        marker = download_filename_identity_marker(
            book.get("source_id") or LEGACY_QUEUE_SOURCE_ID,
            book.get("id", "unknown"),
        )
        return f"{book.get('title','Без названия')} {marker}"
    return book.get("title", "Без названия")


def duplicate_storage_title_candidates(book):
    """Возвращает primary и legacy basename для точной проверки существующих файлов."""
    primary = duplicate_storage_title(book)
    if book.get("duplicate_count", 1) <= 1 or book.get("duplicate_preferred", True):
        return (primary,)
    # Старый marker source-unaware, поэтому transitional fallback нужен всем источникам.
    legacy = legacy_duplicate_storage_title(book)
    return tuple(dict.fromkeys((primary, legacy)))


def apply_duplicate_local_status(book):
    """Обновляет наличие локальных файлов конкретного дубля."""
    paths = [
        local_paths(book["author"], title)
        for title in duplicate_storage_title_candidates(book)
    ]
    book["duplicate_exists_epub"] = any(os.path.isfile(p["epub"]) for p in paths)
    book["duplicate_exists_fb2"] = any(os.path.isfile(p["fb2"]) for p in paths)
    book["duplicate_exists_any"] = book["duplicate_exists_epub"] or book["duplicate_exists_fb2"]
    return book


def annotate_duplicates(books):
    """Группирует дубли и отмечает предпочтительные издания."""
    groups = {}
    for book in books:
        book["size_bytes"] = parse_size_bytes(book.get("size", ""))
        book["duplicate_count"] = 1
        book["duplicate_preferred"] = True
        book["duplicate_group"] = book.get("id", "")
        book["duplicate_exists_epub"] = book.get("exists_epub", False)
        book["duplicate_exists_fb2"] = book.get("exists_fb2", False)
        book["duplicate_exists_any"] = book.get("exists_any", False)
        groups.setdefault(duplicate_key(book), []).append(book)
    duplicate_groups = 0
    duplicate_extra = 0
    for group in groups.values():
        preferred = max(group, key=duplicate_score)
        if len(group) > 1:
            duplicate_groups += 1
            duplicate_extra += len(group) - 1
        for book in group:
            book["duplicate_count"] = len(group)
            book["duplicate_preferred"] = book["id"] == preferred["id"]
            book["duplicate_group"] = preferred["id"]
            apply_duplicate_local_status(book)
    return duplicate_groups, duplicate_extra


def select_books_for_job(all_books, selected_ids, download_duplicates=False):
    """Формирует очередь без уже существующих файлов.

    Возвращает: queue, duplicates_filtered, selected_count, existing_filtered.
    При выключенных дублях выбранная группа всегда сводится к предпочтительному изданию.
    """
    selected = [b for b in all_books if b["id"] in selected_ids]
    selected_count = len(selected)
    if not selected:
        return [], 0, 0, 0

    if download_duplicates:
        candidates = selected
        duplicates_filtered = 0
        for book in candidates:
            apply_duplicate_local_status(book)
        queue = [b for b in candidates if not b.get("duplicate_exists_any", False)]
        existing_filtered = len(candidates) - len(queue)
        return queue, duplicates_filtered, selected_count, existing_filtered

    all_groups = {}
    for book in all_books:
        all_groups.setdefault(duplicate_key(book), []).append(book)
    selected_keys = []
    seen = set()
    for book in selected:
        key = duplicate_key(book)
        if key not in seen:
            seen.add(key)
            selected_keys.append(key)
    candidates = [max(all_groups[key], key=duplicate_score) for key in selected_keys]
    duplicates_filtered = max(0, selected_count - len(candidates))
    for book in candidates:
        apply_local_status(book)
    queue = [b for b in candidates if not b.get("exists_any", False)]
    existing_filtered = len(candidates) - len(queue)
    return queue, duplicates_filtered, selected_count, existing_filtered

# ============================================================
# Работа с Flibusta OPDS
# ============================================================

def display_text(value, fallback="Без названия"):
    """Возвращает очищенный текст или запасную подпись."""
    value = re.sub(r"[\x00-\x1f]", " ", (value or "")).strip()
    value = re.sub(r"\s+", " ", value)
    return (value or fallback)[:300]


def allowed_legacy_opds_url(url):
    """Разрешает запрос только к ожидаемому HTTPS-хосту Flibusta."""
    return url == LEGACY_OPDS_BASE or url.startswith(LEGACY_OPDS_BASE + "/")


def legacy_opds_get(url, retry_attempts=None, retry_delay=None, **kwargs):
    """HTTP GET к Флибусте.

    По умолчанию используется общий retry для OPDS/обложек. Для загрузки книг
    save_epub/save_fb2 передают retry_attempts=1 и управляют повторами сами,
    чтобы один запрос не получал два вложенных цикла повторов.
    """
    if not allowed_legacy_opds_url(url):
        raise RuntimeError("Запрещённый URL")
    kwargs.setdefault("timeout", TIMEOUT)
    headers = kwargs.pop("headers", {})
    headers.setdefault("User-Agent", "OPDS-Desk/1.0")
    attempts = RETRY_ATTEMPTS if retry_attempts is None else max(1, int(retry_attempts))
    delay_base = RETRY_DELAY if retry_delay is None else max(0, float(retry_delay))
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            r = requests.get(url, headers=headers, **kwargs)
            r.raise_for_status()
            return r
        except requests.RequestException as exc:
            last_error = exc
            if attempt >= attempts:
                break
            time.sleep(delay_base * attempt)
    raise last_error


def compact_error(exc, limit=180):
    """Сокращает текст исключения для интерфейса и журнала."""
    text = re.sub(r"\s+", " ", str(exc or "Неизвестная ошибка")).strip()
    if len(text) > limit:
        text = text[:limit - 1].rstrip() + "…"
    return text


def emit_download_progress(progress, detail, attempt=0, attempt_max=0, file_format="", stage="", elapsed=0.0):
    """Передаёт callback текущее состояние загрузки книги."""
    if not progress:
        return
    progress({
        "detail": detail,
        "attempt": int(attempt or 0),
        "attempt_max": int(attempt_max or 0),
        "format": (file_format or "").upper(),
        "stage": stage or "",
        "elapsed": float(elapsed or 0.0),
    })


def parse_book_id(href):
    """Извлекает идентификатор книги из OPDS-ссылки."""
    m = re.search(r"/b/(\d+)/(?:fb2|epub)$", href or "")
    return m.group(1) if m else None


def parse_content_info(entry):
    """Читает размер и число скачиваний из OPDS entry."""
    node = entry.find("atom:content", NS)
    if node is None or not node.text:
        return {"translator": "", "size": "", "downloads": ""}
    text = html.unescape(node.text)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    def grab(pattern):
        m = re.search(pattern, text, flags=re.I)
        return m.group(1).strip() if m else ""
    return {"translator": grab(r"Перевод:\s*([^\n]+)"), "size": grab(r"Размер:\s*([^\n]+)"), "downloads": grab(r"Скачиваний:\s*([^\n]+)")}


def parse_related_links(entry):
    """Разбирает авторов и серии, связанные с OPDS-записью."""
    authors, series, seen_a, seen_s = [], [], set(), set()
    for link in entry.findall("atom:link", NS):
        if link.get("rel") != "related":
            continue
        href, title = link.get("href") or "", link.get("title") or ""
        m = re.fullmatch(r"/opds/author/(\d+)", href)
        if m:
            aid = m.group(1)
            n = re.search(r"Все книги автора\s+(.+)", title)
            name = display_text(n.group(1) if n else f"Автор {aid}")
            if aid not in seen_a:
                seen_a.add(aid); authors.append({"id": aid, "name": name})
            continue
        m = re.fullmatch(r"/opds/sequencebooks/(\d+)", href)
        if m:
            sid = m.group(1)
            n = re.search(r'Все книги серии\s+"(.+)"', title)
            name = display_text(n.group(1) if n else f"Серия {sid}")
            if sid not in seen_s:
                seen_s.add(sid); series.append({"id": sid, "name": name})
    return authors, series


def local_paths(author, title):
    """Формирует пути EPUB и FB2 внутри выбранной библиотеки."""
    # ext4 ограничивает один компонент имени 255 байт. Оставляем запас под расширение и .part.
    author_dir = os.path.join(DESTINATION, clean_name(author, MAX_AUTHOR_COMPONENT_BYTES))
    base = clean_name(title, MAX_TITLE_COMPONENT_BYTES)
    return {"epub": os.path.join(author_dir, base + ".epub"), "fb2": os.path.join(author_dir, base + ".fb2")}


def apply_local_status(book):
    """Отмечает форматы книги, уже сохранённые локально."""
    p = local_paths(book["author"], book["title"])
    book["exists_epub"] = os.path.isfile(p["epub"])
    book["exists_fb2"] = os.path.isfile(p["fb2"])
    book["exists_any"] = book["exists_epub"] or book["exists_fb2"]
    return book


def parse_entry(entry):
    """Преобразует XML entry OPDS в словарь книги."""
    title = display_text(entry.findtext("atom:title", default="", namespaces=NS))
    names = []
    for node in entry.findall("atom:author/atom:name", NS):
        if node.text:
            name = display_text(node.text, "Неизвестный автор")
            if name not in names: names.append(name)
    author = ", ".join(names) if names else "Неизвестный автор"
    language = display_text(entry.findtext("dc:language", default="", namespaces=NS), "")
    genres = []
    for c in entry.findall("atom:category", NS):
        genre = display_text(c.get("label") or c.get("term") or "", "")
        if genre and genre not in genres: genres.append(genre)
    author_links, series_links = parse_related_links(entry)
    extra = parse_content_info(entry)
    book_id, has_fb2, has_epub, cover_href, web_url = None, False, False, "", ""
    for link in entry.findall("atom:link", NS):
        rel, mime, href = link.get("rel") or "", link.get("type") or "", link.get("href") or ""
        if rel == OPDS_ACQUISITION:
            candidate = parse_book_id(href)
            if candidate: book_id = candidate
            if mime == "application/fb2+zip": has_fb2 = True
            elif mime == "application/epub+zip": has_epub = True
        if not cover_href and (href.startswith("/i/") or href.startswith("/ia/")) and ("image" in rel or "thumbnail" in rel):
            cover_href = href
        if rel == "alternate" and mime == "text/html" and href.startswith("/b/"):
            web_url = urljoin(LEGACY_OPDS_BASE, href)
    if not book_id:
        return None
    return apply_local_status({"id": book_id, "title": title, "author": author, "language": language, "genres": genres, "author_links": author_links, "series_links": series_links, "translator": extra["translator"], "size": extra["size"], "size_bytes": parse_size_bytes(extra["size"]), "downloads": extra["downloads"], "fb2": has_fb2, "epub": has_epub, "cover_href": cover_href, "web_url": web_url, "duplicate_count": 1, "duplicate_preferred": True})


def parse_feed_books(root):
    """Возвращает распознанные книги из корня OPDS-ленты."""
    result = []
    for entry in root.findall("atom:entry", NS):
        book = parse_entry(entry)
        if book: result.append(book)
    return result


def find_next_href(root):
    """Находит ссылку next в OPDS-ленте, если она объявлена."""
    for link in root.findall("atom:link", NS):
        if link.get("rel") == "next" and link.get("href"):
            return link.get("href")
    return None



def normalize_author_text(value):
    """Нормализация для локального сопоставления имён авторов.

    ВАЖНО: дефис внутри имени/фамилии сохраняется как часть одного токена.
    Поэтому запрос "Жюль Верн" даёт токены ["жюль", "верн"], а кандидат
    "Жюль-Верн Жан" — ["жюль-верн", "жан"] и больше не считается
    совпадением по двум отдельным словам.
    """
    value = unicodedata.normalize("NFKC", value or "").casefold().replace("ё", "е")
    # Приводим распространённые Unicode-варианты дефиса к обычному '-'.
    value = re.sub(r"[‐‑‒–—―−]", "-", value)
    # Слово может содержать внутренний дефис, но не начинается/заканчивается им.
    tokens = re.findall(r"[^\W_]+(?:-[^\W_]+)*", value, flags=re.UNICODE)
    return " ".join(tokens)


# ============================================================
# Каталоги, пагинация и кэш
# ============================================================

def catalog_start_url(kind, catalog_id):
    """Формирует начальный OPDS URL каталога автора или серии."""
    if kind == "author": return f"{LEGACY_OPDS_BASE}/opds/author/{catalog_id}/alphabet"
    if kind == "series": return f"{LEGACY_OPDS_BASE}/opds/sequencebooks/{catalog_id}"
    raise RuntimeError("Неизвестный тип каталога")


def catalog_page_cache_key(kind, catalog_id, page):
    """Строит ключ конкретной страницы каталога."""
    return (
        catalog_cache_key(current_source_id(), str(kind), str(catalog_id)),
        max(0, int(page)),
    )


def prepare_catalog_page_book(book, refresh_local=False):
    """Добавляет книге безопасные UI-поля постраничного каталога."""
    if refresh_local:
        apply_local_status(book)
    book["duplicate_group"] = book["id"]
    book["duplicate_count"] = 1
    book["duplicate_preferred"] = True
    book["duplicate_exists_epub"] = book.get("exists_epub", False)
    book["duplicate_exists_fb2"] = book.get("exists_fb2", False)
    book["duplicate_exists_any"] = book.get("exists_any", False)
    return book


def cached_catalog_page(kind, catalog_id, page):
    """Возвращает свежую страницу каталога из памяти."""
    key = catalog_page_cache_key(kind, catalog_id, page)
    with catalog_page_cache_lock:
        cached = catalog_page_cache.get(key)
        if not cached or time.time() - cached["time"] >= CATALOG_CACHE_TTL:
            return None
    for book in cached["books"]:
        prepare_catalog_page_book(book, refresh_local=True)
    return cached


def registered_catalog_page_cache_key(ref, token, page):
    """Строит ключ neutral page cache из snapshot CatalogRef."""
    if not isinstance(ref, CatalogRef):
        raise TypeError("ref должен быть CatalogRef")
    try:
        page = max(0, int(page))
    except (TypeError, ValueError) as exc:
        raise ValueError("Некорректный номер страницы") from exc
    if page >= MAX_CATALOG_PAGES:
        raise RuntimeError("Превышен лимит страниц каталога")
    return (
        catalog_cache_key(ref.source_id, "opds", token),
        page,
    )


def cached_registered_catalog_page(ref, token, page):
    """Возвращает безопасную копию свежей neutral catalog page."""
    key = registered_catalog_page_cache_key(ref, token, page)
    with catalog_page_cache_lock:
        cached = catalog_page_cache.get(key)
        if not cached or time.time() - cached["time"] >= CATALOG_CACHE_TTL:
            return None
        result = copy.deepcopy(cached)
    for book in result["books"]:
        prepare_catalog_page_book(book, refresh_local=True)
    return result


def _empty_registered_catalog_page(ref, page, title=""):
    """Возвращает незакэшированный результат после конца OPDS-цепочки."""
    return {
        "title": title or ref.title,
        "books": [],
        "page": page,
        "has_next": False,
        "page_url": "",
        "requested_url": "",
        "next_url": "",
        "navigation": (),
    }


def load_registered_catalog_page(token, page=0, force=False, client=None):
    """Загружает neutral catalog page по реальной цепочке OPDS next."""
    try:
        page = max(0, int(page))
    except (TypeError, ValueError) as exc:
        raise ValueError("Некорректный номер страницы") from exc
    if page >= MAX_CATALOG_PAGES:
        raise RuntimeError("Превышен лимит страниц каталога")

    ref = get_current_catalog_ref(token)
    if ref is None:
        raise ValueError("OPDS-каталог недоступен или устарел")
    target_key = registered_catalog_page_cache_key(ref, token, page)
    if not force:
        cached = cached_registered_catalog_page(ref, token, page)
        if cached:
            return cached

    known_pages = {}
    for known_page in range(page):
        cached = cached_registered_catalog_page(ref, token, known_page)
        if cached:
            known_pages[known_page] = cached

    last_title = ref.title
    seen_urls = set()
    if known_pages:
        predecessor_page = max(known_pages)
        predecessor = known_pages[predecessor_page]
        current_page = predecessor_page + 1
        current_url = predecessor.get("next_url", "")
        last_title = predecessor.get("title") or last_title
        seen_urls.add(ref.url)
        for known_page, known in known_pages.items():
            if known_page <= predecessor_page and known.get("page_url"):
                seen_urls.add(known["page_url"])
        if not current_url or current_url in seen_urls:
            return _empty_registered_catalog_page(ref, page, last_title)
    else:
        current_page = 0
        current_url = ref.url

    while current_page <= page:
        if current_url in seen_urls:
            return _empty_registered_catalog_page(ref, page, last_title)
        seen_before_request = set(seen_urls)
        seen_urls.add(current_url)

        loaded = load_opds_catalog_page(
            current_url,
            source_id=ref.source_id,
            client=client,
        )
        current_ref = get_current_catalog_ref(token)
        if current_ref is None or current_ref.source_id != ref.source_id:
            raise ValueError("OPDS-источник изменён во время загрузки")
        if loaded.final_url in seen_before_request:
            return _empty_registered_catalog_page(ref, page, last_title)
        seen_urls.add(loaded.final_url)

        books = [
            prepare_catalog_page_book(copy.deepcopy(book))
            for book in loaded.books
        ]
        navigation_refs = tuple(
            navigation_ref
            for navigation_ref in loaded.navigation
            if isinstance(navigation_ref, CatalogRef)
            and navigation_ref.source_id == ref.source_id
        )
        registered_navigation = register_catalog_navigation(navigation_refs)
        current_ref = get_current_catalog_ref(token)
        if current_ref is None or current_ref.source_id != ref.source_id:
            raise ValueError("OPDS-источник изменён во время загрузки")

        payload = {
            "title": loaded.title,
            "books": books,
            "page": current_page,
            "has_next": bool(loaded.next_url),
            "page_url": loaded.final_url,
            "requested_url": loaded.requested_url,
            "next_url": loaded.next_url,
            "navigation": registered_navigation,
            "time": time.time(),
        }
        cache_key = registered_catalog_page_cache_key(ref, token, current_page)
        with catalog_page_cache_lock:
            catalog_page_cache[cache_key] = copy.deepcopy(payload)
            if force and current_page == page:
                cache_prefix = target_key[0]
                downstream_keys = [
                    key
                    for key in catalog_page_cache
                    if isinstance(key, tuple)
                    and len(key) == 2
                    and key[0] == cache_prefix
                    and key[1] > page
                ]
                for key in downstream_keys:
                    catalog_page_cache.pop(key, None)

        last_title = loaded.title or last_title
        if current_page == page:
            return payload
        if not loaded.next_url or loaded.next_url in seen_urls:
            return _empty_registered_catalog_page(ref, page, last_title)
        current_page += 1
        current_url = loaded.next_url


def resolve_preferred_registered_catalog_token(token, client=None):
    """Одним hop разрешает однозначный author-related navigation root."""
    ref = get_current_catalog_ref(token)
    if ref is None:
        raise ValueError("OPDS-каталог недоступен или устарел")
    if ref.kind != "related" or not is_author_related_catalog_title(ref.title):
        return token

    root_page = load_registered_catalog_page(
        token,
        page=0,
        client=client,
    )
    has_downloadable_books = any(
        catalog_book_has_downloadable_acquisition(book)
        for book in root_page.get("books", ())
    )
    if has_downloadable_books or root_page.get("has_next", False):
        return token

    navigation = root_page.get("navigation", ())
    if not navigation:
        return token
    child = select_preferred_registered_catalog_child(navigation)
    if child is None or child.token == token:
        return token

    child_page = load_registered_catalog_page(
        child.token,
        page=0,
        client=client,
    )
    has_downloadable_books = any(
        catalog_book_has_downloadable_acquisition(book)
        for book in child_page.get("books", ())
    )
    return child.token if has_downloadable_books else token


def load_catalog_page(kind, catalog_id, page=0, force=False):
    """Загружает страницу по сохранённой цепочке OPDS next URL.

    Уже известные страницы не скачиваются повторно; при прямом переходе
    недостающая часть цепочки достраивается до запрошенного номера.
    """
    page = max(0, int(page))
    if page >= MAX_CATALOG_PAGES:
        raise RuntimeError("Превышен лимит страниц каталога")
    if not force:
        cached = cached_catalog_page(kind, catalog_id, page)
        if cached:
            return cached

    current_page = 0
    current_url = catalog_start_url(kind, catalog_id)
    for known_page in range(page - 1, -1, -1):
        known = cached_catalog_page(kind, catalog_id, known_page)
        if not known:
            continue
        if not known.get("next_url"):
            return {
                "title": "",
                "books": [],
                "page": page,
                "has_next": False,
                "page_url": known["page_url"],
                "next_url": "",
                "time": time.time(),
            }
        current_page = known_page + 1
        current_url = known["next_url"]
        break

    seen_urls = set()
    while current_page <= page:
        if current_page != page or not force:
            cached = cached_catalog_page(kind, catalog_id, current_page)
            if cached:
                if current_page == page:
                    return cached
                next_url = cached.get("next_url", "")
                if not next_url:
                    return {
                        "title": "",
                        "books": [],
                        "page": page,
                        "has_next": False,
                        "page_url": cached["page_url"],
                        "next_url": "",
                        "time": time.time(),
                    }
                current_url = next_url
                current_page += 1
                continue

        if current_url in seen_urls:
            raise RuntimeError("Обнаружен цикл в OPDS-пагинации")
        seen_urls.add(current_url)
        root = ET.fromstring(legacy_opds_get(current_url).content)
        feed_title = display_text(root.findtext("atom:title", default="", namespaces=NS), "")
        books = []
        for entry in root.findall("atom:entry", NS):
            book = parse_entry(entry)
            if book:
                books.append(prepare_catalog_page_book(book))
        next_href = find_next_href(root)
        next_url = urljoin(LEGACY_OPDS_BASE, next_href) if next_href else ""
        if next_url and not allowed_legacy_opds_url(next_url):
            raise RuntimeError("Некорректная OPDS-ссылка")
        payload = {
            "title": feed_title,
            "books": books,
            "page": current_page,
            "has_next": bool(next_url),
            "page_url": current_url,
            "next_url": next_url,
            "time": time.time(),
        }
        with catalog_page_cache_lock:
            catalog_page_cache[catalog_page_cache_key(kind, catalog_id, current_page)] = payload
        if current_page == page:
            return payload
        if not next_url:
            return {
                "title": "",
                "books": [],
                "page": page,
                "has_next": False,
                "page_url": current_url,
                "next_url": "",
                "time": time.time(),
            }
        current_page += 1
        current_url = next_url



def get_cached_catalog_page(kind, catalog_id, page=0, force=False):
    """Возвращает страницу каталога с обновлённым локальным статусом."""
    page = max(0, int(page))
    if page >= MAX_CATALOG_PAGES:
        raise RuntimeError("Превышен лимит страниц каталога")
    if not force:
        cached = cached_catalog_page(kind, catalog_id, page)
        if cached:
            return cached
    return load_catalog_page(kind, catalog_id, page, force=force)


def registered_catalog_full_cache_key(ref, token):
    """Строит full-cache key из snapshot нейтрального CatalogRef."""
    if not isinstance(ref, CatalogRef):
        raise TypeError("ref должен быть CatalogRef")
    return catalog_cache_key(ref.source_id, "opds", token)


def collect_registered_catalog(token, force=False, client=None):
    """Собирает полный neutral catalog через registered page loader."""
    ref = get_current_catalog_ref(token)
    if ref is None:
        raise ValueError("OPDS-каталог недоступен или устарел")
    cache_key = registered_catalog_full_cache_key(ref, token)
    now = time.time()

    if not force:
        with catalog_lock:
            cached = catalog_cache.get(cache_key)
            if cached and now - cached["time"] < CATALOG_CACHE_TTL:
                result = copy.deepcopy(cached["result"])
            else:
                result = None
        if result is not None:
            for book in result["books"]:
                prepare_catalog_page_book(book, refresh_local=True)
            duplicate_groups, duplicate_extra = annotate_duplicates(result["books"])
            result["duplicate_groups"] = duplicate_groups
            result["duplicate_extra"] = duplicate_extra
            return result

    books_by_id = {}
    feed_title = ""
    pages = 0
    last_result = None
    for page in range(MAX_CATALOG_PAGES):
        page_result = load_registered_catalog_page(
            token,
            page=page,
            force=bool(force and page == 0),
            client=client,
        )
        if "page_url" in page_result and not page_result.get("page_url"):
            break
        if pages == 0:
            feed_title = page_result.get("title") or ref.title
        pages += 1
        for book in page_result.get("books", []):
            books_by_id.setdefault(book.get("id"), copy.deepcopy(book))
        last_result = page_result
        if not page_result.get("has_next", False):
            break
    else:
        if last_result and last_result.get("has_next", False):
            raise RuntimeError("Превышен лимит страниц каталога")

    current_ref = get_current_catalog_ref(token)
    if current_ref is None or current_ref.source_id != ref.source_id:
        raise ValueError("OPDS-источник изменён во время загрузки")

    books = list(books_by_id.values())
    for book in books:
        prepare_catalog_page_book(book, refresh_local=True)
    duplicate_groups, duplicate_extra = annotate_duplicates(books)
    collected_at = time.time()
    result = {
        "title": feed_title or ref.title,
        "books": books,
        "pages": pages,
        "duplicate_groups": duplicate_groups,
        "duplicate_extra": duplicate_extra,
        "time": collected_at,
    }

    current_ref = get_current_catalog_ref(token)
    if current_ref is None or current_ref.source_id != ref.source_id:
        raise ValueError("OPDS-источник изменён во время загрузки")
    with catalog_lock:
        catalog_cache[cache_key] = {
            "time": collected_at,
            "result": copy.deepcopy(result),
        }
    return result


def catalog_book_to_readonly_view(book):
    """Оставляет только безопасные метаданные compatibility book."""
    if not isinstance(book, dict):
        raise ValueError("Ожидается словарь книги")
    authors = tuple(str(value) for value in (book.get("authors") or ()))
    author = str(book.get("author") or ", ".join(authors) or "Неизвестный автор")
    formats = tuple(
        name
        for name, available in (
            ("EPUB", book.get("epub")),
            ("FB2", book.get("fb2")),
        )
        if available
    )
    return RegisteredCatalogBookView(
        id=str(book.get("id") or ""),
        title=str(book.get("title") or "Без названия"),
        author=author,
        authors=authors,
        language=str(book.get("language") or ""),
        genres=tuple(str(value) for value in (book.get("genres") or ())),
        formats=formats,
        translator=str(book.get("translator") or ""),
        size=str(book.get("size") or ""),
        has_cover=bool(book.get("cover_url") or book.get("thumbnail_url")),
        related=register_catalog_refs(book.get("related") or ()),
    )


def build_opds_search_view(search_page, query, page=0):
    """Преобразует уже загруженную OPDS search page в безопасную view model."""
    if not isinstance(search_page, OPDSCatalogPage):
        raise ValueError("Ожидается OPDSCatalogPage")
    normalized_query = normalize_opds_search_query(query)
    page = _validate_opds_search_page_number(page)
    title = str(search_page.title or "").strip() or "Результаты поиска"
    registered_views = []
    for book in search_page.books:
        register_opds_search_book(
            search_page.source_id,
            normalized_query,
            book,
        )
        registered_views.append(catalog_book_to_readonly_view(book))
    return OPDSSearchView(
        query=normalized_query,
        books=tuple(registered_views),
        page=page,
        has_previous=page > 0,
        has_next=bool(search_page.next_url),
        title=title,
        total_results=search_page.total_results,
    )


def build_registered_catalog_view(token, page=0, view_all=False, client=None):
    """Строит read-only view без возможностей загрузки или очереди."""
    ref = get_current_catalog_ref(token)
    if ref is None:
        raise ValueError("OPDS-каталог недоступен или устарел")

    if view_all:
        result = collect_registered_catalog(token, client=client)
        return RegisteredCatalogView(
            token=token,
            title=result.get("title") or ref.title,
            books=tuple(
                catalog_book_to_readonly_view(book)
                for book in result.get("books", ())
            ),
            page=0,
            pages=int(result.get("pages") or 0),
            has_previous=False,
            has_next=False,
            view_all=True,
            navigation=(),
        )

    result = load_registered_catalog_page(token, page=page, client=client)
    result_page = int(result.get("page") or 0)
    navigation = tuple(
        item
        for item in result.get("navigation", ())
        if isinstance(item, RegisteredCatalogRef)
    )
    return RegisteredCatalogView(
        token=token,
        title=result.get("title") or ref.title,
        books=tuple(
            catalog_book_to_readonly_view(book)
            for book in result.get("books", ())
        ),
        page=result_page,
        pages=result_page + 1,
        has_previous=result_page > 0,
        has_next=bool(result.get("has_next", False)),
        view_all=False,
        navigation=navigation,
    )


def collect_catalog(kind, catalog_id):
    """Последовательно собирает полный каталог по ссылкам next."""
    url = catalog_start_url(kind, catalog_id)
    books_by_id, seen_urls, feed_title, pages = {}, set(), "", 0
    while url:
        if url in seen_urls: raise RuntimeError("Обнаружен цикл в OPDS-пагинации")
        seen_urls.add(url); pages += 1
        if pages > MAX_CATALOG_PAGES: raise RuntimeError("Превышен лимит страниц каталога")
        root = ET.fromstring(legacy_opds_get(url).content)
        if not feed_title: feed_title = display_text(root.findtext("atom:title", default="", namespaces=NS), "")
        for book in parse_feed_books(root): books_by_id.setdefault(book["id"], book)
        next_href = find_next_href(root)
        if not next_href: break
        url = urljoin(LEGACY_OPDS_BASE, next_href)
        if not allowed_legacy_opds_url(url): raise RuntimeError("Некорректная OPDS-ссылка")
    return {"title": feed_title, "books": list(books_by_id.values()), "pages": pages}


def get_cached_catalog(kind, catalog_id, force=False):
    """Возвращает полный каталог из кэша или загружает его заново."""
    key = catalog_cache_key(current_source_id(), str(kind), str(catalog_id))
    now = time.time()
    if not force:
        with catalog_lock:
            cached = catalog_cache.get(key)
            if cached and now - cached["time"] < CATALOG_CACHE_TTL:
                for b in cached["result"]["books"]: apply_local_status(b)
                duplicate_groups, duplicate_extra = annotate_duplicates(cached["result"]["books"])
                cached["result"]["duplicate_groups"] = duplicate_groups
                cached["result"]["duplicate_extra"] = duplicate_extra
                return cached["result"]
    result = collect_catalog(kind, catalog_id)
    duplicate_groups, duplicate_extra = annotate_duplicates(result["books"])
    result["duplicate_groups"] = duplicate_groups
    result["duplicate_extra"] = duplicate_extra
    with catalog_lock: catalog_cache[key] = {"time": now, "result": result}
    return result



# ============================================================
# Загрузка и проверка книг
# ============================================================

class DownloadValidationError(RuntimeError):
    """Скачанный файл получен, но не прошёл структурную проверку."""


def download_error_info(exc):
    """Возвращает стабильную категорию ошибки и признак осмысленного retry."""
    message = compact_error(exc, 300)
    low = message.lower()

    if isinstance(exc, requests.Timeout):
        return {"code": "timeout", "label": "Таймаут", "retryable": True}
    if isinstance(exc, requests.ConnectionError):
        return {"code": "network", "label": "Ошибка сети", "retryable": True}
    if isinstance(exc, requests.HTTPError):
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        if status == 404:
            return {"code": "http_404", "label": "Не найдено (HTTP 404)", "retryable": False}
        if status == 429:
            return {"code": "http_429", "label": "Ограничение запросов (HTTP 429)", "retryable": True}
        if status is not None and 500 <= int(status) <= 599:
            return {"code": f"http_{status}", "label": f"Ошибка OPDS-источника (HTTP {status})", "retryable": True}
        if status is not None:
            return {"code": f"http_{status}", "label": f"HTTP {status}", "retryable": False}
        return {"code": "http", "label": "HTTP-ошибка", "retryable": True}
    if isinstance(exc, PermissionError):
        return {"code": "permission", "label": "Нет доступа к файлу", "retryable": False}
    if isinstance(exc, OSError) and getattr(exc, "errno", None) == 28:
        return {"code": "disk_full", "label": "Недостаточно места", "retryable": False}
    if isinstance(exc, (DownloadValidationError, zipfile.BadZipFile, ET.ParseError)):
        return {"code": "invalid_file", "label": "Проверка файла", "retryable": True}
    if "нет подходящего формата" in low:
        return {"code": "no_format", "label": "Нет подходящего формата", "retryable": False}
    if "превышает допустимый размер" in low:
        return {"code": "too_large", "label": "Файл слишком большой", "retryable": False}
    if any(token in low for token in ("некоррект", "не похож", "не найден fb2", "epub", "fictionbook", "контейнер epub")):
        return {"code": "invalid_file", "label": "Проверка файла", "retryable": True}
    return {"code": "download", "label": "Ошибка загрузки", "retryable": True}


def validate_epub_file(path):
    """Проверяет ZIP-структуру EPUB и корневой package-файл до публикации книги."""
    if not os.path.isfile(path):
        raise DownloadValidationError("EPUB-файл не создан")
    size = os.path.getsize(path)
    if size < 64:
        raise DownloadValidationError("EPUB-файл пустой или слишком маленький")
    if size > MAX_DOWNLOAD_SIZE:
        raise DownloadValidationError("EPUB превышает допустимый размер")
    if not zipfile.is_zipfile(path):
        raise DownloadValidationError("Сервер вернул не EPUB: файл не является ZIP")

    try:
        with zipfile.ZipFile(path) as z:
            bad = z.testzip()
            if bad:
                raise DownloadValidationError(f"EPUB повреждён: ошибка CRC в {bad}")
            names = set(z.namelist())
            if "mimetype" not in names:
                raise DownloadValidationError("EPUB: отсутствует mimetype")
            mimetype = z.read("mimetype").strip()
            if mimetype != b"application/epub+zip":
                raise DownloadValidationError("EPUB: неверный mimetype")
            container_name = "META-INF/container.xml"
            if container_name not in names:
                raise DownloadValidationError("EPUB: отсутствует META-INF/container.xml")
            try:
                container_root = ET.fromstring(z.read(container_name))
            except ET.ParseError as exc:
                raise DownloadValidationError(f"EPUB: повреждён container.xml: {compact_error(exc)}") from exc

            package_path = ""
            for node in container_root.iter():
                if str(node.tag).split("}")[-1] == "rootfile":
                    package_path = (node.attrib.get("full-path") or "").strip()
                    if package_path:
                        break
            if not package_path:
                raise DownloadValidationError("EPUB: в container.xml не указан package-файл")
            if package_path not in names:
                raise DownloadValidationError(f"EPUB: package-файл отсутствует: {package_path}")
            try:
                package_root = ET.fromstring(z.read(package_path))
            except ET.ParseError as exc:
                raise DownloadValidationError(f"EPUB: повреждён package-файл: {compact_error(exc)}") from exc
            if str(package_root.tag).split("}")[-1].lower() != "package":
                raise DownloadValidationError("EPUB: корневой OPF-элемент не package")
    except zipfile.BadZipFile as exc:
        raise DownloadValidationError(f"EPUB ZIP повреждён: {compact_error(exc)}") from exc
    return True


def validate_fb2_file(path):
    """Проверяет XML FB2 до атомарного переименования .part в конечный файл."""
    if not os.path.isfile(path):
        raise DownloadValidationError("FB2-файл не создан")
    size = os.path.getsize(path)
    if size < 64:
        raise DownloadValidationError("FB2-файл пустой или слишком маленький")
    if size > MAX_DOWNLOAD_SIZE:
        raise DownloadValidationError("FB2 превышает допустимый размер")
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise DownloadValidationError(f"FB2 содержит некорректный XML: {compact_error(exc)}") from exc
    if str(root.tag).split("}")[-1] != "FictionBook":
        raise DownloadValidationError("FB2: корневой элемент не FictionBook")
    return True


def save_opds_acquisition(
    url,
    destination,
    file_format,
    mime_type="",
    progress=None,
    session=None,
):
    """Стримит и проверяет файл по фактическому acquisition URL нейтрального OPDS."""
    if file_format not in {"epub", "fb2"}:
        raise ValueError(f"Неподдерживаемый формат OPDS acquisition: {file_format}")
    normalized_url = normalize_opds_url(url)
    destination_path = os.fsdecode(os.fspath(destination))
    download_artifact = destination_path + ".opds-download.part"
    format_label = file_format.upper()
    owns_session = session is None
    http_session = requests.Session() if owns_session else session
    last_error = None

    def cleanup_attempt():
        for path in (destination_path, download_artifact):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass

    def stream_response(response, path):
        raw_length = (getattr(response, "headers", {}) or {}).get("Content-Length")
        try:
            content_length = int(raw_length)
        except (TypeError, ValueError):
            content_length = None
        if content_length is not None and content_length >= 0:
            if content_length > MAX_DOWNLOAD_SIZE:
                raise RuntimeError(
                    f"{format_label} превышает допустимый размер загрузки"
                )

        written = 0
        with open(path, "wb") as output:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                written += len(chunk)
                if written > MAX_DOWNLOAD_SIZE:
                    raise RuntimeError(
                        f"{format_label} превышает допустимый размер загрузки"
                    )
                output.write(chunk)

    def extract_fb2(download_path):
        with zipfile.ZipFile(download_path) as archive:
            members = [
                info
                for info in archive.infolist()
                if not info.is_dir() and info.filename.lower().endswith(".fb2")
            ]
            if not members:
                raise DownloadValidationError("В OPDS acquisition ZIP не найден FB2")
            member = members[0]
            if member.file_size > MAX_DOWNLOAD_SIZE:
                raise RuntimeError("FB2 превышает допустимый распакованный размер")
            extracted = 0
            with archive.open(member) as source, open(destination_path, "wb") as output:
                while True:
                    chunk = source.read(64 * 1024)
                    if not chunk:
                        break
                    extracted += len(chunk)
                    if extracted > MAX_DOWNLOAD_SIZE:
                        raise RuntimeError(
                            "FB2 превышает допустимый распакованный размер"
                        )
                    output.write(chunk)

    try:
        for attempt in range(1, DOWNLOAD_RETRY_ATTEMPTS + 1):
            cleanup_attempt()
            started = time.monotonic()
            emit_download_progress(
                progress,
                f"{format_label} · попытка {attempt} из {DOWNLOAD_RETRY_ATTEMPTS} · ожидание ответа сервера…",
                attempt,
                DOWNLOAD_RETRY_ATTEMPTS,
                file_format,
                "request",
            )
            try:
                response = http_session.get(
                    normalized_url,
                    headers={"User-Agent": "OPDS-Desktop-Client/1.0"},
                    stream=True,
                    allow_redirects=True,
                    timeout=(DOWNLOAD_CONNECT_TIMEOUT, DOWNLOAD_READ_TIMEOUT),
                )
                try:
                    normalize_opds_url(response.url)
                    response.raise_for_status()
                    emit_download_progress(
                        progress,
                        f"{format_label} · попытка {attempt} из {DOWNLOAD_RETRY_ATTEMPTS} · соединение установлено, получение файла…",
                        attempt,
                        DOWNLOAD_RETRY_ATTEMPTS,
                        file_format,
                        "download",
                    )
                    stream_response(
                        response,
                        destination_path if file_format == "epub" else download_artifact,
                    )
                finally:
                    response.close()

                if file_format == "epub":
                    validate_epub_file(destination_path)
                elif zipfile.is_zipfile(download_artifact):
                    extract_fb2(download_artifact)
                    validate_fb2_file(destination_path)
                    os.remove(download_artifact)
                else:
                    os.replace(download_artifact, destination_path)
                    validate_fb2_file(destination_path)

                elapsed = time.monotonic() - started
                emit_download_progress(
                    progress,
                    f"{format_label} · получен за {elapsed:.1f} сек",
                    attempt,
                    DOWNLOAD_RETRY_ATTEMPTS,
                    file_format,
                    "success",
                    elapsed,
                )
                return elapsed
            except Exception as exc:
                last_error = exc
                cleanup_attempt()
                elapsed = time.monotonic() - started
                error_info = download_error_info(exc)
                retryable = error_info["retryable"] and not isinstance(exc, ValueError)
                if attempt >= DOWNLOAD_RETRY_ATTEMPTS or not retryable:
                    emit_download_progress(
                        progress,
                        f"{format_label} · попытка {attempt} из {DOWNLOAD_RETRY_ATTEMPTS} завершилась ошибкой: {compact_error(exc)}",
                        attempt,
                        DOWNLOAD_RETRY_ATTEMPTS,
                        file_format,
                        "failed",
                        elapsed,
                    )
                    break
                delay = DOWNLOAD_RETRY_DELAY * attempt
                emit_download_progress(
                    progress,
                    f"{format_label} · попытка {attempt} из {DOWNLOAD_RETRY_ATTEMPTS} не удалась: {compact_error(exc)} · повтор через {delay} сек",
                    attempt,
                    DOWNLOAD_RETRY_ATTEMPTS,
                    file_format,
                    "retry_wait",
                    elapsed,
                )
                time.sleep(delay)
    finally:
        if owns_session:
            http_session.close()
    raise last_error


def cleanup_partial_files():
    """Удаляет только служебные *.part Bridge из каталога назначения."""
    removed = 0
    if not os.path.isdir(DESTINATION):
        return 0
    for root, _, files in os.walk(DESTINATION):
        for filename in files:
            if not filename.endswith(".part"):
                continue
            path = os.path.join(root, filename)
            try:
                os.remove(path)
                removed += 1
            except OSError:
                pass
    return removed


def save_epub(book_id, destination, progress=None):
    """Скачивает, проверяет и атомарно сохраняет EPUB."""
    url = f"{LEGACY_OPDS_BASE}/b/{book_id}/epub"
    last_error = None
    for attempt in range(1, DOWNLOAD_RETRY_ATTEMPTS + 1):
        if os.path.exists(destination):
            os.remove(destination)
        started = time.monotonic()
        emit_download_progress(
            progress,
            f"EPUB · попытка {attempt} из {DOWNLOAD_RETRY_ATTEMPTS} · ожидание ответа OPDS-источника…",
            attempt, DOWNLOAD_RETRY_ATTEMPTS, "epub", "request"
        )
        try:
            # ВАЖНО: retry_attempts=1. Повторы выполняются только этим циклом.
            with legacy_opds_get(
                url,
                stream=True,
                timeout=(DOWNLOAD_CONNECT_TIMEOUT, DOWNLOAD_READ_TIMEOUT),
                retry_attempts=1,
            ) as r:
                emit_download_progress(
                    progress,
                    f"EPUB · попытка {attempt} из {DOWNLOAD_RETRY_ATTEMPTS} · соединение установлено, получение файла…",
                    attempt, DOWNLOAD_RETRY_ATTEMPTS, "epub", "download"
                )
                size = 0
                with open(destination, "wb") as out:
                    for chunk in r.iter_content(chunk_size=64 * 1024):
                        if not chunk:
                            continue
                        size += len(chunk)
                        if size > MAX_DOWNLOAD_SIZE:
                            raise RuntimeError("EPUB превышает допустимый размер")
                        out.write(chunk)
            validate_epub_file(destination)
            elapsed = time.monotonic() - started
            emit_download_progress(
                progress,
                f"EPUB · получен за {elapsed:.1f} сек",
                attempt, DOWNLOAD_RETRY_ATTEMPTS, "epub", "success", elapsed
            )
            return elapsed
        except Exception as exc:
            last_error = exc
            if os.path.exists(destination):
                os.remove(destination)
            elapsed = time.monotonic() - started
            error_info = download_error_info(exc)
            if attempt >= DOWNLOAD_RETRY_ATTEMPTS or not error_info["retryable"]:
                emit_download_progress(
                    progress,
                    f"EPUB · попытка {attempt} из {DOWNLOAD_RETRY_ATTEMPTS} завершилась ошибкой: {compact_error(exc)}",
                    attempt, DOWNLOAD_RETRY_ATTEMPTS, "epub", "failed", elapsed
                )
                break
            delay = DOWNLOAD_RETRY_DELAY * attempt
            emit_download_progress(
                progress,
                f"EPUB · попытка {attempt} из {DOWNLOAD_RETRY_ATTEMPTS} не удалась: {compact_error(exc)} · повтор через {delay} сек",
                attempt, DOWNLOAD_RETRY_ATTEMPTS, "epub", "retry_wait", elapsed
            )
            time.sleep(delay)
    raise last_error


def save_fb2(book_id, destination, progress=None):
    """Скачивает и проверяет FB2 либо содержащий его ZIP."""
    url = f"{LEGACY_OPDS_BASE}/b/{book_id}/fb2"
    last_error = None
    for attempt in range(1, DOWNLOAD_RETRY_ATTEMPTS + 1):
        if os.path.exists(destination):
            os.remove(destination)
        started = time.monotonic()
        emit_download_progress(
            progress,
            f"FB2 · попытка {attempt} из {DOWNLOAD_RETRY_ATTEMPTS} · ожидание ответа OPDS-источника…",
            attempt, DOWNLOAD_RETRY_ATTEMPTS, "fb2", "request"
        )
        try:
            # ВАЖНО: retry_attempts=1. Повторы выполняются только этим циклом.
            with legacy_opds_get(
                url,
                timeout=(DOWNLOAD_CONNECT_TIMEOUT, DOWNLOAD_READ_TIMEOUT),
                retry_attempts=1,
            ) as r:
                content = r.content
            if len(content) > MAX_DOWNLOAD_SIZE:
                raise RuntimeError("Архив превышает допустимый размер")
            buf = io.BytesIO(content)
            if not zipfile.is_zipfile(buf):
                raise RuntimeError("OPDS-источник вернул некорректный FB2 ZIP")
            buf.seek(0)
            with zipfile.ZipFile(buf) as z:
                names = [n for n in z.namelist() if n.lower().endswith(".fb2")]
                if not names:
                    raise RuntimeError("В архиве не найден FB2")
                info = z.getinfo(names[0])
                if info.file_size > MAX_DOWNLOAD_SIZE:
                    raise RuntimeError("FB2 превышает допустимый размер")
                data = z.read(names[0])
            if b"<FictionBook" not in data[:10000]:
                raise RuntimeError("Распакованный файл не похож на FB2")
            with open(destination, "wb") as out:
                out.write(data)
            validate_fb2_file(destination)
            elapsed = time.monotonic() - started
            emit_download_progress(
                progress,
                f"FB2 · получен за {elapsed:.1f} сек",
                attempt, DOWNLOAD_RETRY_ATTEMPTS, "fb2", "success", elapsed
            )
            return elapsed
        except Exception as exc:
            last_error = exc
            if os.path.exists(destination):
                os.remove(destination)
            elapsed = time.monotonic() - started
            error_info = download_error_info(exc)
            if attempt >= DOWNLOAD_RETRY_ATTEMPTS or not error_info["retryable"]:
                emit_download_progress(
                    progress,
                    f"FB2 · попытка {attempt} из {DOWNLOAD_RETRY_ATTEMPTS} завершилась ошибкой: {compact_error(exc)}",
                    attempt, DOWNLOAD_RETRY_ATTEMPTS, "fb2", "failed", elapsed
                )
                break
            delay = DOWNLOAD_RETRY_DELAY * attempt
            emit_download_progress(
                progress,
                f"FB2 · попытка {attempt} из {DOWNLOAD_RETRY_ATTEMPTS} не удалась: {compact_error(exc)} · повтор через {delay} сек",
                attempt, DOWNLOAD_RETRY_ATTEMPTS, "fb2", "retry_wait", elapsed
            )
            time.sleep(delay)
    raise last_error

def choose_bulk_format(book, mode):
    """Выбирает доступный формат с учётом режима задания."""
    if mode == "epub": return "epub" if book.get("epub") else None
    if mode == "fb2": return "fb2" if book.get("fb2") else None
    if book.get("epub"): return "epub"
    if book.get("fb2"): return "fb2"
    return None


def download_one_book(book, file_format, duplicate_mode=False, progress=None):
    """Сохраняет одну книгу в соответствующий авторский каталог."""
    storage_title = duplicate_storage_title(book) if duplicate_mode else book["title"]
    p = local_paths(book["author"], storage_title)
    lookup_titles = (
        duplicate_storage_title_candidates(book)
        if duplicate_mode
        else (book["title"],)
    )
    for lookup_title in lookup_titles:
        existing = local_paths(book["author"], lookup_title)
        if os.path.isfile(existing["epub"]) or os.path.isfile(existing["fb2"]):
            return "skipped", "Уже существует", 0.0
    destination = p[file_format]
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    temporary = destination + ".part"
    if os.path.exists(temporary):
        os.remove(temporary)
    try:
        source_id = str(book.get("source_id") or LEGACY_QUEUE_SOURCE_ID)
        if source_id == LEGACY_QUEUE_SOURCE_ID:
            if file_format == "epub":
                elapsed = save_epub(book["id"], temporary, progress=progress)
            else:
                elapsed = save_fb2(book["id"], temporary, progress=progress)
        elif file_format == "epub":
            url = book.get("epub_url")
            if not url:
                raise RuntimeError("Для EPUB отсутствует acquisition URL")
            elapsed = save_opds_acquisition(
                url,
                temporary,
                "epub",
                mime_type=book.get("epub_mime_type", ""),
                progress=progress,
            )
        else:
            url = book.get("fb2_url")
            if not url:
                raise RuntimeError("Для FB2 отсутствует acquisition URL")
            elapsed = save_opds_acquisition(
                url,
                temporary,
                "fb2",
                mime_type=book.get("fb2_mime_type", ""),
                progress=progress,
            )
        os.replace(temporary, destination)
        return "downloaded", destination, elapsed
    except Exception:
        if os.path.exists(temporary):
            os.remove(temporary)
        raise


def catalog_return(kind, catalog_id, name, page=0, view=""):
    """Формирует ссылку возврата к исходному виду каталога."""
    label = "Вернуться к автору" if kind == "author" else "Вернуться к серии"
    path = f"/{'author' if kind == 'author' else 'series'}/{catalog_id}"
    try:
        page = max(0, min(int(page or 0), MAX_CATALOG_PAGES - 1))
    except (TypeError, ValueError):
        page = 0
    params = {}
    if name:
        params["name"] = name
    if view == "all":
        params["view"] = "all"
    elif page > 0:
        params["page"] = page
    if params:
        path += "?" + urlencode(params)
    return path, label


def catalog_selection_clear_token(kind, catalog_id):
    """Строит source-aware token одноразовой очистки выбора."""
    storage_key = catalog_selection_storage_key(
        current_source_id(),
        str(kind),
        str(catalog_id),
    )
    return f"clear:{storage_key}"


def mark_catalog_selection_clear(kind, catalog_id):
    """Помечает выбор каталога для очистки после успешной операции."""
    pending = dict(session.get("catalog_selections_to_clear", {}))
    pending[catalog_selection_clear_token(kind, catalog_id)] = True
    session["catalog_selections_to_clear"] = pending


def catalog_selection_clear_pending(kind, catalog_id, consume=False):
    """Проверяет и при необходимости потребляет флаг очистки выбора."""
    pending = dict(session.get("catalog_selections_to_clear", {}))
    token = catalog_selection_clear_token(kind, catalog_id)
    marked = bool(pending.get(token))
    if marked and consume:
        pending.pop(token, None)
        if pending:
            session["catalog_selections_to_clear"] = pending
        else:
            session.pop("catalog_selections_to_clear", None)
    return marked




# ============================================================
# Состояние приложения и очередь загрузки
# ============================================================

def human_bytes(value):
    """Форматирует количество байт для интерфейса."""
    try:
        value = max(0, int(value))
    except Exception:
        value = 0
    units = ("Б", "КБ", "МБ", "ГБ", "ТБ")
    number = float(value)
    for unit in units:
        if number < 1024 or unit == units[-1]:
            if unit == "Б":
                return f"{int(number)} {unit}"
            if number >= 100:
                return f"{number:.0f} {unit}"
            if number >= 10:
                return f"{number:.1f} {unit}"
            return f"{number:.2f} {unit}"
        number /= 1024
    return f"{value} Б"


def disk_status():
    """Возвращает объём диска и состояние свободного резерва."""
    path = DESTINATION if os.path.exists(DESTINATION) else "/"
    usage = shutil.disk_usage(path)
    try:
        min_free_gb = max(1.0, float(queue_setting_get("min_free_gb", QUEUE_DEFAULT_MIN_FREE_GB)))
    except Exception:
        min_free_gb = float(QUEUE_DEFAULT_MIN_FREE_GB)
    min_free_bytes = int(min_free_gb * 1024**3)
    return {
        "path": path,
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
        "total_text": human_bytes(usage.total),
        "used_text": human_bytes(usage.used),
        "free_text": human_bytes(usage.free),
        "min_free_bytes": min_free_bytes,
        "min_free_gb": int(min_free_gb) if min_free_gb.is_integer() else min_free_gb,
        "low": usage.free < min_free_bytes,
    }



def next_auto_run_info():
    """Вычисляет состояние следующего запуска по расписанию."""
    enabled = queue_setting_get("auto_enabled", "0") == "1"
    if not enabled:
        return {"state": "warn", "text": "Автозапуск выключен", "detail": "Включается в настройках очереди"}

    auto_time = queue_setting_get("auto_time", QUEUE_DEFAULT_TIME)
    tz_text = queue_setting_get("tz_offset", QUEUE_DEFAULT_TZ_OFFSET)
    m = re.fullmatch(r"(\d{2}):(\d{2})", auto_time or "")
    if not m:
        return {"state": "error", "text": "Ошибка расписания", "detail": f"Некорректное время: {auto_time}"}

    tz = timezone(parse_tz_offset(tz_text))
    now = datetime.now(tz)
    target = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)

    suffix = ""
    if queue_pending_count() == 0:
        suffix = " · очередь пуста"
    elif queue_setting_get("paused", "0") == "1":
        suffix = " · очередь на паузе"

    return {
        "state": "ok" if queue_setting_get("paused", "0") != "1" else "warn",
        "text": target.strftime("%d.%m %H:%M") + suffix,
        "detail": target.strftime("%d.%m.%Y %H:%M") + f" UTC{tz_text}",
    }


def health_snapshot(force=False):
    """Собирает кэшируемый снимок основных компонентов."""
    now = time.time()
    with health_cache_lock:
        cached = health_cache.get("data")
        age = now - float(health_cache.get("time") or 0)
        if cached is not None and not force and age < HEALTH_CACHE_TTL:
            return json.loads(json.dumps(cached, ensure_ascii=False))

    data = {
        "bridge": {"state": "ok", "text": "Работает", "detail": "Локальное desktop-приложение"},
    }

    # Проверяем прямой доступ к Flibusta.
    try:
        r = requests.get(
            f"{LEGACY_OPDS_BASE}/opds",
            timeout=(3, 5),
            allow_redirects=True,
            stream=True,
        )
        status = int(r.status_code)
        r.close()

        if 200 <= status < 400:
            health_entry = {
                "state": "ok",
                "text": f"Доступна · HTTP {status}",
                "detail": LEGACY_OPDS_BASE,
            }
        else:
            health_entry = {
                "state": "error",
                "text": f"HTTP {status}",
                "detail": LEGACY_OPDS_BASE,
            }
    except Exception as exc:
        health_entry = {
            "state": "error",
            "text": "Недоступна",
            "detail": str(exc),
        }
    data["legacy_opds"] = health_entry
    data["flibusta"] = health_entry

    destination_ok = os.path.isdir(DESTINATION) and os.access(DESTINATION, os.R_OK | os.W_OK | os.X_OK)
    data["destination"] = {
        "state": "ok" if destination_ok else "error",
        "text": "Чтение/запись OK" if destination_ok else "Нет доступа",
        "detail": DESTINATION,
    }

    try:
        disk = disk_status()
        data["disk"] = {
            "state": "error" if disk["low"] else "ok",
            "text": f"{disk['free_text']} свободно",
            "detail": f"Всего {disk['total_text']} · резерв {disk['min_free_gb']} ГБ",
        }
    except Exception as exc:
        data["disk"] = {"state": "error", "text": "Ошибка", "detail": str(exc)}

    try:
        counts = queue_counts()
        if queue_is_worker_active():
            queue_text = f"Скачивается · ждут {counts.get('pending', 0)}"
            queue_state = "ok"
        elif queue_setting_get("paused", "0") == "1":
            queue_text = f"Пауза · ждут {counts.get('pending', 0)}"
            queue_state = "warn"
        elif counts.get("pending", 0):
            queue_text = f"Ожидает {counts.get('pending', 0)}"
            queue_state = "ok"
        else:
            queue_text = "Пуста"
            queue_state = "ok"
        data["queue"] = {
            "state": queue_state,
            "text": queue_text,
            "detail": f"Ошибок в истории: {counts.get('error', 0)}",
        }
    except Exception as exc:
        data["queue"] = {"state": "error", "text": "Ошибка", "detail": str(exc)}

    try:
        data["schedule"] = next_auto_run_info()
    except Exception as exc:
        data["schedule"] = {"state": "error", "text": "Ошибка", "detail": str(exc)}

    data["checked_at"] = format_time(now)
    data["cache_ttl"] = HEALTH_CACHE_TTL

    with health_cache_lock:
        health_cache["time"] = now
        health_cache["data"] = json.loads(json.dumps(data, ensure_ascii=False))
    return data


def queue_connect():
    """Открывает настроенное соединение с локальной SQLite-очередью."""
    conn = sqlite3.connect(QUEUE_DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_queue_db():
    """Создаёт схему очереди и восстанавливает незавершённый запуск."""
    os.makedirs(os.path.dirname(QUEUE_DB_FILE), exist_ok=True)
    with queue_db_lock, queue_connect() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS queue_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            flibusta_id TEXT NOT NULL,
            source_id TEXT NOT NULL DEFAULT '',
            source_item_id TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL,
            author TEXT NOT NULL,
            book_json TEXT NOT NULL,
            format_mode TEXT NOT NULL DEFAULT 'auto',
            download_duplicates INTEGER NOT NULL DEFAULT 0,
            priority INTEGER NOT NULL DEFAULT 0,
            run_id TEXT NOT NULL DEFAULT '',
            downloaded_bytes INTEGER NOT NULL DEFAULT 0,
            error_category TEXT NOT NULL DEFAULT '',
            retry_queued INTEGER NOT NULL DEFAULT 0,
            retry_of_id INTEGER,
            status TEXT NOT NULL DEFAULT 'pending',
            added_at REAL NOT NULL,
            started_at REAL,
            finished_at REAL,
            attempts INTEGER NOT NULL DEFAULT 0,
            detail TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            origin_kind TEXT NOT NULL DEFAULT '',
            origin_id TEXT NOT NULL DEFAULT '',
            origin_name TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_queue_status_id ON queue_items(status,id);
        CREATE TABLE IF NOT EXISTS queue_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL DEFAULT 'info',
            title TEXT NOT NULL,
            message TEXT NOT NULL,
            target_url TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            read_at REAL
        );
        CREATE INDEX IF NOT EXISTS idx_notifications_read_created ON notifications(read_at,created_at DESC);
        CREATE TABLE IF NOT EXISTS queue_runs (
            run_id TEXT PRIMARY KEY,
            started_at REAL NOT NULL,
            finished_at REAL,
            status TEXT NOT NULL DEFAULT 'running',
            trigger TEXT NOT NULL DEFAULT 'manual',
            recovered_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_queue_runs_started ON queue_runs(started_at DESC);
        """)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(queue_items)")}
        if "priority" not in columns:
            conn.execute("ALTER TABLE queue_items ADD COLUMN priority INTEGER NOT NULL DEFAULT 0")
        if "run_id" not in columns:
            conn.execute("ALTER TABLE queue_items ADD COLUMN run_id TEXT NOT NULL DEFAULT ''")
        if "downloaded_bytes" not in columns:
            conn.execute("ALTER TABLE queue_items ADD COLUMN downloaded_bytes INTEGER NOT NULL DEFAULT 0")
        if "error_category" not in columns:
            conn.execute("ALTER TABLE queue_items ADD COLUMN error_category TEXT NOT NULL DEFAULT ''")
        if "retry_queued" not in columns:
            conn.execute("ALTER TABLE queue_items ADD COLUMN retry_queued INTEGER NOT NULL DEFAULT 0")
        if "retry_of_id" not in columns:
            conn.execute("ALTER TABLE queue_items ADD COLUMN retry_of_id INTEGER")
        if "source_id" not in columns:
            conn.execute("ALTER TABLE queue_items ADD COLUMN source_id TEXT NOT NULL DEFAULT ''")
        if "source_item_id" not in columns:
            conn.execute("ALTER TABLE queue_items ADD COLUMN source_item_id TEXT NOT NULL DEFAULT ''")
        conn.execute(
            "UPDATE queue_items SET source_id=? WHERE source_id=''",
            (LEGACY_QUEUE_SOURCE_ID,),
        )
        conn.execute(
            "UPDATE queue_items SET source_item_id=flibusta_id WHERE source_item_id=''"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_queue_status_priority_id ON queue_items(status,priority DESC,id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_queue_run_id ON queue_items(run_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_queue_retry_of_id ON queue_items(retry_of_id)")

        conn.execute("DROP INDEX IF EXISTS uq_queue_active_flibusta")

        # Устраняем старые active-дубли одной source-aware identity перед UNIQUE index.
        duplicate_groups = conn.execute(
            """SELECT source_id,source_item_id
               FROM queue_items
               WHERE status IN ('pending','downloading')
               GROUP BY source_id,source_item_id
               HAVING COUNT(*) > 1"""
        ).fetchall()
        migration_now = time.time()
        for group in duplicate_groups:
            active_rows = conn.execute(
                """SELECT id,status
                   FROM queue_items
                   WHERE source_id=? AND source_item_id=?
                     AND status IN ('pending','downloading')
                   ORDER BY CASE status WHEN 'downloading' THEN 0 ELSE 1 END, id""",
                (group["source_id"], group["source_item_id"]),
            ).fetchall()
            for duplicate in active_rows[1:]:
                conn.execute(
                    """UPDATE queue_items
                       SET status='skipped',
                           finished_at=COALESCE(finished_at,?),
                           detail='Дубликат source-aware identity устранён при миграции очереди',
                           error_category='duplicate_queue'
                       WHERE id=?""",
                    (migration_now, duplicate["id"]),
                )
        conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS uq_queue_active_source_item
               ON queue_items(source_id,source_item_id)
               WHERE status IN ('pending','downloading')"""
        )

        # Снимок состояния ДО сброса run_active нужен для аварийного автопродолжения.
        active_row = conn.execute("SELECT value FROM queue_settings WHERE key='run_active'").fetchone()
        was_active = bool(active_row and str(active_row["value"]) == "1")
        run_row = conn.execute("SELECT value FROM queue_settings WHERE key='current_run_id'").fetchone()
        recovery_run_id = str(run_row["value"] or "") if run_row else ""
        stale_downloading = int(conn.execute(
            "SELECT COUNT(*) AS c FROM queue_items WHERE status='downloading'"
        ).fetchone()["c"])
        conn.execute(
            """UPDATE queue_items
               SET status='pending',
                   detail='Восстановлено после перезапуска · будет повторено',
                   started_at=NULL,
                   attempts=0
               WHERE status='downloading'"""
        )
        defaults = {
            'auto_enabled': '0',
            'auto_time': QUEUE_DEFAULT_TIME,
            'tz_offset': QUEUE_DEFAULT_TZ_OFFSET,
            'min_free_gb': str(QUEUE_DEFAULT_MIN_FREE_GB),
            'paused': '0',
            'pause_reason': '',
            'last_auto_run_key': '',
            'run_total': '0',
            'run_processed': '0',
            'run_downloaded_bytes': '0',
            'run_started_at': '0',
            'run_finished_at': '0',
            'run_active': '0',
            'last_notified_run_started_at': '0',
            'current_run_id': '',
            'last_notified_run_id': '',
        }
        for key, value in defaults.items():
            conn.execute("INSERT OR IGNORE INTO queue_settings(key,value) VALUES(?,?)", (key, value))

        # Создаём записи для запусков v17, чтобы история запусков появилась сразу после обновления.
        legacy_runs = conn.execute(
            """SELECT run_id,
                      COALESCE(MIN(started_at), MIN(added_at)) AS started_at,
                      MAX(finished_at) AS finished_at,
                      SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS errors,
                      SUM(CASE WHEN status IN ('pending','downloading') THEN 1 ELSE 0 END) AS active_items
               FROM queue_items
               WHERE run_id <> ''
               GROUP BY run_id"""
        ).fetchall()
        for row in legacy_runs:
            status = "running" if int(row["active_items"] or 0) else ("completed_with_errors" if int(row["errors"] or 0) else "completed")
            conn.execute(
                """INSERT OR IGNORE INTO queue_runs(run_id,started_at,finished_at,status,trigger,recovered_count)
                   VALUES(?,?,?,?,?,0)""",
                (
                    row["run_id"],
                    float(row["started_at"] or time.time()),
                    None if status == "running" else row["finished_at"],
                    status,
                    "legacy",
                ),
            )

        pending_after_recovery = int(conn.execute(
            "SELECT COUNT(*) AS c FROM queue_items WHERE status='pending'"
        ).fetchone()["c"])
        should_resume = bool(was_active and pending_after_recovery > 0 and recovery_run_id)
        should_finalize = bool(was_active and pending_after_recovery == 0 and stale_downloading == 0 and recovery_run_id)
        if recovery_run_id and (stale_downloading or should_resume):
            conn.execute(
                """UPDATE queue_runs
                   SET status='running',
                       finished_at=NULL,
                       recovered_count=recovered_count+?
                   WHERE run_id=?""",
                (stale_downloading, recovery_run_id),
            )

        conn.execute("UPDATE queue_settings SET value='0' WHERE key='run_active'")
        conn.commit()

    return {
        "was_active": was_active,
        "resume": should_resume,
        "finalize": should_finalize,
        "run_id": recovery_run_id,
        "recovered_items": stale_downloading,
    }


def queue_setting_get(key, default=''):
    """Читает строковую настройку очереди из SQLite."""
    with queue_db_lock, queue_connect() as conn:
        row = conn.execute("SELECT value FROM queue_settings WHERE key=?", (key,)).fetchone()
        return row['value'] if row else default


def queue_setting_set(key, value):
    """Записывает строковую настройку очереди в SQLite."""
    with queue_db_lock, queue_connect() as conn:
        conn.execute("INSERT INTO queue_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
        conn.commit()


def queue_setting_int(key, default=0):
    """Читает целочисленную настройку с запасным значением."""
    try:
        return int(float(queue_setting_get(key, str(default))))
    except Exception:
        return int(default)



def notification_create(kind, title, message, target_url="/queue/history"):
    """Сохраняет внутреннее уведомление о фоновой работе."""
    kind = kind if kind in {"info", "success", "warning", "error"} else "info"
    with queue_db_lock, queue_connect() as conn:
        conn.execute(
            "INSERT INTO notifications(kind,title,message,target_url,created_at) VALUES(?,?,?,?,?)",
            (kind, display_text(title, "Уведомление"), display_text(message, ""), target_url or "", time.time()),
        )
        conn.commit()


def notification_unread_count():
    """Возвращает количество непрочитанных уведомлений."""
    try:
        with queue_db_lock, queue_connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM notifications WHERE read_at IS NULL").fetchone()
            return int(row["c"] if row else 0)
    except Exception:
        return 0


def notification_list(limit=100):
    """Возвращает последние внутренние уведомления."""
    with queue_db_lock, queue_connect() as conn:
        rows = conn.execute("SELECT * FROM notifications ORDER BY created_at DESC LIMIT ?", (int(limit),)).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["created_text"] = format_time(item.get("created_at"))
        result.append(item)
    return result


def notification_mark_all_read():
    """Отмечает все внутренние уведомления прочитанными."""
    with queue_db_lock, queue_connect() as conn:
        conn.execute("UPDATE notifications SET read_at=? WHERE read_at IS NULL", (time.time(),))
        conn.commit()


def queue_run_result_counts(run_id=None):
    """Суммирует результаты книг выбранного запуска."""
    run_id = run_id if run_id is not None else queue_setting_get("current_run_id", "")
    result = {"done": 0, "skipped": 0, "error": 0}
    if not run_id:
        return result
    with queue_db_lock, queue_connect() as conn:
        rows = conn.execute(
            """SELECT status, COUNT(*) AS c
               FROM queue_items
               WHERE run_id=? AND finished_at IS NOT NULL
               GROUP BY status""",
            (run_id,),
        ).fetchall()
    for row in rows:
        if row["status"] in result:
            result[row["status"]] = int(row["c"])
    return result


def queue_run_error_items(run_id, limit=10):
    """Возвращает ограниченный список ошибок запуска."""
    if not run_id:
        return []
    with queue_db_lock, queue_connect() as conn:
        rows = conn.execute(
            """SELECT title, author, error
               FROM queue_items
               WHERE run_id=? AND status='error'
               ORDER BY finished_at, id
               LIMIT ?""",
            (run_id, int(limit)),
        ).fetchall()
    return [dict(row) for row in rows]


def queue_create_completion_notification():
    """Создаёт итоговое уведомление после завершения очереди."""
    run_id = queue_setting_get("current_run_id", "")
    if not run_id:
        return

    already = queue_setting_get("last_notified_run_id", "")
    if already == run_id:
        return

    summary = queue_run_summary(run_id)
    if not summary:
        return
    counts = summary["counts"]
    errors = counts.get("error", 0)

    if errors:
        title = "Очередь завершена с ошибками"
        kind = "error"
        failed = queue_run_error_items(run_id, limit=5)
        failed_names = [display_text(row.get("title"), "Без названия") for row in failed]
        error_part = f"Не скачано книг: {errors}"
        if failed_names:
            error_part += " · " + "; ".join(failed_names)
            if errors > len(failed_names):
                error_part += f"; ещё {errors-len(failed_names)}"
    else:
        title = "Очередь завершена"
        kind = "success"
        error_part = ""

    parts = [
        f"Готово: {counts.get('done',0)}",
        f"Пропущено: {counts.get('skipped',0)}",
        f"Ошибок: {errors}",
        f"Загружено: {summary.get('downloaded_text','0 Б')}",
        f"Время: {summary.get('elapsed_text','00:00:00')}",
    ]
    if error_part:
        parts.append(error_part)

    target_url = f"/runs/{run_id}"
    notification_create(kind, title, " · ".join(parts), target_url)
    queue_setting_set("last_notified_run_id", run_id)
    queue_setting_set("last_notified_run_started_at", queue_setting_get("run_started_at", "0"))


def queue_run_summary(run_id):
    """Собирает подробную сводку одного запуска очереди."""
    if not run_id:
        return None
    with queue_db_lock, queue_connect() as conn:
        run = conn.execute("SELECT * FROM queue_runs WHERE run_id=?", (run_id,)).fetchone()
        rows = conn.execute(
            """SELECT status, COUNT(*) AS c, COALESCE(SUM(downloaded_bytes),0) AS bytes
               FROM queue_items
               WHERE run_id=?
               GROUP BY status""",
            (run_id,),
        ).fetchall()
        timing = conn.execute(
            """SELECT MIN(started_at) AS first_started, MAX(finished_at) AS last_finished
               FROM queue_items WHERE run_id=?""",
            (run_id,),
        ).fetchone()
    if not run:
        return None
    counts = {"done":0, "skipped":0, "error":0, "pending":0, "downloading":0}
    downloaded = 0
    for row in rows:
        if row["status"] in counts:
            counts[row["status"]] = int(row["c"] or 0)
        downloaded += int(row["bytes"] or 0)
    started_at = float(run["started_at"] or (timing["first_started"] if timing else 0) or 0)
    finished_at = float(run["finished_at"] or 0)
    active = run["status"] in {"running","paused"}
    end = time.time() if active else (finished_at or float((timing["last_finished"] if timing else 0) or started_at))
    elapsed = max(0, int(end - started_at)) if started_at else 0
    trigger_names = {"manual":"Вручную","schedule":"По расписанию","recovery":"Восстановление","legacy":"До v18"}
    status_names = {
        "running":"Выполняется",
        "paused":"На паузе",
        "completed":"Завершён",
        "completed_with_errors":"Завершён с ошибками",
        "interrupted":"Прерван",
    }
    total = sum(counts.values())
    return {
        "run_id": run_id,
        "short_id": run_id[:8],
        "status": run["status"],
        "status_text": status_names.get(run["status"], run["status"]),
        "trigger": run["trigger"],
        "trigger_text": trigger_names.get(run["trigger"], run["trigger"]),
        "recovered_count": int(run["recovered_count"] or 0),
        "started_at": started_at,
        "finished_at": finished_at,
        "started_text": format_time(started_at) if started_at else "",
        "finished_text": format_time(finished_at) if finished_at else "",
        "elapsed_seconds": elapsed,
        "elapsed_text": str(timedelta(seconds=elapsed)),
        "downloaded_bytes": downloaded,
        "downloaded_text": human_bytes(downloaded),
        "counts": counts,
        "total": total,
    }


def queue_run_finalize(run_id, finished_at=None):
    """Фиксирует счётчики и время завершения запуска."""
    if not run_id:
        return
    summary = queue_run_summary(run_id)
    errors = summary["counts"]["error"] if summary else 0
    status = "completed_with_errors" if errors else "completed"
    finished_at = float(finished_at or time.time())
    with queue_db_lock, queue_connect() as conn:
        conn.execute(
            "UPDATE queue_runs SET status=?, finished_at=? WHERE run_id=?",
            (status, finished_at, run_id),
        )
        conn.commit()


def queue_run_mark_paused(run_id):
    """Помечает текущий запуск как остановленный на паузе."""
    if not run_id:
        return
    with queue_db_lock, queue_connect() as conn:
        conn.execute("UPDATE queue_runs SET status='paused' WHERE run_id=?", (run_id,))
        conn.commit()


def queue_runs_list(limit=100):
    """Возвращает последние запуски фоновой очереди."""
    with queue_db_lock, queue_connect() as conn:
        rows = conn.execute(
            "SELECT run_id FROM queue_runs ORDER BY started_at DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    result = []
    for row in rows:
        summary = queue_run_summary(row["run_id"])
        if summary and summary["total"]:
            result.append(summary)
    return result


def queue_run_items(run_id):
    """Возвращает книги конкретного запуска очереди."""
    with queue_db_lock, queue_connect() as conn:
        rows = conn.execute(
            """SELECT * FROM queue_items
               WHERE run_id=?
               ORDER BY COALESCE(started_at,added_at), id""",
            (run_id,),
        ).fetchall()
    status_names = {'pending':'Ожидает','downloading':'Загружается','done':'Готово','skipped':'Пропущено','error':'Ошибка'}
    items = []
    for row in rows:
        item = dict(row)
        item["status_text"] = status_names.get(item["status"], item["status"])
        item["added_text"] = format_time(item.get("added_at"))
        item["started_text"] = format_time(item.get("started_at")) if item.get("started_at") else ""
        item["finished_text"] = format_time(item.get("finished_at")) if item.get("finished_at") else ""
        item["downloaded_text"] = human_bytes(item.get("downloaded_bytes") or 0)
        items.append(item)
    return items


def queue_retry_error_copies(run_id=None):
    """Создаёт новые pending-записи, сохраняя исходные ошибки в истории запуска."""
    with queue_db_lock, queue_connect() as conn:
        if run_id:
            rows = conn.execute(
                """SELECT * FROM queue_items
                   WHERE status='error' AND run_id=? AND retry_queued=0
                   ORDER BY id""",
                (run_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM queue_items
                   WHERE status='error' AND retry_queued=0
                   ORDER BY id"""
            ).fetchall()

        added = 0
        skipped = 0
        now = time.time()
        for row in rows:
            active = conn.execute(
                """SELECT 1 FROM queue_items
                   WHERE source_id=? AND source_item_id=?
                     AND status IN ('pending','downloading')
                   LIMIT 1""",
                (row["source_id"], row["source_item_id"]),
            ).fetchone()
            if active:
                skipped += 1
                continue
            cur = conn.execute(
                """INSERT INTO queue_items(
                       flibusta_id,source_id,source_item_id,
                       title,author,book_json,format_mode,download_duplicates,
                       priority,run_id,downloaded_bytes,error_category,retry_queued,retry_of_id,
                       status,added_at,started_at,finished_at,attempts,detail,error,
                       origin_kind,origin_id,origin_name
                   )
                   VALUES(?,?,?,?,?,?,?,?,?,'',0,'',0,?,'pending',?,NULL,NULL,0,'Повтор после ошибки','',?,?,?)""",
                (
                    row["flibusta_id"], row["source_id"], row["source_item_id"],
                    row["title"], row["author"], row["book_json"],
                    row["format_mode"], row["download_duplicates"], row["priority"],
                    row["id"], now,
                    row["origin_kind"], row["origin_id"], row["origin_name"],
                ),
            )
            if cur.rowcount:
                conn.execute("UPDATE queue_items SET retry_queued=1 WHERE id=?", (row["id"],))
                added += 1
        conn.commit()
    return added, skipped


@app.context_processor
def inject_bridge_context():
    """Добавляет счётчик уведомлений в контекст Jinja."""
    return {"unread_notifications": notification_unread_count()}


def queue_run_increment(processed=0, downloaded_bytes=0):
    """Атомарно увеличивает прогресс текущего запуска."""
    with queue_db_lock, queue_connect() as conn:
        for key, delta in (("run_processed", processed), ("run_downloaded_bytes", downloaded_bytes)):
            row = conn.execute("SELECT value FROM queue_settings WHERE key=?", (key,)).fetchone()
            current = int(float(row["value"])) if row else 0
            value = max(0, current + int(delta))
            conn.execute("INSERT INTO queue_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
        conn.commit()


def queue_run_initialize(trigger="manual"):
    """Создаёт запись запуска и связывает ожидающие книги."""
    total = queue_pending_count()
    now = time.time()
    run_id = uuid.uuid4().hex
    queue_setting_set("current_run_id", run_id)
    queue_setting_set("run_total", total)
    queue_setting_set("run_processed", 0)
    queue_setting_set("run_downloaded_bytes", 0)
    queue_setting_set("run_started_at", now)
    queue_setting_set("run_finished_at", 0)
    queue_setting_set("run_active", 1)
    with queue_db_lock, queue_connect() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO queue_runs(run_id,started_at,finished_at,status,trigger,recovered_count)
               VALUES(?,?,NULL,'running',?,0)""",
            (run_id, now, trigger if trigger in {"manual","schedule","recovery"} else "manual"),
        )
        # Все книги, ожидавшие в момент старта, относятся именно к этому запуску.
        conn.execute("UPDATE queue_items SET run_id=? WHERE status='pending'", (run_id,))
        conn.commit()
    return run_id


def queue_run_sync_total():
    """Синхронизирует объём запуска с числом его книг."""
    current_run_id = queue_setting_get("current_run_id", "")
    if current_run_id:
        # Книги, добавленные во время работающего запуска, включаются в тот же run_id.
        with queue_db_lock, queue_connect() as conn:
            conn.execute(
                "UPDATE queue_items SET run_id=? WHERE status='pending' AND run_id=''",
                (current_run_id,),
            )
            conn.commit()
    processed = queue_setting_int("run_processed", 0)
    counts = queue_counts()
    desired = processed + counts.get("pending", 0) + counts.get("downloading", 0)
    if desired > queue_setting_int("run_total", 0):
        queue_setting_set("run_total", desired)


def queue_run_progress():
    """Возвращает сохранённый прогресс активного запуска."""
    total = queue_setting_int("run_total", 0)
    processed = queue_setting_int("run_processed", 0)
    downloaded = queue_setting_int("run_downloaded_bytes", 0)
    started_at = float(queue_setting_get("run_started_at", "0") or 0)
    finished_at = float(queue_setting_get("run_finished_at", "0") or 0)
    active = queue_setting_get("run_active", "0") == "1"
    now = time.time()
    end = now if active or not finished_at else finished_at
    elapsed = max(0, int(end - started_at)) if started_at else 0
    h, rem = divmod(elapsed, 3600)
    m, s = divmod(rem, 60)
    percent = int(round(processed * 100 / total)) if total else 0
    percent = max(0, min(100, percent))
    return {
        "total": total,
        "processed": processed,
        "percent": percent,
        "downloaded_bytes": downloaded,
        "downloaded_text": human_bytes(downloaded),
        "started": bool(started_at),
        "active": active,
        "elapsed_text": f"{h:02d}:{m:02d}:{s:02d}",
    }


def queue_pending_size_summary():
    """Суммирует известные размеры ожидающих книг."""
    total = 0
    unknown = 0
    count = 0
    with queue_db_lock, queue_connect() as conn:
        rows = conn.execute("SELECT book_json FROM queue_items WHERE status IN ('pending','downloading')").fetchall()
    for row in rows:
        count += 1
        try:
            book = json.loads(row["book_json"])
            size = int(book.get("size_bytes") or parse_size_bytes(book.get("size", "")) or 0)
        except Exception:
            size = 0
        if size > 0:
            total += size
        else:
            unknown += 1
    return {"bytes": total, "text": human_bytes(total), "unknown": unknown, "count": count}


def queue_disk_guard():
    """Проверяет резерв места перед загрузкой очередной книги."""
    disk = disk_status()
    if disk["low"]:
        reason = f"Недостаточно свободного места: {disk['free_text']}; резерв {disk['min_free_gb']} ГБ"
        queue_setting_set("paused", "1")
        queue_setting_set("pause_reason", reason)
        return False, reason
    return True, ""


def queue_counts():
    """Группирует количество элементов очереди по состояниям."""
    result = {k: 0 for k in ('pending','downloading','done','skipped','error')}
    with queue_db_lock, queue_connect() as conn:
        for row in conn.execute("SELECT status, COUNT(*) AS c FROM queue_items GROUP BY status"):
            if row['status'] in result:
                result[row['status']] = row['c']
    return result



def queue_book_identity(book):
    """Возвращает source-aware opaque identity книги без runtime-конфигурации."""
    source_id = str(book.get("source_id") or LEGACY_QUEUE_SOURCE_ID)
    source_item_id = str(book.get("id") or "")
    return source_id, source_item_id


def queue_book_json_snapshot(book):
    """Создаёт независимый JSON-safe snapshot книги для очереди."""
    if not isinstance(book, dict):
        raise TypeError("Ожидается словарь книги")
    snapshot = copy.deepcopy(book)
    if "related" in snapshot:
        related = snapshot.get("related")
        if related is None:
            snapshot["related"] = []
        elif isinstance(related, (tuple, list)):
            snapshot["related"] = [
                {
                    "source_id": ref.source_id,
                    "url": ref.url,
                    "title": ref.title,
                    "kind": ref.kind,
                }
                if isinstance(ref, CatalogRef)
                else copy.deepcopy(ref)
                for ref in related
            ]
    json.dumps(snapshot, ensure_ascii=False)
    return snapshot


def queue_active_source_item_ids(source_id):
    """Возвращает opaque item IDs активной очереди только одного источника."""
    with queue_db_lock, queue_connect() as conn:
        rows = conn.execute(
            """SELECT source_item_id FROM queue_items
               WHERE source_id=? AND status IN ('pending','downloading')""",
            (str(source_id),),
        ).fetchall()
    return {str(row["source_item_id"]) for row in rows}


def queue_active_book_ids():
    """Сохраняет legacy-контракт active IDs в изолированном legacy namespace."""
    return queue_active_source_item_ids(LEGACY_QUEUE_SOURCE_ID)


def queue_pending_count():
    """Возвращает число книг, ожидающих фоновой загрузки."""
    return queue_counts().get('pending', 0)


def queue_active_exists(source_item_id, source_id=LEGACY_QUEUE_SOURCE_ID):
    """Проверяет source-aware identity среди активных элементов очереди."""
    with queue_db_lock, queue_connect() as conn:
        row = conn.execute(
            """SELECT 1 FROM queue_items
               WHERE source_id=? AND source_item_id=?
                 AND status IN ('pending','downloading')
               LIMIT 1""",
            (str(source_id), str(source_item_id)),
        ).fetchone()
        return bool(row)


def queue_add_book(book, format_mode='auto', download_duplicates=False, origin_kind='', origin_id='', origin_name=''):
    """Добавляет книгу с атомарной уникальностью active source-aware identity."""
    source_id, source_item_id = queue_book_identity(book)
    book_snapshot = queue_book_json_snapshot(book)
    with queue_db_lock, queue_connect() as conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO queue_items(
                   flibusta_id,source_id,source_item_id,title,author,book_json,
                   format_mode,download_duplicates,status,added_at,
                   origin_kind,origin_id,origin_name
               )
               VALUES(?,?,?,?,?,?,?,?,'pending',?,?,?,?)""",
            (
                source_item_id,
                source_id,
                source_item_id,
                display_text(book.get('title',''), 'Без названия'),
                display_text(book.get('author',''), 'Неизвестный автор'),
                json.dumps(book_snapshot, ensure_ascii=False),
                format_mode if format_mode in {'auto','epub','fb2'} else 'auto',
                1 if download_duplicates else 0,
                time.time(),
                origin_kind if origin_kind in {'author','series'} else '',
                str(origin_id or ''),
                display_text(origin_name, ''),
            ),
        )
        conn.commit()
        return bool(cur.rowcount)


def queue_list_items(limit=500):
    """Возвращает активную очередь в порядке приоритета."""
    with queue_db_lock, queue_connect() as conn:
        rows = conn.execute(
            """SELECT * FROM queue_items
               WHERE status IN ('downloading','pending') OR (status='error' AND retry_queued=0)
               ORDER BY
                 CASE status WHEN 'downloading' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END,
                 CASE WHEN status IN ('downloading','pending') THEN priority ELSE 0 END DESC,
                 id ASC
               LIMIT ?""",
            (int(limit),),
        ).fetchall()
    status_names = {'pending':'Ожидает','downloading':'Загружается','done':'Готово','skipped':'Пропущено','error':'Ошибка'}
    priority_names = {1:('Высокий','high'),0:('Обычный','normal'),-1:('Низкий','low')}
    result=[]
    for row in rows:
        item=dict(row)
        item['status_text']=status_names.get(item['status'], item['status'])
        item['format_text']={'auto':'EPUB → FB2','epub':'Только EPUB','fb2':'Только FB2'}.get(item['format_mode'],item['format_mode'])
        item['added_text']=format_time(item['added_at'])
        priority = max(-1, min(1, int(item.get('priority',0) or 0)))
        item['priority_text'], item['priority_class'] = priority_names[priority]
        try:
            book = json.loads(item['book_json'])
            size = int(book.get('size_bytes') or parse_size_bytes(book.get('size','')) or 0)
        except Exception:
            size = 0
        item['size_text'] = human_bytes(size) if size > 0 else ''
        result.append(item)
    return result


def queue_history_items(status_filter='all', search_query='', limit=200):
    """Возвращает отфильтрованную историю обработанных книг."""
    allowed = {'all','done','skipped','error'}
    if status_filter not in allowed:
        status_filter = 'all'
    sql = """SELECT * FROM queue_items WHERE status IN ('done','skipped','error')"""
    params = []
    if status_filter != 'all':
        sql += " AND status=?"
        params.append(status_filter)
    if search_query:
        sql += " AND (title LIKE ? OR author LIKE ?)"
        pattern = f"%{search_query}%"
        params.extend([pattern, pattern])
    sql += " ORDER BY COALESCE(finished_at,added_at) DESC, id DESC LIMIT ?"
    params.append(int(limit))
    with queue_db_lock, queue_connect() as conn:
        rows = conn.execute(sql, params).fetchall()

    status_names = {'done':'Готово','skipped':'Пропущено','error':'Ошибка'}
    priority_names = {1:'Высокий',0:'Обычный',-1:'Низкий'}
    items = []
    for row in rows:
        item = dict(row)
        item['status_text'] = status_names.get(item['status'], item['status'])
        item['format_text'] = {'auto':'EPUB → FB2','epub':'Только EPUB','fb2':'Только FB2'}.get(item['format_mode'], item['format_mode'])
        item['priority_text'] = priority_names.get(max(-1,min(1,int(item.get('priority',0) or 0))), 'Обычный')
        item['added_text'] = format_time(item.get('added_at'))
        item['started_text'] = format_time(item.get('started_at')) if item.get('started_at') else ''
        item['finished_text'] = format_time(item.get('finished_at')) if item.get('finished_at') else ''
        if item.get('started_at') and item.get('finished_at'):
            duration = max(0, float(item['finished_at']) - float(item['started_at']))
            item['duration_text'] = f"{duration:.1f} сек" if duration < 60 else f"{duration/60:.1f} мин"
        else:
            item['duration_text'] = ''
        items.append(item)
    return items


def queue_current_item():
    """Возвращает книгу, которую сейчас обрабатывает worker."""
    with queue_db_lock, queue_connect() as conn:
        row=conn.execute("SELECT * FROM queue_items WHERE status='downloading' ORDER BY id LIMIT 1").fetchone()
    return dict(row) if row else None


def queue_is_worker_active():
    """Проверяет, жив ли поток фоновой загрузки."""
    global queue_worker_thread
    with queue_worker_lock:
        return bool(queue_worker_thread and queue_worker_thread.is_alive())


def queue_update_item(item_id, **changes):
    """Обновляет разрешённые поля элемента очереди."""
    if not changes:
        return
    allowed={'status','started_at','finished_at','attempts','detail','error','run_id','downloaded_bytes','error_category','retry_queued','retry_of_id'}
    clean={k:v for k,v in changes.items() if k in allowed}
    if not clean:
        return
    sql=', '.join(f"{k}=?" for k in clean)
    values=list(clean.values())+[int(item_id)]
    with queue_db_lock, queue_connect() as conn:
        conn.execute(f"UPDATE queue_items SET {sql} WHERE id=?", values)
        conn.commit()


def queue_finish_item(item_id, status, detail, error='', downloaded_bytes=0, error_category=''):
    """Фиксирует окончательный результат обработки книги."""
    queue_update_item(
        item_id,
        status=status,
        finished_at=time.time(),
        error=error,
        error_category=error_category,
        downloaded_bytes=max(0, int(downloaded_bytes or 0)),
        detail=detail,
    )
    queue_run_increment(processed=1, downloaded_bytes=downloaded_bytes)


def queue_runtime_book(item, book):
    """Накладывает каноническую DB identity на сохранённый snapshot книги."""
    if not isinstance(book, dict):
        raise ValueError("Некорректные данные книги в очереди")
    runtime_book = dict(book)
    runtime_book["source_id"] = str(
        item.get("source_id") or LEGACY_QUEUE_SOURCE_ID
    )
    runtime_book["id"] = str(
        item.get("source_item_id") or item.get("flibusta_id") or ""
    )
    return runtime_book


def run_queue_worker():
    """Последовательно обрабатывает ожидающие книги очереди."""
    global queue_worker_thread
    try:
        queue_setting_set("run_active", "1")
        while True:
            if queue_setting_get('paused','0') == '1':
                break

            ok, _ = queue_disk_guard()
            if not ok:
                break

            queue_run_sync_total()
            with queue_db_lock, queue_connect() as conn:
                row=conn.execute(
                    "SELECT * FROM queue_items WHERE status='pending' ORDER BY priority DESC, id ASC LIMIT 1"
                ).fetchone()
            if not row:
                break

            item=dict(row)
            item_id=item['id']
            try:
                raw_book=json.loads(item['book_json'])
                book=queue_runtime_book(item,raw_book)
            except Exception as exc:
                queue_finish_item(
                    item_id,
                    'error',
                    'Ошибка данных',
                    error=f'Некорректные данные очереди: {compact_error(exc)}',
                    error_category='Данные очереди'
                )
                continue

            current_run_id = queue_setting_get("current_run_id", "")
            queue_update_item(item_id,status='downloading',run_id=current_run_id,started_at=time.time(),finished_at=None,error='',error_category='',downloaded_bytes=0,detail='Подготовка загрузки…')

            def report(payload):
                queue_update_item(item_id,detail=payload.get('detail',''),attempts=payload.get('attempt',0))

            try:
                duplicate_mode=bool(item.get('download_duplicates'))
                if duplicate_mode:
                    apply_duplicate_local_status(book)
                    already=book.get('duplicate_exists_any',False)
                else:
                    apply_local_status(book)
                    already=book.get('exists_any',False)

                if already:
                    queue_finish_item(item_id,'skipped','Уже существует локально')
                    time.sleep(BULK_DELAY)
                    continue

                # Проверяем свободное место повторно непосредственно перед сетевой загрузкой.
                ok, reason = queue_disk_guard()
                if not ok:
                    queue_update_item(item_id,status='pending',started_at=None,detail=reason)
                    break

                fmt=choose_bulk_format(book,item.get('format_mode','auto'))
                if not fmt:
                    raise RuntimeError('Нет подходящего формата')

                queue_update_item(item_id,detail='Ожидание свободного загрузчика…')
                with download_serial_lock:
                    result,path,elapsed=download_one_book(book,fmt,duplicate_mode=duplicate_mode,progress=report)

                if result=='skipped':
                    queue_finish_item(item_id,'skipped','Уже существует локально')
                else:
                    try:
                        downloaded_bytes = os.path.getsize(path)
                    except Exception:
                        downloaded_bytes = int(book.get("size_bytes") or 0)
                    queue_finish_item(
                        item_id,
                        'done',
                        f'Добавлено · {fmt.upper()} · {elapsed:.1f} сек',
                        downloaded_bytes=downloaded_bytes,
                    )
            except Exception as exc:
                error_info = download_error_info(exc)
                queue_finish_item(
                    item_id,
                    'error',
                    f"Ошибка: {error_info['label']} · переход к следующей книге",
                    error=compact_error(exc,300),
                    error_category=error_info["label"],
                )
            time.sleep(BULK_DELAY)
    finally:
        queue_setting_set("run_active", "0")
        current_run_id = queue_setting_get("current_run_id", "")
        if queue_pending_count() <= 0:
            finished_at = time.time()
            queue_setting_set("run_finished_at", finished_at)
            try:
                queue_run_finalize(current_run_id, finished_at)
                queue_create_completion_notification()
            except Exception:
                pass
        else:
            try:
                queue_run_mark_paused(current_run_id)
            except Exception:
                pass
        with queue_worker_lock:
            queue_worker_thread=None


def start_queue_worker(force_resume=False, continue_run=False, trigger="manual"):
    """Безопасно запускает единственный поток обработчика."""
    global queue_worker_thread
    if force_resume:
        queue_setting_set('paused','0')
        queue_setting_set('pause_reason','')

    with queue_worker_lock:
        if queue_worker_thread and queue_worker_thread.is_alive():
            return False

        if queue_setting_get('paused','0') == '1':
            return False

        if queue_pending_count() <= 0:
            return False

        ok, _ = queue_disk_guard()
        if not ok:
            return False

        current_run_id = queue_setting_get("current_run_id", "")
        if not continue_run or not current_run_id:
            queue_run_initialize(trigger=trigger)
        else:
            queue_setting_set("run_active","1")
            queue_setting_set("run_finished_at","0")
            with queue_db_lock, queue_connect() as conn:
                conn.execute(
                    "UPDATE queue_runs SET status='running', finished_at=NULL WHERE run_id=?",
                    (current_run_id,),
                )
                conn.commit()
            queue_run_sync_total()

        queue_worker_thread=threading.Thread(target=run_queue_worker,daemon=True,name='opds-queue-worker')
        queue_worker_thread.start()
        return True


def parse_tz_offset(value):
    """Преобразует строку UTC-смещения в timedelta."""
    m=re.fullmatch(r'([+-])(\d{2}):(\d{2})', value or '')
    if not m:
        return timedelta(hours=3)
    minutes=int(m.group(2))*60+int(m.group(3))
    if m.group(1)=='-': minutes=-minutes
    return timedelta(minutes=minutes)


def queue_scheduler_loop():
    """Проверяет расписание и запускает очередь вовремя."""
    while True:
        try:
            if queue_setting_get('auto_enabled','0')=='1' and queue_setting_get('paused','0')!='1':
                auto_time=queue_setting_get('auto_time',QUEUE_DEFAULT_TIME)
                m=re.fullmatch(r'(\d{2}):(\d{2})',auto_time)
                if m:
                    sched_minutes=int(m.group(1))*60+int(m.group(2))
                    offset=parse_tz_offset(queue_setting_get('tz_offset',QUEUE_DEFAULT_TZ_OFFSET))
                    now=datetime.now(timezone.utc)+offset
                    now_minutes=now.hour*60+now.minute
                    delta=(now_minutes-sched_minutes)%1440
                    if 0 <= delta < 60:
                        run_date=(now.date() if now_minutes>=sched_minutes else (now.date()-timedelta(days=1))).isoformat()
                        if queue_setting_get('last_auto_run_key','') != run_date and queue_pending_count()>0:
                            queue_setting_set('last_auto_run_key',run_date)
                            start_queue_worker(trigger="schedule")
        except Exception:
            pass
        time.sleep(QUEUE_SCHEDULER_INTERVAL)


def start_queue_scheduler():
    """Запускает единственный daemon-поток планировщика."""
    global queue_scheduler_thread
    if queue_scheduler_thread and queue_scheduler_thread.is_alive():
        return
    queue_scheduler_thread=threading.Thread(target=queue_scheduler_loop,daemon=True,name='opds-queue-scheduler')
    queue_scheduler_thread.start()


# ============================================================
# Массовые задания
# ============================================================

def persist_jobs():
    """Атомарно сохраняет последние массовые задания в JSON."""
    with jobs_lock:
        snapshot = json.loads(json.dumps(jobs, ensure_ascii=False))
    tmp = JOB_STATE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f: json.dump(snapshot, f, ensure_ascii=False, indent=2)
        os.replace(tmp, JOB_STATE_FILE)
    except Exception:
        if os.path.exists(tmp):
            try: os.remove(tmp)
            except Exception: pass


def load_jobs():
    """Загружает задания и отмечает прерванные перезапуском."""
    if not os.path.isfile(JOB_STATE_FILE): return
    try:
        with open(JOB_STATE_FILE, "r", encoding="utf-8") as f: saved = json.load(f)
        if not isinstance(saved, dict): return
        for jid, job in saved.items():
            if job.get("status") in {"pending", "running"}:
                job["status"] = "interrupted"; job["status_text"] = "Прервано перезапуском сервиса"; job["current"] = ""
            job.setdefault("created_at", time.time())
            job.setdefault("origin_kind", "")
            job.setdefault("origin_id", "")
            job.setdefault("origin_name", "")
            job.setdefault("origin_page", 0)
            job.setdefault("origin_view", "")
            job.setdefault("duplicates_filtered", 0)
            job.setdefault("download_duplicates", False)
            job.setdefault("selected_count", job.get("total", 0))
            job.setdefault("existing_filtered", 0)
            job.setdefault("current_detail", "")
            job.setdefault("current_attempt", 0)
            job.setdefault("current_attempt_max", 0)
            job.setdefault("current_format", "")
            job.setdefault("current_stage", "")
            job.setdefault("current_elapsed", 0.0)
            jobs[jid] = job
    except Exception: pass


def job_update(job_id, **changes):
    """Потокобезопасно обновляет поля массового задания."""
    with jobs_lock:
        if job_id in jobs: jobs[job_id].update(changes)
    persist_jobs()


def job_snapshot(job_id):
    """Возвращает независимую копию состояния задания."""
    with jobs_lock:
        job = jobs.get(job_id)
        if not job: return None
        snap = json.loads(json.dumps(job, ensure_ascii=False))
    ret, label = catalog_return(snap.get("origin_kind"), snap.get("origin_id"), snap.get("origin_name"), snap.get("origin_page", 0), snap.get("origin_view", "")) if snap.get("origin_kind") in {"author","series"} else ("/", "Вернуться к поиску")
    snap["return_url"], snap["return_label"] = ret, label
    return snap


def create_job(title, books_to_download, format_mode, origin_kind, origin_id, origin_name, duplicates_filtered=0, download_duplicates=False, selected_count=None, existing_filtered=0, origin_page=0, origin_view=""):
    """Создаёт массовое задание и запускает отдельный поток."""
    job_id = uuid.uuid4().hex[:12]
    if selected_count is None:
        selected_count = len(books_to_download)
    try:
        origin_page = max(0, min(int(origin_page or 0), MAX_CATALOG_PAGES - 1))
    except (TypeError, ValueError):
        origin_page = 0
    job = {"id": job_id, "title": display_text(title, "Массовая загрузка"), "format_mode": format_mode, "status": "pending", "status_text": "Ожидание запуска", "total": len(books_to_download), "processed": 0, "downloaded": 0, "skipped": 0, "error_count": 0, "current": "", "current_detail": "", "current_attempt": 0, "current_attempt_max": 0, "current_format": "", "current_stage": "", "current_elapsed": 0.0, "errors": [], "failed_books": [], "cancel": False, "created_at": time.time(), "finished_at": None, "origin_kind": origin_kind, "origin_id": str(origin_id), "origin_name": display_text(origin_name, ""), "origin_page": origin_page, "origin_view": "all" if origin_view == "all" else "", "duplicates_filtered": int(duplicates_filtered or 0), "download_duplicates": bool(download_duplicates), "selected_count": int(selected_count or 0), "existing_filtered": int(existing_filtered or 0)}
    with jobs_lock: jobs[job_id] = job
    persist_jobs()
    threading.Thread(target=run_bulk_job, args=(job_id, books_to_download), daemon=True).start()
    return job_id


def run_bulk_job(job_id, books_to_download):
    """Скачивает книги и последовательно обновляет прогресс."""
    job_update(job_id, status="running", status_text="Загрузка выполняется")
    for book in books_to_download:
        current = job_snapshot(job_id)
        if not current:
            return
        if current.get("cancel"):
            job_update(
                job_id,
                status="cancelled",
                status_text="Загрузка остановлена",
                current="",
                current_detail="",
                finished_at=time.time(),
            )
            return

        title = f"{book['author']} / {book['title']}"
        job_update(
            job_id,
            current=title,
            current_detail="Подготовка загрузки…",
            current_attempt=0,
            current_attempt_max=DOWNLOAD_RETRY_ATTEMPTS,
            current_format="",
            current_stage="prepare",
            current_elapsed=0.0,
        )

        def report_download(payload):
            job_update(
                job_id,
                current_detail=payload.get("detail", ""),
                current_attempt=payload.get("attempt", 0),
                current_attempt_max=payload.get("attempt_max", 0),
                current_format=payload.get("format", ""),
                current_stage=payload.get("stage", ""),
                current_elapsed=payload.get("elapsed", 0.0),
            )

        try:
            current = job_snapshot(job_id)
            duplicate_mode = bool(current.get("download_duplicates", False))
            if duplicate_mode:
                apply_duplicate_local_status(book)
                already_exists = book.get("duplicate_exists_any", False)
            else:
                apply_local_status(book)
                already_exists = book.get("exists_any", False)

            if already_exists:
                job_update(
                    job_id,
                    skipped=current["skipped"] + 1,
                    processed=current["processed"] + 1,
                    current_detail="Пропущено · файл уже существует",
                    current_stage="skipped",
                )
                time.sleep(BULK_DELAY)
                continue

            fmt = choose_bulk_format(book, current["format_mode"])
            if not fmt:
                raise RuntimeError("Нет подходящего формата")

            job_update(job_id, current_detail="Ожидание свободного загрузчика…", current_stage="waiting")
            with download_serial_lock:
                result, _, elapsed = download_one_book(
                    book,
                    fmt,
                    duplicate_mode=duplicate_mode,
                    progress=report_download,
                )
            current = job_snapshot(job_id)
            if result == "skipped":
                job_update(
                    job_id,
                    skipped=current["skipped"] + 1,
                    processed=current["processed"] + 1,
                    current_detail="Пропущено · файл уже существует",
                    current_stage="skipped",
                )
            else:
                job_update(
                    job_id,
                    downloaded=current["downloaded"] + 1,
                    processed=current["processed"] + 1,
                    current_detail=f"Добавлено · {fmt.upper()} · {elapsed:.1f} сек",
                    current_stage="success",
                    current_elapsed=elapsed,
                )
        except Exception as exc:
            current = job_snapshot(job_id)
            err_text = compact_error(exc, 300)
            err = (current.get("errors", []) + [f"{title}: {err_text}"])[-50:]
            failed = current.get("failed_books", []) + [book]
            job_update(
                job_id,
                error_count=current["error_count"] + 1,
                processed=current["processed"] + 1,
                errors=err,
                failed_books=failed,
                current_detail=f"Ошибка · {err_text} · переход к следующей книге",
                current_stage="failed",
            )
        time.sleep(BULK_DELAY)

    job_update(
        job_id,
        status="finished",
        status_text="Загрузка завершена",
        current="",
        current_detail="Все книги в очереди обработаны",
        current_attempt=0,
        current_attempt_max=0,
        current_format="",
        current_stage="finished",
        current_elapsed=0.0,
        finished_at=time.time(),
    )

def format_time(ts):
    """
    Все временные метки храним как Unix time (UTC), но показываем
    в часовом поясе, выбранном на странице очереди.
    """
    try:
        offset = parse_tz_offset(
            queue_setting_get("tz_offset", QUEUE_DEFAULT_TZ_OFFSET)
        )
        local_tz = timezone(offset)
        return datetime.fromtimestamp(float(ts), tz=local_tz).strftime("%d.%m.%Y %H:%M")
    except Exception:
        return "—"


def render_error_page(title, message, status_code):
    """Рендерит локальную HTML-страницу ошибки с сохранением HTTP-статуса."""
    return render_template_string(
        ERROR_HTML,
        css=COMMON_CSS,
        title=title,
        message=message,
        status_code=status_code,
    ), status_code


# ============================================================
# Flask-маршруты
# ============================================================

@app.route("/setup")
def setup_page():
    """Показывает мастер выбора библиотеки при первом запуске."""
    return render_template_string(
        SETUP_HTML,
        css=COMMON_CSS,
        destination=DESTINATION,
    )
@app.route("/settings")
def settings_page():
    """Показывает настройки каталога библиотеки."""
    return render_template_string(
        SETTINGS_HTML,
        css=COMMON_CSS,
        destination=DESTINATION,
    )


@app.route("/settings/opds", methods=["GET", "POST"])
def opds_settings_page():
    """Проверяет и сохраняет только явно указанный пользователем OPDS."""
    message = ""
    error = ""
    status = 200

    if request.method == "POST":
        action = request.form.get("action", "")
        if action == "save":
            url = request.form.get("opds_url", "").strip()
            if not url:
                error = "Укажите адрес OPDS-каталога."
            else:
                try:
                    validation = configure_opds_source(url)
                except (OSError, ValueError, requests.RequestException):
                    validation = None
                if validation is not None and validation.valid is True:
                    return redirect(url_for("opds_settings_page", saved="1"))
                error = "Не удалось проверить OPDS-источник."
        elif action == "clear":
            try:
                clear_configured_opds_source()
            except (OSError, ValueError):
                error = "Не удалось удалить OPDS-источник."
            else:
                return redirect(url_for("opds_settings_page", cleared="1"))
        else:
            error = "Некорректное действие."
            status = 400
    elif request.args.get("saved") == "1":
        message = "OPDS-источник сохранён."
    elif request.args.get("cleared") == "1":
        message = "OPDS-источник удалён."

    source = current_source_config()
    return render_template_string(
        OPDS_SETTINGS_HTML,
        configured=has_configured_opds_source(),
        source_name=source.display_name,
        opds_url=source.root_url,
        message=message,
        error=error,
    ), status


@app.route("/")
def index():
    """Показывает setup либо нейтральную домашнюю страницу."""
    if not APP_CONFIG.get("setup_complete", False):
        return setup_page()

    source = current_source_config()
    return render_template_string(
        NEUTRAL_HOME_HTML,
        configured=has_configured_opds_source(),
        source_name=source.display_name,
    )



@app.route("/api/health")
def health_api():
    """Возвращает JSON-снимок состояния для панели интерфейса."""
    force = request.args.get("force") == "1"
    return jsonify(health_snapshot(force=force))


@app.route("/search-return")
def search_return():
    """Вернуться именно к последней странице результатов поиска."""
    target = session.get("last_search_url", "")
    if isinstance(target, str) and target.startswith("/") and not target.startswith("//"):
        return redirect(target)
    return redirect(url_for("index"))


@app.route("/context-return")
def context_return():
    """
    Вернуться к последнему рабочему экрану:
    результаты поиска, все книги автора или серия.
    Очередь/История/Уведомления сами этот контекст не перезаписывают.
    """
    target = session.get("last_context_url", "")
    if isinstance(target, str) and target.startswith("/") and not target.startswith("//"):
        return redirect(target)

    target = session.get("last_search_url", "")
    if isinstance(target, str) and target.startswith("/") and not target.startswith("//"):
        return redirect(target)

    return redirect(url_for("index"))



@app.route("/image")
def image_proxy():
    """Передаёт разрешённые изображения Flibusta интерфейсу."""
    href = request.args.get("href", "").strip()
    if not (href.startswith("/i/") or href.startswith("/ia/")) or ".." in href: return Response(status=404)
    try:
        r = legacy_opds_get(urljoin(LEGACY_OPDS_BASE, href))
        ctype = r.headers.get("Content-Type", "")
        if not ctype.startswith("image/"): return Response(status=404)
        if len(r.content) > MAX_IMAGE_SIZE: return Response(status=413)
        return Response(r.content, content_type=ctype, headers={"Cache-Control": "public, max-age=86400"})
    except Exception: return Response(status=404)



def catalog_series_summary(books):
    """Готовит счётчики серий для фильтра каталога."""
    groups = {}
    no_series_count = 0
    for book in books:
        links = book.get("series_links") or []
        ids = []
        for item in links:
            sid = str(item.get("id") or "")
            name = display_text(item.get("name") or "", f"Серия {sid}")
            if not sid:
                continue
            ids.append(sid)
            group = groups.setdefault(sid, {"id": sid, "name": name, "count": 0})
            group["count"] += 1
        book["series_ids"] = ",".join(ids)
        if not ids:
            no_series_count += 1
    ordered = sorted(groups.values(), key=lambda x: normalize_author_text(x["name"]))
    return ordered, no_series_count


def render_catalog(kind, catalog_id, name, page=0, view_all=False):
    """Отображает постраничный либо полный каталог."""
    # Очередь должна уметь вернуться именно сюда, а не только к поиску,
    # из которого пользователь когда-то открыл автора/серию.
    session["last_context_url"] = request.full_path.rstrip("?")

    page = 0 if view_all else max(0, int(page))
    if view_all:
        result = get_cached_catalog(kind, catalog_id)
    else:
        result = get_cached_catalog_page(kind, catalog_id, page)
    books = result.get("books", [])
    if kind == "author":
        title = f"Все книги автора: {name}" if name else (result.get("title") or f"Автор {catalog_id}")
    else:
        title = f"Серия: {name}" if name else (result.get("title") or f"Серия {catalog_id}")

    series_groups, no_series_count = catalog_series_summary(books)
    existing = sum(1 for b in books if b["exists_any"])
    selection_storage_key = catalog_selection_storage_key(
        current_source_id(),
        str(kind),
        str(catalog_id),
    )
    clear_selection = catalog_selection_clear_pending(kind, catalog_id)
    rendered = render_template_string(
        CATALOG_HTML,
        css=COMMON_CSS,
        title=title,
        kind=kind,
        catalog_id=catalog_id,
        name=name,
        page=page,
        view_all=bool(view_all),
        has_next=False if view_all else bool(result.get("has_next", False)),
        books=books,
        total=len(books),
        existing=existing,
        duplicate_groups=result.get("duplicate_groups", 0) if view_all else 0,
        duplicate_extra=result.get("duplicate_extra", 0) if view_all else 0,
        queue_pending_count=queue_pending_count(),
        queued_ids=queue_active_book_ids(),
        series_groups=series_groups,
        no_series_count=no_series_count,
        selection_storage_key=selection_storage_key,
        clear_selection=clear_selection,
    )
    if clear_selection:
        catalog_selection_clear_pending(kind, catalog_id, consume=True)
    return rendered


@app.route("/author/<author_id>")
def author_catalog(author_id):
    """Показывает каталог выбранного автора."""
    if not re.fullmatch(r"\d+", author_id):
        return render_error_page(
            "Автор не найден",
            "Некорректный идентификатор автора.",
            404,
        )
    try:
        page = 0
        try:
            page = max(0, int(request.args.get("page", "0")))
        except ValueError:
            page = 0
        view_all = request.args.get("view", "") == "all"
        return render_catalog("author", author_id, request.args.get("name", "").strip(), page=page, view_all=view_all)
    except Exception as exc: flash(f"Ошибка загрузки каталога автора: {exc}"); return redirect(url_for("index"))


@app.route("/series/<series_id>")
def series_catalog(series_id):
    """Показывает каталог выбранной серии."""
    if not re.fullmatch(r"\d+", series_id):
        return render_error_page(
            "Серия не найдена",
            "Некорректный идентификатор серии.",
            404,
        )
    try:
        page = 0
        try:
            page = max(0, int(request.args.get("page", "0")))
        except ValueError:
            page = 0
        view_all = request.args.get("view", "") == "all"
        return render_catalog("series", series_id, request.args.get("name", "").strip(), page=page, view_all=view_all)
    except Exception as exc: flash(f"Ошибка загрузки серии: {exc}"); return redirect(url_for("index"))


@app.route("/search/opds", methods=["GET"])
def opds_search_page():
    """Показывает read-only результаты поиска текущего OPDS-источника."""
    try:
        query = normalize_opds_search_query(request.args.get("q", ""))
    except ValueError:
        return render_template_string(
            OPDS_SEARCH_HTML,
            view=None,
            error_message="Укажите поисковый запрос.",
            show_settings_link=False,
        ), 400

    try:
        raw_page = request.args.get("page")
        page = 0 if raw_page is None else int(raw_page)
        page = _validate_opds_search_page_number(page)
    except (TypeError, ValueError):
        return render_template_string(
            OPDS_SEARCH_HTML,
            view=None,
            error_message="Некорректный номер страницы.",
            show_settings_link=False,
        ), 400

    try:
        search_page = load_current_opds_search_page(
            query,
            page=page,
            force=False,
        )
    except ValueError as exc:
        error = str(exc)
        if error == "OPDS-источник не настроен":
            message = "OPDS-источник не настроен."
            status = 409
            show_settings_link = True
        elif error == "Этот OPDS-источник не предоставляет поиск":
            message = "Этот OPDS-источник не предоставляет поиск."
            status = 409
            show_settings_link = False
        else:
            message = "Не удалось загрузить результаты OPDS-поиска."
            status = 502
            show_settings_link = False
        return render_template_string(
            OPDS_SEARCH_HTML,
            view=None,
            error_message=message,
            show_settings_link=show_settings_link,
        ), status
    except (RuntimeError, RequestException):
        return render_template_string(
            OPDS_SEARCH_HTML,
            view=None,
            error_message="Не удалось загрузить результаты OPDS-поиска.",
            show_settings_link=False,
        ), 502

    current_url = request.full_path.rstrip("?")
    session["last_context_url"] = current_url
    session["last_search_url"] = current_url
    selection_storage_key = catalog_selection_storage_key(
        search_page.source_id,
        "search",
        query,
    )
    clear_selection = catalog_selection_clear_pending("search", query)
    view = build_opds_search_view(
        search_page,
        query,
        page=page,
    )
    rendered = render_template_string(
        OPDS_SEARCH_HTML,
        view=view,
        error_message="",
        show_settings_link=False,
        selection_storage_key=selection_storage_key,
        clear_selection=clear_selection,
    )
    if clear_selection:
        catalog_selection_clear_pending("search", query, consume=True)
    return rendered


@app.post("/search/opds/queue")
def opds_search_queue_add():
    """Добавляет server-resolved neutral OPDS search selection в очередь."""
    try:
        query = normalize_opds_search_query(request.form.get("q", ""))
    except ValueError:
        return Response("Укажите поисковый запрос.", status=400)

    source = current_source_config()
    if not source.root_url or not source.source_id:
        return Response("OPDS-источник не настроен.", status=409)

    raw_source_item_ids = request.form.getlist("book_id")
    if len(raw_source_item_ids) > MAX_OPDS_SEARCH_QUEUE_SELECTION:
        return Response("Выбрано слишком много книг.", status=400)
    source_item_ids = unique_opaque_ids(raw_source_item_ids)
    if not source_item_ids:
        return Response("Не выбрана ни одна книга", status=400)

    format_mode = request.form.get("format_mode", "auto")
    if format_mode not in {"auto", "epub", "fb2"}:
        format_mode = "auto"

    try:
        books = resolve_opds_search_selection(
            source.source_id,
            query,
            source_item_ids,
        )
    except ValueError as exc:
        flash(str(exc))
        return redirect(url_for("opds_search_page", q=query), code=303)

    added = 0
    existing_filtered = 0
    already_queued = 0
    unsupported_format = 0
    for book in books:
        apply_local_status(book)
        if book.get("exists_any"):
            existing_filtered += 1
            continue
        if choose_catalog_book_format(book, format_mode) is None:
            unsupported_format += 1
            continue
        if queue_add_book(book, format_mode, False):
            added += 1
        else:
            already_queued += 1

    parts = [f"В очередь добавлено: {added}"]
    if existing_filtered:
        parts.append(f"уже локально: {existing_filtered}")
    if already_queued:
        parts.append(f"уже в очереди: {already_queued}")
    if unsupported_format:
        parts.append(f"неподдерживаемый формат: {unsupported_format}")
    flash(" · ".join(parts))
    mark_catalog_selection_clear("search", query)
    return redirect(url_for("opds_search_page", q=query), code=303)


@app.route("/catalog/opds/<token>", methods=["GET"])
def registered_catalog_page(token):
    """Показывает зарегистрированный OPDS-каталог только для чтения."""
    try:
        page = max(0, int(request.args.get("page", "0")))
    except (TypeError, ValueError):
        page = 0
    view_all = request.args.get("view", "") == "all"
    try:
        if page == 0:
            resolved_token = resolve_preferred_registered_catalog_token(token)
            if resolved_token != token:
                redirect_values = {"token": resolved_token}
                if view_all:
                    redirect_values["view"] = "all"
                return redirect(
                    url_for("registered_catalog_page", **redirect_values)
                )
        view = build_registered_catalog_view(
            token,
            page=page,
            view_all=view_all,
        )
    except ValueError as exc:
        if str(exc) == "OPDS-каталог недоступен или устарел":
            message = "OPDS-каталог недоступен или устарел."
            status = 404
        else:
            message = "Не удалось загрузить OPDS-каталог."
            status = 502
        return render_template_string(
            REGISTERED_CATALOG_HTML,
            view=None,
            error_message=message,
        ), status
    except (RuntimeError, requests.RequestException):
        return render_template_string(
            REGISTERED_CATALOG_HTML,
            view=None,
            error_message="Не удалось загрузить OPDS-каталог.",
        ), 502
    return render_template_string(
        REGISTERED_CATALOG_HTML,
        view=view,
        error_message="",
    )


@app.route("/catalog/opds", methods=["GET"])
def open_current_opds_catalog():
    """Открывает корень текущего настроенного OPDS-источника без сети."""
    if not has_configured_opds_source():
        return render_template_string(
            REGISTERED_CATALOG_HTML,
            view=None,
            error_message="OPDS-источник не настроен.",
        ), 409
    try:
        token = register_current_root_catalog()
    except ValueError:
        return render_template_string(
            REGISTERED_CATALOG_HTML,
            view=None,
            error_message="Не удалось открыть OPDS-каталог.",
        ), 409
    if token is None:
        return render_template_string(
            REGISTERED_CATALOG_HTML,
            view=None,
            error_message="OPDS-источник не настроен.",
        ), 409
    return redirect(
        url_for(
            "registered_catalog_page",
            token=token,
        )
    )


@app.post("/queue/add-bulk")
def queue_add_bulk():
    """Передаёт выбранные книги каталога в очередь."""
    kind=request.form.get('kind','')
    catalog_id=request.form.get('catalog_id','')
    catalog_name=display_text(request.form.get('catalog_name',''),'Очередь')
    format_mode=request.form.get('format_mode','auto')
    selected_ids=set(request.form.getlist('book_id'))
    download_duplicates=request.form.get('download_duplicates')=='1'
    if kind not in {'author','series'} or not re.fullmatch(r'\d+',catalog_id):
        flash('Некорректный каталог'); return redirect(url_for('index'))
    if format_mode not in {'auto','epub','fb2'}: format_mode='auto'
    if not selected_ids:
        flash('Не выбрана ни одна книга'); return redirect(request.referrer or url_for('index'))
    try:
        origin_page=max(0,min(int(request.form.get('origin_page','0')),MAX_CATALOG_PAGES-1))
    except (TypeError,ValueError):
        origin_page=0
    origin_view='all' if request.form.get('origin_view')=='all' else ''
    try:
        result=get_cached_catalog(kind,catalog_id)
    except Exception as exc:
        flash(f'Не удалось получить каталог: {exc}'); return redirect(url_for('index'))
    selected,duplicates_filtered,selected_count,existing_filtered=select_books_for_job(result['books'],selected_ids,download_duplicates=download_duplicates)
    origin_name=catalog_name.split(':',1)[1].strip() if ':' in catalog_name else catalog_name
    return_url,_=catalog_return(kind,catalog_id,origin_name,origin_page,origin_view)
    added=0; already_queued=0
    for book in selected:
        if queue_add_book(book,format_mode,download_duplicates,kind,catalog_id,origin_name): added+=1
        else: already_queued+=1
    parts=[f'В очередь добавлено: {added}']
    if existing_filtered: parts.append(f'уже было локально: {existing_filtered}')
    if duplicates_filtered: parts.append(f'альтернативных дублей исключено: {duplicates_filtered}')
    if already_queued: parts.append(f'уже находилось в очереди: {already_queued}')
    flash(' · '.join(parts))
    response = redirect(return_url, code=303)
    mark_catalog_selection_clear(kind, catalog_id)
    return response


@app.route("/queue")
def queue_page():
    """Показывает очередь, расписание и дисковый статус."""
    counts=queue_counts()
    items=queue_list_items()
    paused=queue_setting_get('paused','0')=='1'
    disk=disk_status()
    size_summary=queue_pending_size_summary()
    run=queue_run_progress()
    min_free_gb=queue_setting_get('min_free_gb',str(QUEUE_DEFAULT_MIN_FREE_GB))
    return render_template_string(
        QUEUE_HTML,css=COMMON_CSS,counts=counts,items=items,
        worker_active=queue_is_worker_active(),current_item=queue_current_item(),paused=paused,
        pause_reason=queue_setting_get('pause_reason',''),
        auto_enabled=queue_setting_get('auto_enabled','0')=='1',
        auto_time=queue_setting_get('auto_time',QUEUE_DEFAULT_TIME),
        tz_offset=queue_setting_get('tz_offset',QUEUE_DEFAULT_TZ_OFFSET),
        tz_options=['-05:00','+00:00','+01:00','+02:00','+03:00','+04:00','+05:00','+06:00','+08:00','+09:00'],
        disk=disk,size_summary=size_summary,run=run,min_free_gb=min_free_gb,destination=DESTINATION,
    )


@app.route("/api/queue/state")
def queue_state_api():
    """Возвращает текущее состояние фонового worker."""
    counts=queue_counts()
    current=queue_current_item()
    if current:
        current={
            "id":current.get("id"),
            "title":current.get("title",""),
            "author":current.get("author",""),
            "detail":current.get("detail",""),
        }
    return jsonify({
        "counts":counts,
        "worker_active":queue_is_worker_active(),
        "paused":queue_setting_get('paused','0')=='1',
        "pause_reason":queue_setting_get('pause_reason',''),
        "current_item":current,
        "disk":disk_status(),
        "size_summary":queue_pending_size_summary(),
        "run":queue_run_progress(),
        "min_free_gb":queue_setting_get('min_free_gb',str(QUEUE_DEFAULT_MIN_FREE_GB)),
    })


@app.post("/queue/settings")
def queue_settings():
    """Проверяет и сохраняет параметры расписания."""
    enabled='1' if request.form.get('auto_enabled')=='1' else '0'
    auto_time=request.form.get('auto_time',QUEUE_DEFAULT_TIME)
    tz_offset=request.form.get('tz_offset',QUEUE_DEFAULT_TZ_OFFSET)
    min_free_raw=request.form.get('min_free_gb',str(QUEUE_DEFAULT_MIN_FREE_GB))

    if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',auto_time):
        flash('Некорректное время запуска'); return redirect(url_for('queue_page'))
    if not re.fullmatch(r'[+-](?:0\d|1[0-4]):[0-5]\d',tz_offset):
        tz_offset=QUEUE_DEFAULT_TZ_OFFSET
    try:
        min_free_gb=int(min_free_raw)
        if not 1 <= min_free_gb <= 500:
            raise ValueError
    except Exception:
        flash('Минимальный свободный остаток должен быть от 1 до 500 ГБ')
        return redirect(url_for('queue_page'))

    queue_setting_set('auto_enabled',enabled)
    queue_setting_set('auto_time',auto_time)
    queue_setting_set('tz_offset',tz_offset)
    queue_setting_set('min_free_gb',min_free_gb)

    # Если места снова достаточно, снимаем только автоматическую "дисковую" причину паузы.
    reason=queue_setting_get('pause_reason','')
    if reason.startswith('Недостаточно свободного места'):
        disk=disk_status()
        if not disk['low']:
            queue_setting_set('pause_reason','')

    flash(f"Настройки сохранены: {'автозапуск включён' if enabled=='1' else 'автозапуск выключен'} · {auto_time} UTC{tz_offset} · резерв {min_free_gb} ГБ")
    return redirect(url_for('queue_page'))


@app.post("/queue/start")
def queue_start_now():
    """Запускает ожидающие книги вручную."""
    if start_queue_worker(force_resume=True, trigger="manual"):
        flash('Очередь запущена')
    elif queue_pending_count()==0:
        flash('В очереди нет ожидающих книг')
    elif queue_setting_get('paused','0')=='1':
        flash(queue_setting_get('pause_reason','Очередь находится на паузе'))
    else:
        flash('Очередь уже выполняется')
    return redirect(url_for('queue_page'))


@app.post("/queue/pause")
def queue_pause():
    """Ставит очередь на паузу после текущей операции."""
    queue_setting_set('paused','1')
    queue_setting_set('pause_reason','Поставлено на паузу пользователем')
    flash('Пауза включена. Текущая книга завершится, следующая не начнётся.')
    return redirect(url_for('queue_page'))


@app.post("/queue/resume")
def queue_resume():
    """Снимает паузу и продолжает обработку очереди."""
    queue_setting_set('paused','0')
    queue_setting_set('pause_reason','')
    started=start_queue_worker(continue_run=True)
    if started:
        flash('Очередь продолжена')
    elif queue_pending_count()==0:
        flash('В очереди нет ожидающих книг')
    else:
        flash(queue_setting_get('pause_reason','Не удалось продолжить очередь'))
    return redirect(url_for('queue_page'))


@app.post("/queue/priority/<int:item_id>/<direction>")
def queue_priority(item_id,direction):
    """Изменяет относительный приоритет ожидающей книги."""
    if direction not in {'up','down'}:
        return Response('Некорректное направление',status=400)
    with queue_db_lock, queue_connect() as conn:
        row=conn.execute("SELECT status,priority FROM queue_items WHERE id=?",(item_id,)).fetchone()
        if not row:
            flash('Элемент очереди не найден')
        elif row['status']!='pending':
            flash('Приоритет можно менять только у ожидающей книги')
        else:
            current=int(row['priority'] or 0)
            new=max(-1,min(1,current+(1 if direction=='up' else -1)))
            conn.execute("UPDATE queue_items SET priority=? WHERE id=?",(new,item_id))
            conn.commit()
    return redirect(url_for('queue_page'))


@app.post("/queue/remove/<int:item_id>")
def queue_remove(item_id):
    """Удаляет из очереди ещё не начатую книгу."""
    with queue_db_lock, queue_connect() as conn:
        row=conn.execute("SELECT status,retry_of_id FROM queue_items WHERE id=?",(item_id,)).fetchone()
        if row and row['status']!='downloading':
            if row["status"] == "pending" and row["retry_of_id"]:
                conn.execute("UPDATE queue_items SET retry_queued=0 WHERE id=?", (row["retry_of_id"],))
            conn.execute("DELETE FROM queue_items WHERE id=?",(item_id,))
            conn.commit()
            flash('Элемент удалён из очереди')
        elif row:
            flash('Нельзя удалить книгу во время загрузки')
    return redirect(url_for('queue_page'))


@app.post("/queue/clear-pending")
def queue_clear_pending():
    """Очищает все ожидающие элементы очереди."""
    with queue_db_lock, queue_connect() as conn:
        cur=conn.execute("DELETE FROM queue_items WHERE status='pending'"); conn.commit()
    flash(f'Удалено ожидающих элементов: {cur.rowcount}')
    return redirect(url_for('queue_page'))


@app.post("/queue/retry-errors")
def queue_retry_errors():
    """Возвращает ошибочные элементы истории в очередь."""
    added, skipped = queue_retry_error_copies()
    message = f"Добавлено повторов после ошибок: {added}"
    if skipped:
        message += f" · уже в очереди: {skipped}"
    flash(message)
    return redirect(url_for('queue_page'))



@app.route("/runs")
def queue_runs_page():
    """Показывает список сохранённых запусков."""
    return render_template_string(
        RUNS_HTML,
        css=COMMON_CSS,
        runs=queue_runs_list(100),
    )


@app.route("/runs/<run_id>")
def queue_run_detail(run_id):
    """Показывает книги и итоги выбранного запуска."""
    if not re.fullmatch(r"[0-9a-fA-F]{32}", run_id or ""):
        return render_error_page(
            "Некорректный запуск",
            "Некорректный run_id.",
            400,
        )
    run = queue_run_summary(run_id)
    if not run:
        return render_error_page(
            "Запуск не найден",
            "Запуск не найден.",
            404,
        )
    return render_template_string(
        RUN_DETAIL_HTML,
        css=COMMON_CSS,
        run=run,
        items=queue_run_items(run_id),
    )


@app.post("/runs/<run_id>/retry-errors")
def queue_retry_run_errors(run_id):
    """Повторно ставит в очередь ошибки выбранного запуска."""
    if not re.fullmatch(r"[0-9a-fA-F]{32}", run_id or ""):
        return Response("Некорректный run_id", status=400)
    if not queue_run_summary(run_id):
        return Response("Запуск не найден", status=404)
    added, skipped = queue_retry_error_copies(run_id)
    message = f"Ошибки запуска добавлены в очередь: {added}"
    if skipped:
        message += f" · уже в очереди: {skipped}"
    flash(message)
    return redirect(url_for("queue_page"))


@app.route("/queue/history")
def queue_history():
    """Показывает фильтруемую историю обработки книг."""
    status_filter = request.args.get("status", "all").strip().lower()
    if status_filter not in {"all","done","skipped","error"}:
        status_filter = "all"
    search_query = request.args.get("q", "").strip()
    counts = queue_counts()
    items = queue_history_items(status_filter=status_filter, search_query=search_query)
    return render_template_string(
        HISTORY_HTML,
        css=COMMON_CSS,
        counts=counts,
        items=items,
        status_filter=status_filter,
        search_query=search_query,
    )


@app.post("/queue/history/clear")
def queue_clear_history():
    """Очищает завершённые записи истории."""
    with queue_db_lock, queue_connect() as conn:
        cur = conn.execute("DELETE FROM queue_items WHERE status IN ('done','skipped')")
        conn.commit()
    flash(f"Удалено из истории: {cur.rowcount}. Ошибки сохранены.")
    return redirect(url_for("queue_history"))


@app.route("/notifications")
def notifications_page():
    """Показывает внутренние уведомления приложения."""
    notices = notification_list(100)
    notification_mark_all_read()
    return render_template_string(NOTIFICATIONS_HTML, css=COMMON_CSS, notices=notices)


@app.post("/notifications/clear")
def notifications_clear():
    """Очищает сохранённые внутренние уведомления."""
    with queue_db_lock, queue_connect() as conn:
        conn.execute("DELETE FROM notifications")
        conn.commit()
    flash("Уведомления очищены")
    return redirect(url_for("notifications_page"))


@app.route("/api/notifications/latest")
def notifications_latest_api():
    """Возвращает последние уведомления для клиентского опроса."""
    with queue_db_lock, queue_connect() as conn:
        row = conn.execute("SELECT * FROM notifications ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return jsonify({"id": 0})
    item = dict(row)
    item["created_text"] = format_time(item.get("created_at"))
    return jsonify(item)


@app.post("/bulk-start")
def bulk_start():
    """Создаёт массовое задание из выбора каталога."""
    kind = request.form.get("kind", "")
    catalog_id = request.form.get("catalog_id", "")
    catalog_name = display_text(request.form.get("catalog_name", ""), "Массовая загрузка")
    format_mode = request.form.get("format_mode", "auto")
    selected_ids = set(request.form.getlist("book_id"))
    all_books = request.form.get("all_books") == "1"
    download_duplicates = request.form.get("download_duplicates") == "1"
    if kind not in {"author","series"} or not re.fullmatch(r"\d+", catalog_id): flash("Некорректный каталог"); return redirect(url_for("index"))
    if format_mode not in {"auto","epub","fb2"}: format_mode = "auto"
    if not all_books and not selected_ids: flash("Не выбрана ни одна книга"); return redirect(request.referrer or url_for("index"))
    try:
        origin_page = max(0, min(int(request.form.get("origin_page", "0")), MAX_CATALOG_PAGES - 1))
    except ValueError:
        origin_page = 0
    origin_view = "all" if request.form.get("origin_view") == "all" else ""
    with jobs_lock:
        for jid, job in jobs.items():
            if job.get("status") in {"pending","running"} and job.get("origin_kind") == kind and str(job.get("origin_id")) == catalog_id:
                flash("Для этого автора/серии уже выполняется массовая загрузка")
                return redirect(url_for("job_page", job_id=jid))
    try: result = get_cached_catalog(kind, catalog_id)
    except Exception as exc: flash(f"Не удалось получить каталог: {exc}"); return redirect(url_for("index"))
    if all_books:
        selected_ids = {str(book["id"]) for book in result["books"]}
    selected, duplicates_filtered, selected_count, existing_filtered = select_books_for_job(result["books"], selected_ids, download_duplicates=download_duplicates)
    origin_name = catalog_name.split(":",1)[1].strip() if ":" in catalog_name else catalog_name
    return_url, _ = catalog_return(kind, catalog_id, origin_name, origin_page, origin_view)
    if selected_count == 0:
        flash("Выбранные книги не найдены")
        return redirect(return_url)
    if not selected:
        flash("Все выбранные книги уже есть в библиотеке — в очередь ничего не добавлено")
        return redirect(return_url)
    jid = create_job(catalog_name, selected, format_mode, kind, catalog_id, origin_name, duplicates_filtered=duplicates_filtered, download_duplicates=download_duplicates, selected_count=selected_count, existing_filtered=existing_filtered, origin_page=origin_page, origin_view=origin_view)
    response = redirect(url_for("job_page", job_id=jid))
    if not all_books:
        mark_catalog_selection_clear(kind, catalog_id)
    return response


@app.route("/job/<job_id>")
def job_page(job_id):
    """Показывает прогресс и ошибки массового задания."""
    job = job_snapshot(job_id)
    if not job:
        return render_error_page(
            "Задание не найдено",
            "Задание не найдено.",
            404,
        )
    return render_template_string(
        JOB_HTML,
        css=COMMON_CSS,
        job=job,
        download_attempts=DOWNLOAD_RETRY_ATTEMPTS,
        connect_timeout=DOWNLOAD_CONNECT_TIMEOUT,
        read_timeout=DOWNLOAD_READ_TIMEOUT,
    )


@app.route("/api/job/<job_id>")
def job_api(job_id):
    """Возвращает JSON-снимок массового задания."""
    job = job_snapshot(job_id)
    return jsonify(job) if job else (jsonify({"error":"not found"}),404)


@app.post("/job/<job_id>/cancel")
def cancel_job(job_id):
    """Запрашивает мягкую отмену задания."""
    job = job_snapshot(job_id)
    if not job: return Response("Задание не найдено", status=404)
    if job["status"] in {"pending","running"}: job_update(job_id, cancel=True, status_text="Остановка после текущей книги...")
    return redirect(url_for("job_page", job_id=job_id))


@app.post("/job/<job_id>/retry")
def retry_job(job_id):
    """Создаёт задание из ранее неудавшихся книг."""
    old = job_snapshot(job_id)
    if not old: return Response("Задание не найдено", status=404)
    failed = old.get("failed_books", [])
    if not failed: flash("Нет ошибок для повторной загрузки"); return redirect(url_for("job_page", job_id=job_id))
    new_id = create_job(old["title"] + " — повтор ошибок", failed, old.get("format_mode","auto"), old.get("origin_kind",""), old.get("origin_id",""), old.get("origin_name",""), duplicates_filtered=0, download_duplicates=old.get("download_duplicates", False), selected_count=len(failed), existing_filtered=0, origin_page=old.get("origin_page", 0), origin_view=old.get("origin_view", ""))
    return redirect(url_for("job_page", job_id=new_id))


@app.route("/jobs")
def jobs_page():
    """Показывает последние массовые загрузки."""
    with jobs_lock: data = [json.loads(json.dumps(j, ensure_ascii=False)) for j in jobs.values()]
    data.sort(key=lambda j: j.get("created_at",0), reverse=True)
    for j in data: j["created_text"] = format_time(j.get("created_at"))
    return render_template_string(JOBS_HTML, css=COMMON_CSS, job_list=data[:50])


load_jobs()
startup_recovery = init_queue_db()
removed_part_files = cleanup_partial_files()
start_queue_scheduler()

if startup_recovery.get("finalize"):
    try:
        finished_at = time.time()
        queue_setting_set("run_finished_at", finished_at)
        queue_run_finalize(startup_recovery.get("run_id"), finished_at)
        queue_create_completion_notification()
    except Exception:
        pass

if startup_recovery.get("resume"):
    try:
        recovered = int(startup_recovery.get("recovered_items") or 0)
        detail = f"Незавершённых книг возвращено в очередь: {recovered} · удалено .part: {removed_part_files}"
        notification_create(
            "warning",
            "Очередь восстановлена после перезапуска",
            detail,
            f"/runs/{startup_recovery.get('run_id')}",
        )
        start_queue_worker(continue_run=True, trigger="recovery")
    except Exception:
        pass

if __name__ == "__main__":
    os.makedirs(DESTINATION, exist_ok=True)

    webview.create_window(
        title=f"OPDS Desk {APP_VERSION}",
        url=app,
        js_api=desktop_api,
        width=1280,
        height=850,
        min_size=(900, 650),
        resizable=True,
    )

    webview.start(
        debug=False,
        private_mode=False,
    )
