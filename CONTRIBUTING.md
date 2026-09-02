# Contributing to OPDS Desk

Contributions that improve the current OPDS client, its tests, documentation, packaging, or maintainability are welcome. Keep each change focused and preserve documented compatibility behavior unless the proposal includes an explicit transition plan.

## Development setup

OPDS Desk currently targets Windows. The current development environment uses Python 3.13; no separate minimum supported Python version is declared. Create a virtual environment and install the pinned runtime dependencies:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r .\requirements.txt
```

See [Development](docs/DEVELOPMENT.md) for repository structure, isolated testing guidance, and packaging commands. Runtime configuration and local-data behavior are documented in [Configuration](docs/CONFIGURATION.md).

## Tests

Run the syntax check and relevant unit tests before submitting a change. For a repository-wide change, use:

```powershell
.\.venv\Scripts\python.exe -m py_compile .\app.py
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
git diff --check
```

Use offline fixtures and mocks for network behavior. Do not make tests depend on a live OPDS service.

## Issues

Describe the OPDS Desk and Windows versions, run mode, reproduction steps, expected behavior, and actual behavior. Include only the smallest useful redacted log excerpt.

Never publish passwords, tokens, private OPDS URLs, private filesystem paths, `config.json`, queue databases, or private library contents. Follow [SECURITY.md](SECURITY.md) for vulnerability reports.

## Pull requests

- Explain the scope and user-visible effect.
- Avoid unrelated refactoring.
- Add or update relevant tests.
- Update English and Russian user documentation together when behavior changes.
- Preserve compatibility and persisted-data contracts unless the change includes an explicit transition plan.
- Keep secrets, private data, generated build output, and runtime state out of the repository.

## Contribution license

By submitting a contribution, you agree that your contribution may be distributed under the project's `GPL-3.0-only` license. Do not submit third-party material unless its provenance and license permit distribution with this project.
