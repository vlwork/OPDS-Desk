# Security Policy

This document describes the current security boundaries of OPDS Desk. It is intended for maintainers and users evaluating the local Windows desktop deployment model. Related technical details are available in [Architecture](docs/ARCHITECTURE.md), [Configuration](docs/CONFIGURATION.md), [Development](docs/DEVELOPMENT.md), and the [Release process](docs/RELEASE.md).

## Supported Versions

The current development branch targets OPDS Desk 1.0.0. No separate long-term support policy for older versions is currently defined.

## Reporting a Vulnerability

A dedicated private vulnerability-reporting channel has not yet been published for this repository.

After the repository is made public, GitHub Private Vulnerability Reporting is the preferred channel once maintainers have explicitly enabled it. Until the repository displays a private reporting option or another private contact, do not disclose sensitive findings through a public issue.

## Intended Deployment Model

OPDS Desk is a local Windows desktop application. pywebview provides the desktop shell, while Flask provides the local backend and UI layer. The Flask backend is intended for use inside the desktop application and is not intended to be exposed directly as a public web service.

The application is not designed as a hosted multi-user service. This deployment model does not by itself guarantee network isolation; outbound network access is part of normal OPDS catalog and book download operation.

## Local Flask Surface

The current Flask routes do not implement application-level user authentication. The repository does not include a CSRF framework, and form routes do not receive framework-provided CSRF protection.

Flask session state is signed with the configured Flask secret key. A signed session cookie provides integrity checking, but its contents should not be treated as encrypted storage. The internal Flask surface should remain within the local desktop deployment boundary.

## Flask Secret Configuration

`OPDS_DESK_SECRET` can be supplied through the process environment. Its value should be treated as private. If the variable is present, the current code uses its exact value; an empty or whitespace-only value is still considered present and is not normalized.

If `OPDS_DESK_SECRET` is not supplied, the current implementation uses a compatibility/default fallback. The fallback value is intentionally not reproduced here. For controlled deployments, set `OPDS_DESK_SECRET` to a non-empty private value. This setting does not address other application or operating-system boundaries.

## OPDS Source URLs

Configured sources use absolute HTTP or HTTPS URLs. URL normalization requires a host, rejects embedded username/password credentials, and removes fragments. Relative links discovered in OPDS metadata are resolved against their containing page before validation.

Redirects are supported. The final redirect URL is normalized and validated before metadata or acquisition content is accepted. Localhost and private-network addresses are intentionally allowed.

Because HTTP is supported, transport confidentiality depends on the selected source and network. Prefer HTTPS when using an untrusted network.

## Redirect Behavior

Redirects are permitted and are not restricted to the original source origin. A metadata request or acquisition download can therefore finish at another HTTP or HTTPS origin advertised through redirect behavior.

This is a current interoperability boundary rather than an assertion about the trustworthiness of a destination. Source selection and redirect targets should be considered when configuring an unfamiliar catalog.

## TLS and Proxy Behavior

HTTPS requests use the standard certificate verification behavior of `requests`. The application does not set `verify=False` and does not provide a custom certificate store.

The standard `requests` environment and system proxy behavior is preserved. Proxy and CA-related environment configuration can therefore influence outbound connections made by the process.

## Local Network Access

Localhost and private-network OPDS sources are intentionally allowed so that OPDS Desk can work with user-managed local servers. Source configuration can therefore direct requests to network destinations reachable by the current Windows user session.

The application does not restrict source hosts to public Internet addresses or to a predefined allowlist.

## Download Validation and Limits

OPDS metadata responses are read with a 10 MiB limit. Acquisition downloads are streamed with a 100 MiB limit, and FB2 content extracted from ZIP is subject to the same unpacked-size limit.

Downloads use temporary `.part` files. EPUB validation checks the ZIP structure and CRC, `mimetype`, `META-INF/container.xml`, the referenced package document, and XML structure. FB2 validation parses XML and requires a `FictionBook` root. FB2 inside ZIP is extracted only after checking the selected member and its unpacked size.

The final book is published to its destination only after the applicable validation succeeds, using `os.replace()` for the final filesystem operation. OPDS Desk does not perform general malware scanning.

## Supported Download Formats

The downloader supports:

- EPUB;
- FB2 XML;
- FB2 inside ZIP.

The downloader does not support PDF, DJVU, or MOBI.

## Filesystem Access

Application state is written under the resolved `APP_DATA_DIR`. Books are written under the configured `library_path`. When a library directory is selected, the application creates and removes a temporary write-test file to confirm write access.

Temporary `.part` files may exist while a download is in progress. Startup cleanup recursively removes files ending in `.part` under the current library destination. The application does not implement a separate filesystem sandbox; writes run with the permissions of the current Windows user.

## Local Data Protection

Local application state includes:

- `config.json`;
- `jobs.json`;
- `queue.db`;
- `queue.db-wal` and `queue.db-shm` while SQLite WAL mode is active;
- browser `sessionStorage` and `localStorage`;
- the Flask session cookie.

These data are not encrypted by OPDS Desk. Protection relies on the Windows account and local filesystem controls. The files and browser state may contain source URLs, local paths, job or queue history, identifiers, and other private metadata.

## Diagnostic Endpoints

The local diagnostics surface includes an `/api/health` JSON endpoint. Diagnostic data may expose operational details such as the configured library destination, disk status, queue state, counts, and error details.

Diagnostic endpoints are intended only for local application use and should not be exposed as a public monitoring API.

## Browser and Session Storage

Search selection state is stored in `sessionStorage`. The notification cursor is stored in `localStorage`. Flask navigation context is stored in a signed session cookie.

pywebview starts with `private_mode=False`, so its browser storage follows normal persistent-profile behavior. Browser and session storage are local application state and should not be treated as encrypted secret storage.

## Runtime Trust Boundary

The queue worker and scheduler run as background threads inside the same local application process as the Flask UI, downloader, and filesystem code. There is no privilege separation between these components.

All components run with the permissions of the current Windows user. A component that performs an allowed operation therefore acts within the same user-level filesystem and network boundary as the rest of the process.

## Known Security Boundaries

- The intended deployment is a local desktop application, not a public Flask service.
- HTTP sources are allowed.
- Redirects may change origin.
- Localhost and private-network destinations are allowed.
- The Flask UI has no application-level user authentication.
- No CSRF framework is present.
- Local persisted and browser data are not encrypted by OPDS Desk.
- General malware scanning is not performed.
- The configured source can direct acquisition downloads to URLs it advertises, subject to current URL, redirect, format, validation, and size checks.

## Operational Recommendations

- Use OPDS sources you trust.
- Prefer HTTPS on untrusted networks.
- Do not expose the internal Flask backend publicly.
- Use a non-empty private `OPDS_DESK_SECRET` for controlled deployments.
- Protect the Windows account and application data directory with appropriate OS controls.
- Back up local data before destructive maintenance.
- Review source URLs before configuring an unfamiliar catalog.
- Keep dependencies updated through normal development maintenance and verify changes with the repository test suite.

## Sensitive Information in Reports

Before sharing an issue, log, or screenshot, remove or redact:

- OPDS URLs;
- private hostnames;
- local filesystem paths;
- usernames;
- secrets and tokens;
- the contents of `config.json`;
- queue or job history when it contains private metadata;
- screenshots showing private library contents.
