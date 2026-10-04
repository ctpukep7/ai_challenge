import pytest

from backend.logic import (context_for, evaluate, local_rewrite, messages_for,
                           parse_rewrite, retrieval_evaluation, select_candidates, summarize)


def candidate(score, text="данные", section="главa", source="test.md"):
    return {"score": score, "text": text, "section": section, "source": source,
            "title": "тест", "chunk_id": text}


def test_threshold_boundary_order_and_top_k():
    values = [candidate(.9, "high"), candidate(.6, "boundary"), candidate(.6, "equal"), candidate(.59, "reject")]
    records, selected, counts = select_candidates(values, .6, 2, True)
    assert [item["text"] for item in selected] == ["high", "boundary"]
    assert [item["decision"] for item in records] == ["selected", "selected", "top_k", "below_threshold"]
    assert counts == {"retrieved": 4, "passed": 3, "kept": 2, "below_threshold": 1, "over_top_k": 1}
    assert [item["marker"] for item in selected] == ["[1]", "[2]"]
    _, baseline, baseline_counts = select_candidates(values, 1, 2, False)
    assert baseline == selected
    assert baseline_counts["below_threshold"] == 0


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), -float("inf"), "0.7"])
def test_corrupt_score_never_enters_context(invalid):
    with pytest.raises(ValueError):
        select_candidates([candidate(invalid)], .6, 2, True)


def test_rejected_data_absent_from_exact_prompt_original_question_preserved():
    _, chunks, _ = select_candidates([candidate(.8, "KEEP"), candidate(.1, "REJECT SECRET")], .6, 3, True)
    messages = messages_for("Почему DIP?", chunks)
    assert "Почему DIP?" in messages[1]["content"]
    assert "KEEP" in messages[1]["content"]
    assert "REJECT SECRET" not in str(messages)
    assert "[2]" not in context_for(chunks)


def test_local_rewrite_general_rules_preserve_question_intent():
    result = local_rewrite("  Как MCP и std io работают в Day-16? Не запускай cron.  ")
    assert "MCP протокол подключения инструментов" in result["query"]
    assert "stdio стандартный ввод и вывод" in result["query"]
    assert "день 16" in result["query"]
    assert "Не запускай cron планировщик фоновых задач" in result["query"]
    assert result["changed"] and len(result["applied_rules"]) == 5
    assert local_rewrite("Сколько стоит авиабилет?")["changed"] is False
    assert "SQLite" not in local_rewrite("Как агент сохраняет историю?")["query"]
    assert "RAG поиск чанков" in local_rewrite("Как работает RAG?")["query"]



@pytest.mark.parametrize("answer", ['not json', '[]', '{"query":null}', '{"query":""}', '{"query":"x","answer":"y"}', '{"query":"' + 'x' * 601 + '"}'])
def test_llm_query_contract_rejects_malformed(answer):
    with pytest.raises(ValueError):
        parse_rewrite(answer)


def test_llm_query_contract_accepts_only_nonempty_query():
    assert parse_rewrite('{"query":"  вопрос  "}') == "вопрос"


def test_proxy_and_negative_evaluation_are_explicit():
    fixture = {"source": "test.md", "section": "главa", "expected": [{"fact": "пример", "aliases": ["пример"]}]}
    chunks = [candidate(.8), candidate(.7, section="другое")]
    assert retrieval_evaluation(fixture, chunks)["chapter_precision_proxy"] == .5
    assert evaluate("Пример [3]", fixture, chunks)["citation_markers_valid"] is False
    negative = {"unanswerable": True, "expected": []}
    assert retrieval_evaluation(negative, [])["correct_empty"] is True
    assert evaluate("Отказ приложения", negative, []) is None


def test_summary_uses_common_successful_denominators_excludes_policy_and_errors():
    def mode(ratio=1, status="completed", policy=False):
        return {"counts": {"kept": 1, "below_threshold": 2}, "answer_status": status,
                "generated_by": "policy" if policy else "llm",
                "evaluation": {"fact_ratio": ratio, "citation_markers_valid": True},
                "retrieval_evaluation": {"unanswerable": False, "chapter_hit": True, "chapter_precision_proxy": 1}}
    good = {"modes": {name: mode() for name in ("baseline", "filtered", "rewritten")}}
    failed = {"modes": {"baseline": mode(0), "filtered": mode(0, "failed"), "rewritten": mode(0)}}
    policy = {"modes": {name: mode(0, "abstained", True) for name in ("baseline", "filtered", "rewritten")}}
    unfinished = {"modes": {"baseline": mode(), "filtered": mode(), "rewritten": None}}
    result = summarize([good, failed, policy, unfinished], {"answer": 8, "rewrite": 1})
    assert result["retrieval"]["common_count"] == 3
    assert result["generation"]["common_count"] == 1
    assert all(item["fact_ratio"] == 1 for item in result["generation"]["modes"].values())
    assert result["calls"]["total"] == 9
    assert summarize([], {"answer": 0, "rewrite": 0})["retrieval"]["modes"]["baseline"]["chapter_hit_rate"] is None
