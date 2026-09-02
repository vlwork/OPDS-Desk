# OPDS Desk Software Bill of Materials

This human-readable SBOM records a fresh Windows x64 PyInstaller onefile build
inspected during release preparation. It is an artifact-specific snapshot, not
a lock file. Transitive and build-tool versions are not fully pinned by the
repository and must be re-inventoried for every final build.

## Application

- Component: OPDS Desk
- Version: 1.0.0
- License: GPL-3.0-only
- Copyright: Copyright © 2026 Researcher Universe Labs
- Artifact type: Windows x64 PyInstaller onefile executable
- Python: 3.13.15
- PyInstaller: 6.21.0

## Runtime Python packages

`Bundled` means that package modules or package data were found in the
PyInstaller analysis/archive, not merely installed in the virtual environment.

| Component | Version | License | Distribution role | Bundled? | Source / confidence |
| --- | --- | --- | --- | --- | --- |
| Flask | 3.1.3 | BSD-3-Clause | Local web application | Yes | [Pallets Flask](https://pypi.org/project/Flask/3.1.3/), package license; HIGH |
| blinker | 1.9.0 | MIT | Flask signals | Yes | [PyPI](https://pypi.org/project/blinker/1.9.0/), package license; HIGH |
| click | 8.4.2 | BSD-3-Clause | Flask command support | Yes | [PyPI](https://pypi.org/project/click/8.4.2/), package license; HIGH |
| colorama | 0.4.6 | BSD-3-Clause | Windows console compatibility | Yes | [PyPI](https://pypi.org/project/colorama/0.4.6/), package license; HIGH |
| itsdangerous | 2.2.0 | BSD-3-Clause | Flask signing support | Yes | [PyPI](https://pypi.org/project/itsdangerous/2.2.0/), package license; HIGH |
| Jinja2 | 3.1.6 | BSD-3-Clause | HTML templates | Yes | [PyPI](https://pypi.org/project/Jinja2/3.1.6/), package license; HIGH |
| MarkupSafe | 3.0.3 | BSD-3-Clause | Template escaping and native speedup | Yes | [PyPI](https://pypi.org/project/MarkupSafe/3.0.3/), package license; HIGH |
| Werkzeug | 3.1.8 | BSD-3-Clause | HTTP/WSGI support | Yes | [PyPI](https://pypi.org/project/Werkzeug/3.1.8/), package license; HIGH |
| requests | 2.34.2 | Apache-2.0 | HTTP transport | Yes | [PyPI](https://pypi.org/project/requests/2.34.2/), package license and NOTICE; HIGH |
| certifi | 2026.7.22 | MPL-2.0 | CA certificate bundle | Yes, including `cacert.pem` | [PyPI](https://pypi.org/project/certifi/2026.7.22/), package notice; HIGH |
| charset-normalizer | 3.4.9 | MIT | Response character encoding | Yes, including native modules | [PyPI](https://pypi.org/project/charset-normalizer/3.4.9/), package license; HIGH |
| idna | 3.18 | BSD-3-Clause | Internationalized domain handling | Yes | [PyPI](https://pypi.org/project/idna/3.18/), package license; HIGH |
| urllib3 | 2.7.0 | MIT | HTTP connection layer | Yes | [PyPI](https://pypi.org/project/urllib3/2.7.0/), package license; HIGH |
| pywebview | 6.2.1 | BSD-3-Clause | Desktop shell and bundled resources | Yes | [pywebview 6.2.1](https://github.com/r0x0r/pywebview/tree/6.2.1), package license; HIGH |
| bottle | 0.13.4 | MIT | pywebview HTTP support | Yes | [PyPI](https://pypi.org/project/bottle/0.13.4/), package license; HIGH |
| proxy_tools | 0.1.0 | BSD-2-Clause-style upstream text | pywebview proxy utility | Yes | [Upstream LICENSE](https://github.com/jtushman/proxy_tools/blob/master/LICENSE.txt); HIGH. Package metadata says MIT, but the source header and upstream license say BSD. |
| typing_extensions | 4.16.0 | PSF-2.0 | Typing compatibility | Yes | [PyPI](https://pypi.org/project/typing-extensions/4.16.0/), package license; HIGH |
| pythonnet | 3.1.0 | MIT for pythonnet code | .NET integration | Yes | [pythonnet](https://pypi.org/project/pythonnet/3.1.0/), package license; HIGH for pythonnet code |
| clr_loader | 0.3.1 | MIT | .NET runtime loader | Yes | [PyPI](https://pypi.org/project/clr-loader/0.3.1/), package license; HIGH |
| cffi | 2.1.1 | MIT-0 | Native interface for clr_loader | Yes, including `_cffi_backend` | [PyPI](https://pypi.org/project/cffi/2.1.1/), package license; HIGH |
| pycparser | 3.0 | BSD-3-Clause | Parser used by CFFI | Yes | [PyPI](https://pypi.org/project/pycparser/3.0/), package license; HIGH |
| setuptools | 84.0.0 | MIT | Runtime metadata support collected by PyInstaller | Yes | [PyPI](https://pypi.org/project/setuptools/84.0.0/), package license; HIGH |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause | Runtime version/requirement handling | Yes | [PyPI](https://pypi.org/project/packaging/26.3/), package license files; HIGH |

### Bundled setuptools-vendored modules

These modules are namespaced inside setuptools. Installed vendored packages
that were not found in the archive are excluded.

| Component | Version | License | Distribution role | Bundled? | Source / confidence |
| --- | --- | --- | --- | --- | --- |
| backports.tarfile | 1.2.0 | MIT | setuptools internal | Yes | Vendored metadata/license; HIGH |
| importlib_metadata | 8.7.1 | Apache-2.0 | setuptools internal metadata | Yes, including dist-info/license | Vendored metadata/license; HIGH |
| jaraco.context | 6.1.0 | MIT | setuptools internal | Yes | Vendored metadata/license; HIGH |
| jaraco.functools | 4.4.0 | MIT | setuptools internal | Yes | Vendored metadata/license; HIGH |
| jaraco.text | 4.0.0 | MIT | setuptools internal | Yes | Vendored metadata/license; HIGH |
| more-itertools | 10.8.0 | MIT | setuptools internal | Yes | Vendored metadata/license; HIGH |
| packaging | 26.0 | Apache-2.0 OR BSD-2-Clause | setuptools internal | Yes | Vendored metadata/license; HIGH |
| tomli | 2.4.0 | MIT | setuptools internal | Yes | Vendored metadata/license; HIGH |
| wheel | 0.46.3 | MIT | setuptools internal | Yes | Vendored metadata/license; HIGH |
| zipp | 3.23.0 | MIT | setuptools internal | Yes | Vendored metadata/license; HIGH |

## Native/runtime components

| Component | Version | License / terms | Files and role | Bundled? | Source / confidence |
| --- | --- | --- | --- | --- | --- |
| CPython runtime | 3.13.15 | PSF License Version 2 plus bundled notices | `python313.dll`, `base_library.zip`, and 18 standard extension `.pyd` files | Yes | Installed Python version metadata and license; HIGH |
| OpenSSL | 3.0.21 | Apache-2.0 | `libcrypto-3.dll`, `libssl-3.dll` | Yes | DLL version metadata and [OpenSSL license policy](https://openssl-library.org/source/license/); HIGH |
| SQLite | 3.50.4 | Public domain | `sqlite3.dll`, `_sqlite3.pyd` | Yes | DLL version metadata and [SQLite copyright statement](https://sqlite.org/copyright.html); HIGH |
| libffi | library ABI 8; exact source version not exposed by the DLL | MIT-style libffi license | `libffi-8.dll`, used by `_ctypes` | Yes | CPython Windows license notice; MEDIUM for exact version, HIGH for license |
| bzip2/libbzip2 | 1.0.8 per CPython notice | bzip2 license | `_bz2.pyd`; no separate bzip2 DLL | Yes | CPython Windows license notice; HIGH |
| liblzma | exact source version not determined | 0BSD for current releases, public domain for older releases; exact embedded version requires source-level confirmation | `_lzma.pyd`; no separate liblzma DLL | Yes | File presence and [XZ Utils licensing](https://tukaani.org/xz/); LOW for exact source version |
| Microsoft WebView2 SDK | 1.0.3856.49 | Microsoft package BSD-style license plus NOTICE | Core and WinForms assemblies; x64, x86, and ARM64 loaders | Yes | Hash match with official NuGet package, license and NOTICE; HIGH |
| WebBrowserInterop | 0.0.0.0 file version | pywebview BSD-3-Clause | x64 and x86 DLLs for MSHTML fallback | Yes | pywebview 6.2.1 source project and package license; HIGH |
| pywebview Android interop | pywebview 6.2.1 | pywebview BSD-3-Clause | `pywebview-android.jar`; not used on Windows | Yes | JAR classes match the pywebview Android source tree; HIGH |
| Python.Runtime | 3.1.0 | pythonnet MIT | `Python.Runtime.dll`, XML and dependency metadata | Yes | pythonnet package license; HIGH |
| .NET Framework compatibility assemblies | build versions 4.6.25714.01 / 4.6.26011.1 | Microsoft .NET Library terms | 96 `System.*`, `Microsoft.Win32.Primitives`, and `netstandard` facade assemblies | Yes | Byte-for-byte match with `Microsoft.NET.Build.Extensions/net461`; HIGH provenance; applicable terms are included with the distribution |
| ClrLoader | 1.0.0.0 DLL version | MIT | x86 and x64 `ClrLoader.dll` | Yes | clr_loader package license; HIGH |
| Microsoft Visual C++ runtime | 14.51.36247.0 | Microsoft Distributable Code terms referenced by CPython | `VCRUNTIME140.dll`, `VCRUNTIME140_1.dll` | Yes | File metadata and CPython Windows license; HIGH provenance |
| Windows Universal C Runtime/API sets | Windows-supplied | Microsoft system-component terms | Native runtime support; no standalone `ucrtbase.dll` or `api-ms-win-` DLLs were found in the final PyInstaller TOC | No | Controlled final-build TOC check; HIGH |
| MarkupSafe native speedup | 3.0.3 | BSD-3-Clause | `_speedups` `.pyd` | Yes | Package metadata/license; HIGH |
| charset-normalizer native modules | 3.4.9 | MIT | three `.pyd` files | Yes | Package metadata/license; HIGH |
| CFFI backend | 2.1.1 | MIT-0 | `_cffi_backend` `.pyd` | Yes | Package metadata/license; HIGH |

No Tcl/Tk files, Qt, CEF, or Fixed Version WebView2 Runtime were found.

## System dependencies

| Component | Classification | Reason |
| --- | --- | --- |
| Windows x64 and Windows APIs | REQUIRED / SYSTEM PROVIDED | The inspected executable and CPython runtime are x64 Windows binaries. |
| Microsoft .NET Framework | REQUIRED / SYSTEM PROVIDED | pythonnet selects `netfx` by default on Windows and pywebview uses WinForms. |
| .NET Framework 4.6.2 or newer | REQUIRED FOR WEBVIEW2 PATH | pywebview's static backend-selection code rejects the Chromium path below 4.6.2. The minimum for the MSHTML fallback was not separately validated. |
| Microsoft Edge WebView2 Evergreen Runtime | CONDITIONALLY USED / SYSTEM PROVIDED | Selected dynamically when pywebview detects a supported installed runtime. |
| Windows MSHTML | CONDITIONALLY USED / SYSTEM PROVIDED | Fallback renderer when Chromium selection fails. |
| Fixed Version WebView2 Runtime | NOT INCLUDED | No Fixed Version runtime tree or Edge/Chromium runtime executable is present. |

## Build toolchain

| Component | Version | License | Included in final artifact? | Source / confidence |
| --- | --- | --- | --- | --- |
| Python | 3.13.15 | PSF License Version 2 | Runtime is included | Installed interpreter; HIGH |
| PyInstaller | 6.21.0 | GPL-2.0-or-later with Bootloader Exception; runtime hooks Apache-2.0 | Bootloader and four core runtime hooks included | Package `COPYING.txt`; HIGH |
| pyinstaller-hooks-contrib | 2026.6 | GPL-2.0-or-later for standard hooks; runtime hooks Apache-2.0 | Standard hooks used during analysis; no contrib runtime hook found | Package license and Analysis TOC; HIGH |
| altgraph | 0.17.5 | MIT | No runtime package modules identified | Package metadata; HIGH |
| pefile | 2024.8.26 | MIT | No runtime package modules identified | Package metadata; HIGH |
| pywin32-ctypes | 0.2.3 | BSD-3-Clause | No runtime package modules identified | Package metadata; HIGH |

The build used standard PyInstaller hooks for the Python runtime, Flask stack,
SQLite and other modules; the pywebview package hook copied `webview/lib` and
`webview/js`; pythonnet/clr_loader hooks copied managed and native runtime
files; certifi and charset-normalizer hooks copied their data/native files.

## Notes / limitations

- Static archive inspection establishes presence, not whether every collected
  file executes on a particular machine. The x86/ARM64 WebView2 loaders and the
  Android JAR are present but not used by the audited x64 Windows path.
- Windows renderer choice is dynamic. The source does not force
  `edgechromium`; it chooses WinForms/WebView2 when its checks pass and MSHTML
  otherwise.
- The final artifact was built with the unrelated Microsoft JDK directory
  excluded from `PATH`. Its PyInstaller TOC contained no `jdk-`, `Java`,
  `ucrtbase.dll`, or `api-ms-win-` matches.
- A machine-readable SPDX/CycloneDX file was not created. No trusted generator
  is installed, and manually inventing package identifiers and Microsoft
  subcomponent relationships would reduce rather than improve accuracy.
- Overall confidence is HIGH for Python package versions, WebView2 provenance,
  and file presence; MEDIUM for the complete classification of Microsoft
  compatibility files; LOW only for the precise liblzma source version embedded
  in the CPython extension.
