# Разработка OPDS Desk

Этот документ описывает текущий workflow разработки OPDS Desk на Windows: подготовку окружения, запуск из исходников, проверку изменений и локальную сборку. Устройство приложения подробнее описано в [Архитектура OPDS Desk](ARCHITECTURE.md), а работа с локальными настройками и данными — в [Конфигурация OPDS Desk](CONFIGURATION.md).

## Окружение разработки

Для разработки используются:

- Windows;
- Python 3;
- `pip` для установки пакетов;
- стандартный модуль `venv` для виртуального окружения.

Текущая подтверждённая среда разработки использует Python 3.13. Репозиторий пока не задаёт официальную минимальную версию Python и не заявляет поддержку конкретных более старых версий.

В репозитории нет `pyproject.toml`, `setup.cfg`, `requirements-dev.txt` и отдельного lock-файла. Runtime-зависимости закреплены непосредственно в `requirements.txt`; зависимость для сборки устанавливается отдельно при необходимости.

## Структура репозитория

- `app.py` — основная реализация desktop-приложения, backend, встроенный UI и запуск pywebview.
- `requirements.txt` — закреплённые runtime-зависимости Python.
- `tests/` — unittest suite и вспомогательный тестовый код.
- `tests/fixtures/` — локальные данные для воспроизводимых тестов, включая OPDS XML.
- `OPDS-Desk.spec` — конфигурация PyInstaller для onedir-сборки.
- `OPDS-Desk-OneFile.spec` — конфигурация PyInstaller для onefile-сборки.
- `version_info.txt` — Windows version metadata для исполняемого файла.
- `README.md` — основная пользовательская документация на английском языке.
- `README_RU.md` — пользовательская документация на русском языке.
- `docs/` — документация по архитектуре, конфигурации и разработке.

Локальные `.venv/`, `build/` и `dist/` не относятся к tracked-структуре репозитория.

## Подготовка виртуального окружения

Создайте и активируйте виртуальное окружение в PowerShell, затем установите runtime-зависимости:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r .\requirements.txt
```

Установить зависимости можно и без activation:

```powershell
.\.venv\Scripts\python.exe -m pip install -r .\requirements.txt
```

## Runtime-зависимости

| Пакет | Версия | Назначение |
| --- | --- | --- |
| Flask | 3.1.3 | локальный backend, routes, session и рендеринг UI |
| requests | 2.34.2 | HTTP(S)-запросы к OPDS-источникам и acquisition URL |
| pywebview | 6.2.1 | desktop-окно и JavaScript ↔ Python bridge |

Дополнительные runtime-пакеты в `requirements.txt` не объявлены.

## Запуск из исходников

Без activation окружения:

```powershell
.\.venv\Scripts\python.exe .\app.py
```

С активированным окружением:

```powershell
python .\app.py
```

`app.py` создаёт desktop-окно pywebview и передаёт ему локальное Flask-приложение. Отдельный внешний Flask server запускать не требуется.

### Особенности импорта app.py

Импорт полного `app.py` имеет startup side effects. Код на уровне модуля определяет и загружает конфигурацию, загружает `jobs.json`, инициализирует `queue.db`, выполняет восстановление незавершённого состояния, очищает временные файлы и запускает daemon-поток планировщика. При наличии незавершённого queue run recovery может запустить worker.

Создание окна защищено условием `if __name__ == "__main__"`, но перечисленная инициализация выполняется и при обычном импорте. Поэтому многие тесты читают `app.py` как исходный текст, разбирают его через AST и исполняют только необходимые определения в изолированном module namespace. Это позволяет проверять отдельные подсистемы без запуска полного runtime startup.

## Работа с app.py

Текущая реализация монолитная: большинство подсистем находится в одном файле. Основные логические области:

- разрешение app-data и конфигурация;
- модели OPDS и `OPDS1Provider`;
- HTTP-клиенты metadata и acquisition;
- поиск и OpenSearch;
- навигация и кэширование каталогов;
- SQLite-очередь, runs, scheduler и notifications;
- downloader и валидаторы EPUB/FB2;
- Flask UI и routes;
- startup и окно pywebview.

При локальном изменении важно учитывать соседние определения, используемые AST-based тестами: перенос или переименование зависимости может потребовать синхронного изменения изолированного test namespace.

### Встроенные HTML/CSS/JavaScript

HTML-шаблоны преимущественно объявлены как строковые константы в `app.py` и рендерятся через `render_template_string`. CSS и JavaScript находятся рядом с соответствующими UI templates, поэтому изменение интерфейса может затрагивать Python string literals, Jinja и browser code одновременно.

После изменения UI обязательно выполните:

- `py_compile` для проверки синтаксиса Python-строк;
- соответствующие route/UI tests;
- ручной smoke test в desktop-окне.

## Тестирование

Полный unittest suite запускается командой:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Тесты охватывают OPDS parser, нормализацию URL, OpenSearch и поиск, навигацию по каталогам, registries и кэши, очередь, downloader, проверку EPUB/FB2, persistence, compatibility behavior, UI/routes и packaging metadata. Процент покрытия репозиторий не фиксирует.

### Проверка синтаксиса

После изменения `app.py` выполните быстрый обязательный check:

```powershell
.\.venv\Scripts\python.exe -m py_compile .\app.py
```

Успешный `py_compile` подтверждает синтаксическую корректность файла, но не заменяет unit tests.

### Запуск отдельных тестов

Существующий модуль тестов search cache можно запустить отдельно:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_opds_search_cache -v
```

Пример запуска одного существующего метода:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_opds_search_cache.OPDSSearchCacheTests.test_b_page_zero_uses_expanded_search_template -v
```

Для изменения в другой подсистеме выберите ближайший существующий test module, затем перед завершением запустите полный suite.

## Test fixtures

Каталог `tests/fixtures/opds/` содержит локальные Atom/OpenSearch XML fixtures для offline-проверок parser, search discovery, pagination, navigation и связанных каталогов.

Fixtures должны быть детерминированными и не должны содержать реальные приватные OPDS URL или пользовательские данные. Новый edge case предпочтительно оформлять отдельным fixture, когда это делает ожидаемый feed и тест понятнее.

Текущий основной suite не требует реальных сетевых запросов: HTTP-сценарии используют внедрённые fake clients, `FakeSession`/`FakeResponse` и локальные fixtures. Создание обычного `requests.Session` в отдельных проверках не сопровождается обращением к удалённому серверу.

## Локальная сборка

PyInstaller является зависимостью для сборки и не входит в `requirements.txt`. При необходимости установите его отдельно в виртуальное окружение:

```powershell
.\.venv\Scripts\python.exe -m pip install PyInstaller
```

Onedir-сборка:

```powershell
.\.venv\Scripts\python.exe -m PyInstaller --clean --noconfirm .\OPDS-Desk.spec
```

Результат:

```text
dist\OPDS-Desk\OPDS-Desk.exe
```

Onefile-сборка:

```powershell
.\.venv\Scripts\python.exe -m PyInstaller --clean --noconfirm .\OPDS-Desk-OneFile.spec
```

Результат:

```text
dist\OPDS-Desk.exe
```

Версия PyInstaller не закреплена в репозитории, поскольку отдельного файла зависимостей для сборки сейчас нет. Подробный release checklist приведён в [процедуре выпуска](RELEASE.md); этот документ не является release procedure.

## Версия приложения

Текущая версия приложения — 1.0.0. Runtime-значение задаётся константой `APP_VERSION` в `app.py`. Windows metadata хранится в `version_info.txt`, а оба spec-файла передают этот файл PyInstaller.

При изменении версии необходимо проверить согласованность `APP_VERSION`, `ProductVersion`, `FileVersion` и числовых полей версии Windows. Текущая Windows `FileVersion` использует четырёхкомпонентную запись `1.0.0.0`, а product/runtime version — `1.0.0`.

## Локальные данные при разработке

Запуск полного `app.py` использует application data текущего пользователя. Разработчику следует учитывать существующие `config.json`, `queue.db`, `jobs.json` и выбранную папку локальной библиотеки: тестовый запуск может увидеть или обновить это состояние.

Расположение данных, их назначение и безопасное резервное копирование описаны в [Конфигурация OPDS Desk](CONFIGURATION.md). Не очищайте application data как обычный способ подготовки development environment.

## Проверка изменений перед commit

Перед staging проверьте рабочее дерево и whitespace:

```powershell
git status --short
git diff --check
```

Добавляйте в staging только ожидаемые файлы. После staging выполните:

```powershell
git diff --cached --check
```

## Минимальная проверка изменения

1. Проверить список изменённых файлов.
2. Выполнить `py_compile`, если менялся `app.py`.
3. Запустить targeted tests изменённой подсистемы.
4. Перед завершением изменения запустить полный unittest suite.
5. Для UI/runtime изменений выполнить ручной smoke test desktop-окна.
6. Выполнить `git diff --check`.
7. Перед commit выполнить `git diff --cached --check`.
