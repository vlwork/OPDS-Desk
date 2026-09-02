# Third-Party Notices

OPDS Desk includes third-party software. This inventory is based on a fresh
Windows x64 PyInstaller onefile build of OPDS Desk 1.0.0 and inspection of the
resulting executable, PyInstaller analysis files, installed package metadata,
and upstream license material. It describes the audited final 1.0.0 build;
future releases must repeat the inventory because unpinned transitive and build
dependencies can change.

Exact license and notice texts prepared for the binary release are in
[`THIRD_PARTY_LICENSES/`](THIRD_PARTY_LICENSES/). The human-readable software
bill of materials is in [`docs/SBOM.md`](docs/SBOM.md).

## Runtime components

| Component | Audited version | License | Confidence and notice source |
| --- | --- | --- | --- |
| CPython | 3.13.15 | PSF License Version 2 and component notices | HIGH — installed `LICENSE.txt`; includes the Windows binary-build terms and notices for bundled bzip2 and libffi. |
| Flask | 3.1.3 | BSD-3-Clause | HIGH — package license file. |
| blinker | 1.9.0 | MIT | HIGH — package license file. |
| click | 8.4.2 | BSD-3-Clause | HIGH — package license file. |
| colorama | 0.4.6 | BSD-3-Clause | HIGH — package license file. |
| itsdangerous | 2.2.0 | BSD-3-Clause | HIGH — package license file. |
| Jinja2 | 3.1.6 | BSD-3-Clause | HIGH — package license file. |
| MarkupSafe | 3.0.3 | BSD-3-Clause | HIGH — package license file. |
| Werkzeug | 3.1.8 | BSD-3-Clause | HIGH — package license file. |
| requests | 2.34.2 | Apache-2.0 | HIGH — package `LICENSE` and `NOTICE`. |
| certifi and `cacert.pem` | 2026.7.22 | MPL-2.0 | HIGH — package notice identifies the CA bundle as a modified extraction of Mozilla certificate data. The CA certificates are third-party trust data, not OPDS Desk code. |
| charset-normalizer | 3.4.9 | MIT | HIGH — package license file. |
| idna | 3.18 | BSD-3-Clause | HIGH — package license file. |
| urllib3 | 2.7.0 | MIT | HIGH — package license file. |
| pywebview | 6.2.1 | BSD-3-Clause | HIGH — package license file. Its own interop DLLs, Android JAR, JavaScript, and Python code are covered by the pywebview project license; Microsoft WebView2 files are listed separately below. |
| bottle | 0.13.4 | MIT | HIGH — package license file. |
| proxy_tools | 0.1.0 | BSD license | HIGH — the release metadata says MIT, but the source header says BSD and the upstream repository contains a BSD `LICENSE.txt`. The exact upstream BSD text is supplied. |
| typing_extensions | 4.16.0 | PSF-2.0 | HIGH — package license file. |
| pythonnet | 3.1.0 | MIT | HIGH for Python.Runtime and pythonnet code — package license file. Microsoft compatibility assemblies copied into the Windows wheel have separate terms below. |
| clr_loader | 0.3.1 | MIT | HIGH — package license file. |
| cffi | 2.1.1 | MIT-0 | HIGH — package license file. |
| pycparser | 3.0 | BSD-3-Clause | HIGH — package license file. |
| setuptools | 84.0.0 | MIT | HIGH — package license file. Although normally build tooling, setuptools modules are present in the executable because of PyInstaller metadata hooks. |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause | HIGH — package license files. Top-level packaging modules are present in the executable. |
| setuptools vendored libraries | versions listed in `docs/SBOM.md` | Permissive licenses listed in the bundled license text | HIGH — installed vendored metadata and license files; only vendored modules found in the executable are listed. |
| OpenSSL | 3.0.21 | Apache-2.0 | HIGH — DLL version metadata and the OpenSSL 3.x upstream license policy. |
| SQLite | 3.50.4 | Public domain | HIGH — DLL version metadata and the SQLite project copyright statement. |

The permissive Python-package licenses above are compatible with distribution
of OPDS Desk under GPL-3.0-only when their attribution and license conditions
are preserved. The certifi-covered files remain available under MPL-2.0; the
unmodified source form is available from the certifi project and Mozilla's NSS
certificate-data source identified in the included certifi notice.

## Build tooling

| Component | Audited version | License | Distribution role |
| --- | --- | --- | --- |
| PyInstaller | 6.21.0 | GPL-2.0-or-later with the Bootloader Exception; runtime hooks under Apache-2.0 | Its bootloader and four core runtime hooks are embedded. The exception permits distribution of the combined executable subject to the application and dependency licenses. |
| pyinstaller-hooks-contrib | 2026.6 | GPL-2.0-or-later for standard hooks; Apache-2.0 for runtime hooks | Standard analysis hooks were used but are not themselves runtime modules in this artifact. |
| altgraph | 0.17.5 | MIT | Build-time dependency analysis only. |
| pefile | 2024.8.26 | MIT | Build-time PE analysis only. |
| pywin32-ctypes | 0.2.3 | BSD-3-Clause | Build-time Windows support only. |

The exact build environment also contains setuptools and packaging. They are
listed under runtime components because modules from both were found in the
executable, not merely used during the build.

## System/external components

| Component | Role | Bundled? | Distribution status |
| --- | --- | --- | --- |
| Windows APIs and WinForms | Desktop shell and operating-system services | No | SYSTEM PROVIDED. |
| Microsoft .NET Framework | Default pythonnet runtime on Windows | No | REQUIRED by the selected WinForms integration; the source does not force a separately bundled CoreCLR. |
| Microsoft Edge WebView2 Evergreen Runtime | Chromium renderer when installed and sufficiently recent | No | CONDITIONALLY USED. pywebview falls back to the Windows MSHTML backend when its runtime checks fail. |
| Windows MSHTML engine | Legacy fallback renderer | No | CONDITIONALLY USED and system provided. |
| Fixed Version WebView2 Runtime | Alternative self-contained Chromium runtime | No | NOT INCLUDED. |
| Visual C++ runtime DLLs | CPython/native runtime support | Yes | BUNDLED; subject to the Microsoft terms referenced by CPython's Windows binary license. |
| Windows Universal C Runtime/API-set components | Native runtime support | No separate copies in the final audited build | SYSTEM PROVIDED. |

## Microsoft WebView2

The executable contains these files from `Microsoft.Web.WebView2` 1.0.3856.49:

- `Microsoft.Web.WebView2.Core.dll`;
- `Microsoft.Web.WebView2.WinForms.dll`;
- `WebView2Loader.dll` for x64, x86, and ARM64.

Hashes of all five files match the corresponding entries in the official NuGet
package. The NuGet package contains `LICENSE.txt` and `NOTICE.txt`; exact copies
are supplied as `Microsoft-WebView2.txt` and
`Microsoft-WebView2-NOTICE.txt`. The package license expressly permits source
and binary redistribution subject to its conditions. The NOTICE contains the
third-party Antlr3.Runtime and StringTemplate4 attributions and is retained.

These files are the WebView2 SDK/.NET wrapper and native loaders. They are not a
Fixed Version WebView2 Runtime. No Edge/Chromium runtime executable or Fixed
Version runtime directory was found in the artifact. The application therefore
uses an installed Evergreen Runtime when pywebview detects one, or its MSHTML
fallback otherwise.

## Distribution notes

The final OPDS Desk 1.0.0 executable was built in a controlled environment with
the unrelated Microsoft JDK directory excluded from `PATH`. Its PyInstaller TOC
was checked for `jdk-`, `Java`, `ucrtbase.dll`, and `api-ms-win-`; no matches
were found.

The pythonnet Windows wheel contributes 96 Microsoft .NET Framework
compatibility assemblies. The Microsoft .NET Library terms are included with
the binary distribution. The 96 compatibility assemblies were independently
matched byte-for-byte to `Microsoft.NET.Build.Extensions/net461`.

No incompatible open-source license was found. See
[`docs/DISTRIBUTION.md`](docs/DISTRIBUTION.md) for the release layout and
verification procedure.
