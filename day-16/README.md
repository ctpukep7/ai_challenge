# День 16 — подключение MCP и список tools

Минимальный Python-клиент подключается к MCP-серверу через актуальный
**Streamable HTTP** transport, выполняет `initialize` и получает каталог через
`tools/list`. Обычный режим не исполняет инструменты: выводятся только имя,
описание и входная JSON Schema. Явные demo-команды ниже могут выполнить
проверенный read-only tool.

По умолчанию выбран публичный endpoint `https://mcp.deepwiki.com/mcp`. Это
внешний сервис без гарантии постоянной доступности или неизменного каталога,
поэтому URL можно заменить аргументом или переменной окружения. Секреты для
этой демонстрации не нужны.

## Запуск

```bash
cd day-16
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Публичная проверка: соединение + tools/list
python client.py

# Тот же результат в JSON: удобно для автоматической обработки
python client.py --json

# Другой совместимый Streamable HTTP endpoint
python client.py --url https://example.com/mcp
MCP_SERVER_URL=https://example.com/mcp python client.py
```

Успешный текстовый вывод начинается с `Connection established`, затем содержит
адрес сервера, количество tools и их метаданные. JSON-режим возвращает объект
с полями `connected`, `server` и `tools`.

При сетевой или протокольной ошибке скрипт пишет понятное сообщение в stderr и
завершается с кодом `1`; `Ctrl+C` — с кодом `130`. Таймаут можно настроить:

```bash
python client.py --timeout 10
```

## Проверенный read-only вызов для демонстрации

Обычный режим выше ограничен только discovery, как требует задание. Если для
видео нужно показать и **явный безопасный вызов инструмента**, используйте
проверенный публичный tool DeepWiki:

```bash
python client.py --demo-repo modelcontextprotocol/python-sdk --json
```

Команда сначала получает `tools/list`, затем вызывает только
`read_wiki_structure` с `repoName`. Это read-only запрос: он возвращает список
тем документации указанного публичного GitHub-репозитория и ничего не изменяет.
Он выполняется исключительно при явном `--demo-repo`; простой `python client.py`
по-прежнему не вызывает никаких tools.

## Офлайн-тест

```bash
cd day-16
python -m unittest -v
```

Тест поднимает `FastMCP` server в памяти с заранее известным `hello` tool.
Поэтому CI доказывает инициализацию и получение каталога без сети и без
зависимости от публичного endpoint.

## Локальный MCP server

Для наглядной демонстрации есть отдельный локальный сервер с двумя read-only
инструментами одного источника: `get_weather(city: str)` и `get_forecast(city: str)`.
Это один процесс
`mcp_server.py`, который запускается `mcp_client.py` как подпроцесс и использует
стандартный MCP transport **stdio**. Клиент выполняет `initialize`, `tools/list`
и по явному выбору вызывает один tool.

```bash
cd day-16
source .venv/bin/activate

# Запускает один server, находит оба tools и вызывает get_weather для Омска.
python mcp_client.py --city Омск

# Тот же server, но клиент вызывает прогноз Open-Meteo на ближайшие три дня.
python mcp_client.py --tool forecast --city Омск

# Проверки работают в памяти и не используют сеть.
python -m unittest -v
```

Сам server при необходимости можно запустить отдельно командой
`python mcp_server.py`; его stdout зарезервирован для MCP-протокола.

Оба инструмента сначала запрашивают координаты города в Open-Meteo Geocoding API,
затем данные в Forecast API. `get_weather` отдаёт текущие температуру, ощущаемую
температуру, ветер и погодный код; `get_forecast` — компактные три дня с датой,
минимальной/максимальной температурой, максимальной вероятностью осадков и
погодным кодом. Это реальные данные без API-ключа; успешный ответ содержит
`"source": "open-meteo"`. У Open-Meteo может быть временная сетевая
недоступность, поэтому server использует короткий timeout и тогда возвращает
структурированную ошибку, а не demo-данные.

## Данные и Git

Для подключения к публичному серверу и получения погоды ключи не нужны.
DeepWiki получает адрес выбранного публичного репозитория только при явном
`--demo-repo`; Open-Meteo получает название города и его координаты при погодном
вызове. Модель LLM в этом дне не используется.

В Git входят исходники, тесты и инструкции. Локальные `.env` и их варианты,
виртуальное окружение, Python-кэш, базы, логи и папка `data/` исключены через
`.gitignore`; демонстрационные видео исключены общими правилами репозитория.

## Источники

- [MCP Python SDK: Streamable HTTP client](https://github.com/modelcontextprotocol/python-sdk/blob/main/examples/snippets/clients/streamable_basic.py)
- [MCP Python SDK: transports](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/client/transports.md)
- [MCP specification: tools/list](https://modelcontextprotocol.io/specification/draft/server/tools/)
