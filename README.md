# OPDS Desk

[Русский README](README_RU.md)

OPDS Desk is a Windows desktop client for browsing, searching, and downloading books from OPDS 1.x catalogs into a local library.

Current application version: **1.0.0**

## Overview

OPDS Desk is a local desktop application with a Flask backend and a pywebview desktop interface. The user configures one OPDS source and chooses a local library directory where downloaded books are stored.

No separate application server is required. Network access is required to connect to the configured OPDS source and to retrieve books from acquisition URLs supplied by that source.

## Compatibility

OPDS Desk is a general-purpose client for OPDS 1.x catalogs.

It is designed to work with standards-compatible OPDS services and has also
been tested with Flibusta-style OPDS catalog structures.

OPDS Desk is an independent project and is not affiliated with, endorsed by,
or operated by Flibusta or any other catalog provider. Users are responsible
for ensuring that their use of configured OPDS sources complies with applicable
laws and the terms of those services.

## Features

- First-run setup and local library folder selection.
- Manual configuration and validation of an OPDS source.
- Browsing of OPDS 1.x catalogs based on Atom feeds.
- Pagination through Atom navigation links supplied by the source.
- OpenSearch-based search when a supported descriptor is available.
- Navigation to related catalogs and catalog sections.
- Book selection from OPDS search results.
- Persistent SQLite-backed download queue.
- Manual queue start, pause, resume, priority, retry, and removal controls.
- Configurable queue scheduler and minimum free disk-space threshold.
- Per-book download progress and retry handling for transient failures.
- Queue history, run details, job views, and notifications.
- Detection of books that already exist in the local library.
- Duplicate-edition grouping and preferred-edition selection.
- EPUB and FB2 downloads, including FB2 packaged inside ZIP archives.
- File-format validation before a completed download is published.

Browsing OPDS catalogs is currently read-only. Books are primarily added to the download queue from OPDS search results.

## Supported catalogs and formats

Catalog support:

- OPDS 1.x based on Atom feeds.
- OpenSearch Description 1.1 when supplied by the configured source.

Download support:

- EPUB.
- FB2 XML.
- FB2 packaged inside a ZIP archive.

Catalogs not currently supported:

- OPDS 2 JSON catalogs.

Book formats not currently supported for downloading:

- PDF.
- DJVU.
- MOBI.

The download list describes formats handled by the current downloader. An OPDS feed may still contain metadata or links for other formats.

## Requirements

For the packaged application:

- Windows.
- Network access to the configured OPDS source and its acquisition URLs.
- Write access to the selected library directory.

For development from source:

- Python 3.
- `pip`.
- `venv`.

The current release is developed and tested with Python 3.13. The repository does not currently define an official minimum Python version or a list of supported Windows versions. The desktop interface is provided by pywebview.

## Installation from source

Clone the repository, create a virtual environment, install dependencies, and run the application:

```powershell
git clone <repository-url>
cd <repository-directory>
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r .\requirements.txt
python .\app.py
```

Alternatively, run it without activating the virtual environment:

```powershell
.\.venv\Scripts\python.exe .\app.py
```

## First run

1. Launch OPDS Desk.
2. Choose an existing local library directory with write access.
3. Complete the initial setup.
4. Open the OPDS settings screen.
5. Enter an absolute HTTP or HTTPS OPDS URL.
6. Wait for the application to validate the source before saving it.
7. Return to the home screen and use search or catalog browsing.

The application does not provide a default OPDS source. The source is entered manually by the user.

## Using the application

### Configure an OPDS source

Open OPDS settings, enter an absolute HTTP(S) URL, and submit the form. OPDS Desk normalizes the URL, retrieves the root feed, and verifies that it is a supported Atom feed before saving the source URL and feed title.

Only one source is configured at a time. The current source can be cleared from the same settings screen.

### Search

Enter a query on the home screen. Search is available only when the configured source exposes a supported OpenSearch or direct Atom search descriptor. If the source does not provide one, catalog browsing can still be used but search is unavailable.

Search results can span multiple pages. Selection is retained while moving between result pages in the same desktop session.

### Browse catalogs

Open the configured catalog from the home screen to browse its publications, navigation sections, related catalogs, and pagination links. A full-catalog view can follow the source's page chain within the application's page limit.

Catalog pages are currently read-only and do not offer direct queue actions.

### Add books to the queue

Select books in OPDS search results and choose a format mode:

- **Auto** prefers EPUB and then FB2 when available.
- **EPUB** accepts only results with an EPUB acquisition link.
- **FB2** accepts only results with a supported FB2 acquisition link.

The submitted selection is resolved against book data retained by the local backend. Books already present in the library, already active in the queue, or without a supported format are skipped.

### Run downloads

Open the queue to start downloads manually, pause after the current item, resume processing, change priorities, or remove pending items. Automatic runs can be enabled for a configured local time and UTC offset.

The queue checks the configured minimum free disk space before starting work. Only one queue worker processes downloads at a time.

### Monitor queue and history

The queue screen reports current item progress and aggregate state. Additional screens provide run summaries, completed-item history, retry actions for failed items, persisted job state, and application notifications.

## Local data

For a fresh installation, application data is stored under:

```text
%LOCALAPPDATA%\OPDSDesk
```

For example:

```text
C:\Users\<User>\AppData\Local\OPDSDesk
```

The directory can contain:

- `config.json` — application, source, setup, and library configuration.
- `jobs.json` — persisted job state.
- `queue.db` — queue items, scheduler settings, notifications, and run history.
- `Library\` — the default local library location if selected and used.

SQLite may also create `queue.db-wal` and `queue.db-shm` while the application is running. The user may select another library directory, in which case book files are written there instead of the default `Library\` directory.

## Network behavior

- OPDS sources may use HTTP or HTTPS.
- Credentials embedded in source URLs are rejected.
- HTTP redirects are supported.
- Standard requests TLS certificate verification is used.
- Normal system proxy and environment behavior used by requests is preserved.
- Private and local-network OPDS sources are allowed.

Use HTTPS for sources accessed over untrusted networks.

## Download validation

- Downloads are written to temporary files.
- Incomplete downloads are not published as final library files.
- EPUB ZIP structure, metadata, and package references are validated.
- Raw FB2 XML is parsed and its root element is checked.
- FB2 ZIP archives are inspected and their extracted FB2 content is validated.
- Download and extracted-file size limits are enforced.
- Completed files are atomically published to their final destination.

## Building Windows executables

PyInstaller is a build dependency and is not included in `requirements.txt`. Install PyInstaller separately in the development environment before building:

```powershell
python -m pip install PyInstaller
```

Build the directory-based application (output: `dist\OPDS-Desk\OPDS-Desk.exe`):

```powershell
.\.venv\Scripts\python.exe -m PyInstaller --clean --noconfirm .\OPDS-Desk.spec
```

Build the one-file application (output: `dist\OPDS-Desk.exe`):

```powershell
.\.venv\Scripts\python.exe -m PyInstaller --clean --noconfirm .\OPDS-Desk-OneFile.spec
```

## Testing

Compile-check the main module and run the full unit test suite:

```powershell
.\.venv\Scripts\python.exe -m py_compile .\app.py
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

The tests cover OPDS parsing, URL handling, OpenSearch and search behavior, catalog navigation, queue identity and worker behavior, downloads and file validation, persistence, and packaging metadata.

## Security

OPDS Desk is designed as a local desktop application. Do not expose its internal Flask service directly as a public network service.

See the [Security Policy](SECURITY.md) for the current security boundaries and vulnerability-reporting guidance.

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Configuration](docs/CONFIGURATION.md)
- [Development](docs/DEVELOPMENT.md)
- [Release process](docs/RELEASE.md)
- [Security Policy](SECURITY.md)
- [Changelog](CHANGELOG.md)
- [Contributing](CONTRIBUTING.md)
- [Third-party notices](THIRD_PARTY_NOTICES.md)
- [Software Bill of Materials](docs/SBOM.md)
- [Binary distribution](docs/DISTRIBUTION.md)

## License

OPDS Desk is licensed under the [GNU General Public License v3.0 only](LICENSE) (`GPL-3.0-only`).

Copyright © 2026 Researcher Universe Labs.
