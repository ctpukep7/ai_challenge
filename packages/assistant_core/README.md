# assistant_core

Общий устанавливаемый Python 3.12 пакет новой недели. Не импортирует предыдущие
дни и не меняет их окружения/индексы. Версии проверенного окружения находятся
в `requirements.lock.txt`; установка из корня:

```bash
.assistant-venv/bin/pip install -r packages/assistant_core/requirements.lock.txt
.assistant-venv/bin/pip install --no-build-isolation -e packages/assistant_core
```

Экспорт: `Document`, `Chunk`, `load_documents(root, paths)`,
`documentation_paths(root)`, `chunk_document(doc, embedder)`,
`FastEmbedder(cache_dir)`, `Index(path, root, paths, embedder)`, `DeepSeek`,
`grounded_answer(question, sources, context, history, provider)`,
`validate_answer(result, sources)`, `ProviderError`.
`paths` — явный список либо callable, возвращающий список относительных путей.
Index предоставляет `status()`, атомарный `build()`, `search(query, top_k=5,
threshold=.35)`. Изменённый корпус запрещает поиск до rebuild.
Для ограничения отдельными источниками есть `source_paths`. Поисковый запрос
длиннее 126 токенов ограничивается первым embedding-окном; генерация получает
полный вопрос. Уточняйте длинные смешанные вопросы для более точного поиска.
`assistant_core.mcp_client.call_tools(server_path, [(name,args)], env=None)` —
async, возвращает `(results_by_tool, trace)` настоящих stdio-вызовов.
Модель embeddings — только cache-only, 2 CPU threads, локальный MiniLM384.

Тестовые doubles передаются явно. В production нет фиктивных embeddings,
готовых ответов или выдуманной трассы. Проверка точной цитаты не является
проверкой смысловой истинности. API-ключ и HTTP-заголовки в результатах не
сохраняются. Сеть DeepSeek имеет deadline; автоматических billed retries нет.
JSON-генерация использует thinking=disabled. finish_reason=length отклоняется
явной ошибкой; частичный JSON не ремонтируется и не публикуется.

`Index.expand_neighbors(sources, limit=2)` добавляет максимум двух соседей
в порядке исходных чанков того же документа. У соседа score=None, роль neighbor
и anchor_id; он не считается прошедшим порог поиска.
