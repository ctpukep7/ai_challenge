# День 32 · Автоматическое AI-ревью PR

CLI и GitHub Action анализируют diff с контекстом документации и кода AI Challenge.
DeepSeek возвращает потенциальные баги, архитектурные проблемы и рекомендации.
У дня нет frontend. Соседние приложения и их окружения не используются.

## Установка с чистого checkout

Из корня проекта (Python 3.12):

```sh
python3.12 -m venv .assistant-venv
.assistant-venv/bin/python -m pip install -r packages/assistant_core/requirements.lock.txt
.assistant-venv/bin/python -m pip install --no-deps --no-build-isolation ./packages/assistant_core ./day-32
```

Локально используется прежний embedding-кэш `day-21/.cache/fastembed`, либо путь
`ASSISTANT_EMBED_CACHE`. Его модель —
`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`. Если кэша нет,
один раз подготовьте **embedding-модель** (это не генеративная LLM):

```sh
ASSISTANT_EMBED_CACHE=/absolute/path/to/cache .assistant-venv/bin/python -c 'import os; from fastembed import TextEmbedding; from assistant_core.rag import MODEL; TextEmbedding(model_name=MODEL, cache_dir=os.environ["ASSISTANT_EMBED_CACHE"], threads=2)'
```

После подготовки эмбеддинги работают локально. Веса новой LLM не нужны.
Workflow на чистом runner отдельно загружает embedding-модель в `runner.temp`.

## Три отдельных этапа

CLI устанавливается как Python-пакет. Примеры ниже запускаются из корня.
Из `day-32` используйте `../.assistant-venv/bin/python -m day32_review ...`;
для generation укажите `--root ..`, поскольку текущий каталог по умолчанию
должен быть корнем доверенного checkout.

```sh
.assistant-venv/bin/python -m day32_review collect --repo OWNER/REPO --pr 123 --work day-32/data/review
.assistant-venv/bin/python -m day32_review generate --root . --work day-32/data/review
.assistant-venv/bin/python -m day32_review publish --repo OWNER/REPO --work day-32/data/review
```

`collect` и `publish` требуют `GITHUB_TOKEN`; `generate` — `DEEPSEEK_API_KEY`.
Переменные берутся из окружения, `.env` автоматически не читается. Никогда не
передавайте ключи аргументами команд и не сохраняйте их в JSON/README.
Для локального анализа `--root` должен указывать на корень checkout **base SHA** PR;
по умолчанию используется текущий каталог, в том числе после установки wheel.
До создания эмбеддингов и обращения к модели CLI проверяет настоящий Git HEAD,
его совпадение с base SHA снимка и отсутствие staged/unstaged изменений tracked
файлов. Untracked файлы в RAG не входят. При необходимости используйте отдельный
чистый checkout; сохраните свою рабочую копию. Дополнительный `TRUSTED_BASE_SHA`
проверяет согласованность снимка с доверенным SHA workflow.

- `collect`: через GitHub API получает base/head SHA, все страницы changed files,
  patch и текстовые версии файлов на точных SHA. Снимок — `snapshot.json`.
- `generate`: строит собственный SQLite/FastEmbed RAG на README, `docs`, исходниках
  дней и общего пакета **из доверенного base checkout**. В реальном режиме только tracked Git файлы:
  ignored/untracked корпус прошлых дней и локальные эксперименты не попадают в RAG. Текст PR head передаётся
  отдельным недоверенным блоком. Модель не имеет инструментов и не исполняет код.
- `publish`: повторно проверяет base/head SHA и обновляет один комментарий с
  маркером Day32, только если его автор — `github-actions[bot]` с типом `Bot`.
  Чужой комментарий с таким маркером не меняется. Упоминания, HTML и Markdown
  из модельного текста экранируются. Локальный PAT создаёт комментарий своим
  автором, поэтому обновление такого комментария намеренно не поддержано;
  штатная публикация выполняется Action от имени бота.

`report.json` и `report.md` содержат результат и покрытие. Источники base RAG
сохраняют путь, строки, хеш и фрагмент. Каждое замечание содержит категорию,
важность P1–P3, объяснение, признак гипотезы, файл, сторону base/head, строку и
точную цитату изменённой строки patch. Неподтверждённые замечания отбрасываются,
их число видно в отчёте, анализ помечается частичным. Для переименований ссылка
base ведёт на старое имя; для удалений — на base SHA.

Base RAG ограничен 1 MB исходного текста: сначала документация, затем изменённые
файлы и соседние модули, затем прочий разрешённый код; пропуски отражаются в
покрытии. Это ограниченный поиск, не полный аудит всего репозитория.

Лимиты: 60 файлов, по 6000 символов каждого patch/base/head, общий текст PR
100000 символов, до 6 LLM-пакетов по 20000 символов текста PR плюс top-5 RAG.
Обрабатываются максимум 30 замечаний на пакет; превышение явно отражается в
пропусках и делает отчёт частичным, даже если первые 30 замечаний корректны.
API GitHub ограничивает список PR 3000 файлами; неполнота отмечается отдельно.
Нет patch, бинарные/большие файлы, исключённые пути, обрезки и превышение бюджета
попадают в ограничения покрытия. Общий deadline одной команды — 15 минут;
DeepSeek — 100 секунд на пакет, без платных повторов. Ошибки конфигурации и
провайдера завершают CLI с ненулевым кодом, а не успешным пустым ревью.
Отсутствие замечаний не доказывает корректность. Автоисправлений и блокировки
merge нет.

## Изолированный пример для тестового PR

`day-32/examples/review_target.py` — небольшой самостоятельный модуль текущего
AI Challenge. Он не подключён к приложениям прежних дней; в main находится
корректная версия. Пример позволяет показать настоящее ревью PR без поломки
рабочих приложений. Намеренные ошибки в демонстрационном PR не следует вливать.

Инварианты для ревью и RAG:

- `subtotal(items)` принимает позиции с целочисленными `price_cents >= 0` и
  `quantity > 0`, считает **цену каждой единицы, умноженную на количество**.
  Пустая корзина даёт 0. Boolean не является допустимой ценой или количеством.
- Все суммы выражены в целых копейках/центах, без float и округления.
- `shipping_fee(subtotal_cents)` принимает целое неотрицательное значение;
  доставка бесплатна **начиная ровно с 5000**, ниже порога стоит 500.

Два воспроизводимых сценария PR: убрать множитель количества и заменить
включительную границу бесплатной доставки строгой. Ассистент должен объяснить
ошибку на корзине из нескольких единиц и на сумме ровно 5000, сослаться на
изменённые строки. Код PR для получения ревью исполнять не требуется.

## GitHub Action: включение и живая приёмка

Workflow: [day32-review.yml](../.github/workflows/day32-review.yml).
События `pull_request_target`: opened, synchronize, reopened; ручной запуск
`workflow_dispatch` принимает номер PR. Concurrency отменяет устаревший запуск.

Для запуска workflow и пакет ревьюера должны уже быть в **базовой ветке**.
В GitHub Settings → Secrets and variables → Actions нужно создать repository
secret `DEEPSEEK_API_KEY`. При отсутствии Secret шаг generation явно падает с
ошибкой конфигурации. После этого открыть тестовый PR внутри репозитория,
проверить комментарий и artifact, добавить коммит и проверить обновление того
же комментария. Fork PR пропускаются до checkout и доступа к DeepSeek.

Checkout всегда на base SHA, `persist-credentials: false`. Код head не
checkout-ится, не импортируется, не собирается, его зависимости не ставятся.
Права workflow: contents:read, pull-requests:write. DeepSeek Secret доступен
только generation, GitHub token передаётся collect/publish. Прямой передачи
выражений event в shell нет. Actions закреплены полными SHA проверенных
официальных releases: checkout4.2.2, setup-python5.6.0, github-script7.0.1,
upload-artifact4.6.2.

Artifacts сохраняются 7 дней: только report JSON/Markdown и error JSON; снимок PR,
локальная база и ключи не загружаются. Markdown также попадает в job summary.
Небольшое окно гонки между последней проверкой SHA и публикацией GitHub API
не устраняет: комментарий всегда показывает проверенный head SHA.

## Какие данные передаются

GitHub API получает owner/repo, номер PR, пути и SHA. DeepSeek получает выбранные
публичные фрагменты документации/кода base, diff и ограниченные тексты base/head;
названия файлов/веток и содержимое PR считаются недоверенными данными.
`.env`, скрытые каталоги, wiki, Memory Bank, node_modules, базы, видео, generated
assets, lock-файлы и пути с именами secret/credential/token исключены. Не создавайте
PR с секретами: проверка имён не является сканером секретов внутри обычного кода.
Точная цитата проверяет происхождение, но не доказывает смысловую верность ревью.

## Локальная проверка и offline replay

```sh
.assistant-venv/bin/python -m pytest day-32/tests -q
.assistant-venv/bin/python -m day32_review --help
```

Тесты используют `httpx.MockTransport`, общий настоящий SQLite RAG с тестовыми
эмбеддингами и fixture-провайдером. Проверяют пагинацию, fork, лимиты, rename/delete,
точные строки/цитаты, неправильные findings, отсутствие Secret, stale SHA,
авторство комментариев, экранирование и запрет исполнения PR.

Для ручного **offline replay** каждого этапа есть `--fixture FILE.json`.
Формат: `repo`, `pr` (GitHub-shaped base/head/changed_files), `files` (GitHub-shaped
changed files), `contents` (объект `SHA:path` → текст), `comments` и `findings`.
Пример запуска collect:

```sh
.assistant-venv/bin/python -m day32_review collect --pr 1 --fixture day-32/tests/replay.json --work day-32/data/offline
.assistant-venv/bin/python -m day32_review generate --fixture day-32/tests/replay.json --work day-32/data/offline
.assistant-venv/bin/python -m day32_review publish --fixture day-32/tests/replay.json --work day-32/data/offline
```

Fixture generation использует локальные настоящие эмбеддинги и заранее заданные
findings, без DeepSeek; публикация записывает только `fixture-mutations.json`.
Отчёт явно помечен OFFLINE FIXTURE и не может публиковаться через настоящий API.
Это проверка механики, не живая приёмка качества модели или GitHub Actions.

**Состояние проверки на 17 октября 2026:** 48 локальных тестов прошли;
проверены wheel-установка и запуск CLI из checkout. Исправлены default root и
проверка настоящего HEAD/чистоты base checkout. На локальном синтетическом diff
текущего проекта настоящий DeepSeek нашёл обе внесённые ошибки примера:
потерю quantity (строка 14) и границу доставки (строка 22). Использовались
настоящие локальные эмбеддинги и RAG; отчёт частичный из-за лимита корпуса 1 MB.
Это проверка модели, а не запуск GitHub Action.

Workflow опубликован и проверен на настоящем [PR №1](https://github.com/ctpukep7/ai_challenge/pull/1):

- [Запуск на открытие PR](https://github.com/ctpukep7/ai_challenge/actions/runs/39034556599) — success: комментарий бота и artifact созданы. Опубликована ошибка количества; ещё одно замечание отклонено строгой проверкой, отчёт partial.
- [Запуск на новый коммит](https://github.com/ctpukep7/ai_challenge/actions/runs/39034689038) — success: количество исправлено, ревью выявило оставшуюся ошибку границы доставки на строке 22. Обновлён [тот же комментарий](https://github.com/ctpukep7/ai_challenge/pull/1#issuecomment-6234157995), дубликатов нет, head SHA актуален.
- В обоих запусках artifact содержит JSON и Markdown. Строки замечаний и хеши RAG-источников дополнительно сверены с настоящими Git-объектами. Во втором запуске нет отклонённых замечаний; partial остаётся из-за лимита RAG-корпуса (427 источников вне бюджета 1 MB).

Secret добавлен владельцем репозитория; значение не читалось и не публиковалось.
Тестовый PR намеренно содержит ошибку только в отдельном примере; не вливать.
Проверены события opened и synchronize. Ручной workflow_dispatch доступен,
но отдельный живой ручной запуск в эту приёмку не входил.

## Демонстрация без голоса

1. Откройте PR №1 → **Commits**: первый коммит вносит две ошибки, второй исправляет количество.
2. В **Files changed** покажите оставшуюся замену `>= 5000` на `> 5000`.
3. Откройте второй запуск Action: зелёные collect, generate и publish.
4. Вернитесь в Conversation: комментарий `github-actions` с пометкой edited, текущим head SHA, объяснением и ссылкой на строку 22. Покажите три раздела ревью и явные ограничения покрытия.
5. На странице запуска откройте artifact: `report.md` и `report.json` с RAG-источниками. Для сравнения первого ревью доступен artifact первого запуска.

Для нового запуска без изменения кода используйте **Actions → Day32 AI review → Run workflow**,
укажите `pr_number=1`. Требуется вход в аккаунт с доступом к репозиторию. Эквивалент CLI:

```sh
gh workflow run day32-review.yml --repo ctpukep7/ai_challenge -f pr_number=1
```

Результат нового вызова модели может отличаться; отсутствие замечания не доказывает
корректность. Тестовый PR и production base остаются раздельными.

15 октября 2026: 25 offline-тестов прошли; общий пакет и CLI успешно собраны
в устанавливаемые wheels. Проверена YAML-структура workflow. Настоящие локальные
FastEmbed и DeepSeek на изолированном синтетическом diff обнаружили отключение
проверки авторизации: 2 RAG-источника, подтверждённая строка 2, один P1 finding,
без пропусков. Отчёт помечен fixture и запрещён к публикации в настоящий PR.
Это живая проверка модели на тестовых данных, не проверка GitHub Action.
