# Распространение бинарной версии OPDS Desk

Этот документ описывает подготовку Windows-бинарника OPDS Desk к публичному распространению.

Он дополняет [RELEASE.md](RELEASE.md) и не заменяет процедуру сборки релиза.

Документ основан на анализе Windows x64 PyInstaller onefile-сборки OPDS Desk 1.0.0. Поскольку часть версий транзитивных и build-зависимостей не закреплена в репозитории, состав каждого окончательного релизного артефакта необходимо проверять заново.

## 1. Лицензия приложения

OPDS Desk распространяется на условиях:

- GNU General Public License version 3.0 only;
- SPDX identifier: `GPL-3.0-only`;
- Copyright © 2026 Researcher Universe Labs.

Основной текст лицензии находится в корневом файле [LICENSE](../LICENSE).

## 2. Основной Windows-артефакт

Основной формат публичного Windows-релиза:

```text
OPDS-Desk-<version>.exe
```

Для версии 1.0.0:

```text
OPDS-Desk-1.0.0.exe
```

Текущая схема сборки использует PyInstaller onefile.

Перед публикацией окончательного EXE необходимо заново проверить:

- ProductVersion;
- FileVersion;
- архитектуру;
- размер;
- SHA-256;
- фактический состав PyInstaller bundle;
- third-party компоненты.

Контрольная сумма конкретной сборки в этом документе намеренно не фиксируется.

## 3. Что содержит onefile-сборка

Проверенная Windows x64 onefile-сборка содержала как минимум:

- CPython runtime;
- Python standard library;
- Flask и его runtime-зависимости;
- requests и его runtime-зависимости;
- pywebview;
- pythonnet;
- clr_loader;
- CFFI;
- SQLite;
- OpenSSL;
- libffi;
- certifi CA bundle;
- Microsoft WebView2 SDK/.NET wrapper components;
- WebView2 native loader DLL;
- Microsoft .NET Framework compatibility assemblies;
- Microsoft Visual C++ runtime DLL;
- другие файлы, автоматически собранные PyInstaller hooks.

Подробный список находится в [SBOM.md](SBOM.md).

Third-party notices и соответствующие лицензионные материалы находятся в:

- [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md);
- [`THIRD_PARTY_LICENSES/`](../THIRD_PARTY_LICENSES/).

## 4. Microsoft WebView2

В проверенной сборке присутствовали компоненты из официального NuGet-пакета:

```text
Microsoft.Web.WebView2 1.0.3856.49
```

Подтверждены:

- `Microsoft.Web.WebView2.Core.dll`;
- `Microsoft.Web.WebView2.WinForms.dll`;
- `WebView2Loader.dll` для x64;
- `WebView2Loader.dll` для x86;
- `WebView2Loader.dll` для ARM64.

Проверенные DLL побайтно совпали с соответствующими файлами официального NuGet-пакета Microsoft.

Для пакета сохранены его собственные:

- `LICENSE.txt`;
- `NOTICE.txt`.

В репозитории они представлены как:

```text
THIRD_PARTY_LICENSES/Microsoft-WebView2.txt
THIRD_PARTY_LICENSES/Microsoft-WebView2-NOTICE.txt
```

## 5. WebView2 Runtime

Fixed Version Microsoft Edge WebView2 Runtime в проверенный EXE не включён.

В частности, не найден отдельный bundled Edge/Chromium runtime tree.

Текущая Windows-логика pywebview выбирает backend динамически.

Когда доступны необходимые .NET/WebView2 компоненты и установлен подходящий Microsoft Edge WebView2 Evergreen Runtime, может использоваться WebView2.

Если Chromium/WebView2 backend не проходит runtime-проверки pywebview, используется поддерживаемый Windows fallback на MSHTML.

Следовательно, Microsoft Edge WebView2 Evergreen Runtime является внешним системным runtime-компонентом, а не частью текущего onefile EXE.

## 6. Компоненты pywebview

PyInstaller hook pywebview включает содержимое, которое может быть шире фактически используемого Windows x64 runtime path.

В проверенной сборке присутствовали, в частности:

- x64/x86/ARM64 варианты `WebView2Loader.dll`;
- `WebBrowserInterop.x86.dll`;
- `WebBrowserInterop.x64.dll`;
- `pywebview-android.jar`.

Наличие компонента внутри bundle не означает, что он выполняется на текущей платформе.

Например, Android JAR присутствует в bundle вследствие механизма сбора pywebview resources, но не используется Windows-приложением.

Не следует удалять такие компоненты из spec-файлов только ради уменьшения bundle без отдельного контролируемого изменения и проверки.

## 7. Microsoft .NET Framework

pythonnet и Windows backend pywebview используют .NET/WinForms integration.

В проверенной сборке присутствовали Microsoft .NET Framework compatibility assemblies.

96 таких assemblies побайтно совпали с соответствующими файлами:

```text
Microsoft.NET.Build.Extensions/net461
```

Для Microsoft .NET Library сохранён соответствующий лицензионный текст:

```text
THIRD_PARTY_LICENSES/Microsoft-dotnet-library.txt
```

Для финального Windows-бинарника состав этих assemblies подтверждён: все 96 файлов byte-for-byte совпадают с Microsoft.NET.Build.Extensions/net461. Текст Microsoft .NET Library terms включён в сопроводительные материалы бинарного релиза.

## 8. Visual C++ Runtime

Проверенная сборка содержала:

```text
VCRUNTIME140.dll
VCRUNTIME140_1.dll
```

Они относятся к native runtime, необходимому CPython и другим бинарным компонентам.

Перед финальным релизом их происхождение и version metadata должны быть повторно зафиксированы в artifact-specific inventory.

## 9. UCRT и API-set DLL

Финальная сборка OPDS Desk 1.0.0 выполнена с исключённым из `PATH` каталогом несвязанного Microsoft JDK.

После сборки PyInstaller TOC проверен на:

- `jdk-`;
- `Java`;
- `ucrtbase.dll`;
- `api-ms-win-`.

Совпадений не найдено. Финальный EXE не содержит отдельных UCRT/API-set DLL из этого постороннего toolchain.

## 10. Контролируемая чистая сборка

Для каждого будущего публичного binary release необходимо выполнять сборку в контролируемом Windows environment.

Цель такой проверки:

- исключить влияние посторонних toolchains;
- исключить случайный подбор DLL через PATH;
- повторно получить PyInstaller dependency inventory;
- убедиться, что в bundle нет DLL из несвязанных SDK/JDK;
- повторно проверить Microsoft runtime components.

После каждой такой сборки необходимо отдельно проверить PyInstaller TOC/analysis output.

## 11. Third-party licenses

В бинарный релиз должны сопровождаться применимые third-party license и notice materials.

Индекс компонентов:

[THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)

Подготовленные тексты:

[`THIRD_PARTY_LICENSES/`](../THIRD_PARTY_LICENSES/)

Текущий каталог включает материалы для:

- Apache-2.0 components;
- MPL-2.0 components;
- Microsoft WebView2;
- Microsoft .NET Library;
- PyInstaller;
- Python;
- bundled Python packages;
- requests NOTICE;
- setuptools и packaging.

Этот набор относится к проверенной сборке.

Если состав финальной сборки изменится, набор third-party материалов также необходимо пересмотреть.

## 12. SBOM

Human-readable Software Bill of Materials:

[SBOM.md](SBOM.md)

SBOM является snapshot конкретного audited build.

Он не является lock-файлом и не гарантирует, что следующая сборка получит идентичный dependency set.

Machine-readable SPDX или CycloneDX SBOM пока не используется.

Причина: текущий repository не содержит закреплённого SBOM generator workflow, а ручное создание machine-readable dependency relationships может снизить точность данных.

## 13. Рекомендуемая структура GitHub Release

После завершения чистой финальной сборки рекомендуемая структура release assets:

```text
OPDS-Desk-1.0.0.exe
LICENSE
THIRD_PARTY_NOTICES.md
THIRD_PARTY_LICENSES.zip
SHA256SUMS.txt
```

Дополнительно исходный GitHub repository содержит:

```text
docs/SBOM.md
docs/DISTRIBUTION.md
docs/RELEASE.md
```

SBOM может быть опубликован как часть source repository и при необходимости приложен к release assets.

## 14. SHA-256

SHA-256 необходимо вычислять только для окончательного релизного EXE.

Пример:

```powershell
Get-FileHash -Algorithm SHA256 .\dist\OPDS-Desk-1.0.0.exe
```

Если EXE был пересобран хотя бы один раз, предыдущая контрольная сумма больше не относится к текущему artifact.

Для опубликованного release рекомендуется создать:

```text
SHA256SUMS.txt
```

только после окончательной сборки.

## 15. Минимальный release gate

Windows EXE можно считать готовым к публичному распространению только после выполнения всех следующих условий:

1. Сборка выполнена в контролируемом environment.
2. В bundle отсутствуют случайные DLL из несвязанных toolchains.
3. Version metadata соответствует версии приложения.
4. Получен окончательный SHA-256.
5. Повторно проверен PyInstaller dependency inventory.
6. Повторно проверены WebView2 components.
7. Подтверждено отсутствие Fixed Version WebView2 Runtime, если distribution model не был намеренно изменён.
8. Проверены bundled Microsoft .NET compatibility assemblies.
9. Обновлён artifact-specific SBOM.
10. Проверен `THIRD_PARTY_NOTICES.md`.
11. Проверен набор `THIRD_PARTY_LICENSES/`.
12. Все обязательные license/NOTICE materials включены в release package.

## 16. Исходный репозиторий и бинарный релиз

Готовность исходного кода OPDS Desk к публикации под GPL-3.0-only и готовность Windows executable с его сопроводительными third-party материалами проверяются отдельно.

Исходный repository и бинарный release следует рассматривать как два отдельных этапа публикации.

## 17. Связанные документы

- [README](../README.md)
- [Russian README](../README_RU.md)
- [Security Policy](../SECURITY.md)
- [Release procedure](RELEASE.md)
- [Software Bill of Materials](SBOM.md)
- [Third-Party Notices](../THIRD_PARTY_NOTICES.md)
- [Project License](../LICENSE)
