from backend.logic import context_for, evaluate, messages_for


FIXTURE = {
    "source": "day-07/README.md",
    "section": "Что сохраняется",
    "expected": [
        {"fact": "сохраняет историю", "aliases": ["сохраняет историю"]},
        {"fact": "использует SQLite", "aliases": ["использует SQLite"]},
    ],
}
CHUNKS = [{
    "source": "day-07/README.md",
    "title": "День 7 — Контекст агента",
    "section": "День 7 — Контекст агента / Что сохраняется",
    "chunk_id": "abc123",
    "text": "Сущности сохраняет историю и использует SQLite.",
    "score": 0.84,
}]


def test_prompt_keeps_baseline_free_of_context():
    question = "Как сохраняется история в дне 7?"
    plain = messages_for(question, None)
    rag = messages_for(question, CHUNKS)
    assert plain[0] == rag[0]
    assert plain[1]["content"] == question
    assert "сохраняет историю" not in plain[1]["content"]
    assert "source: day-07/README.md" in rag[1]["content"]
    assert "chunk_id: abc123" in rag[1]["content"]
    assert "[1]" in context_for(CHUNKS)


def test_context_has_per_chunk_limit():
    long = [{**CHUNKS[0], "text": "x" * 3000}]
    assert "x" * 1900 in context_for(long)
    assert "x" * 1901 not in context_for(long)


def test_evaluation_checks_all_facts_exact_source_section_and_citation_range():
    good = evaluate("Сохраняет историю и использует SQLite [1]", FIXTURE, CHUNKS)
    assert good["fact_hits"] == [True, True]
    assert good["expected_source_retrieved"] is True
    assert good["citation_markers_valid"] is True
    bad = evaluate("Сохраняет историю [5]", FIXTURE, CHUNKS)
    assert bad["fact_hits"] == [True, False]
    assert bad["has_citation_marker"] is True
    assert bad["citation_markers_valid"] is False
    assert evaluate("Сохраняет историю", FIXTURE, None)["expected_source_retrieved"] is None
