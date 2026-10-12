import importlib.util
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location("text_constraint_evaluation", Path(__file__).resolve().parents[1] / "scripts/text_constraint_evaluation.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
evaluate = module.evaluate


@pytest.mark.parametrize("text", ["Снег идёт. Лёд блестит.", "  Снег идёт!\nЛёд блестит?\n", "Мороз щиплет нос. Снег летит."])
def test_two_complete_plain_sentences_without_forbidden_letters(text):
    result = evaluate(text)
    assert result["formal_pass"] and result["sentence_count"] == 2
    assert result["forbidden_counts"] == {"а": 0, "А": 0}
    assert result["semantic_review_required"] is True


def test_both_cases_counted_and_no_trimming_to_successful_suffix():
    result = evaluate("Зима. А снег летит. Лёд блестит.")
    assert not result["formal_pass"]
    assert result["forbidden_counts"] == {"а": 1, "А": 1}
    assert result["sentence_count"] == 3 and "wrong_sentence_count" in result["format_errors"]


@pytest.mark.parametrize("text,error", [
    ("Снег идёт. Лёд блестит. Мороз щиплет нос.", "wrong_sentence_count"),
    ("Снег идёт. Лёд блестит", "unfinished_or_extra_text"),
    ("Снег идёт. Лёд блестит. Конец", "unfinished_or_extra_text"),
    ("Снег идёт.", "wrong_sentence_count"),
    ("", "empty_text"), ("Снег идёт.. Лёд блестит.", "ambiguous_terminal_punctuation"),
    ("Снег идёт... Лёд блестит.", "ambiguous_terminal_punctuation"),
    (". Снег идёт.", "empty_or_non_russian_sentence"),
    ("1. Снег идёт.\n2. Лёд блестит.", "list"),
    ("- Снег идёт.\n- Лёд блестит.", "list"),
    ("Ответ: Снег идёт. Лёд блестит.", "markup_or_heading"),
    ("**Снег идёт. Лёд блестит.**", "markup_or_heading"),
    ("Снег идёт 1.5 суток. Лёд блестит.", "decimal_outside_counter_scope"),
])
def test_format_constraints_cover_entire_response(text, error):
    result = evaluate(text)
    assert not result["formal_pass"] and error in result["format_errors"]


@pytest.mark.parametrize("text", ["Зимa идёт. Лёд блестит.", "Снeг идёт. Лёд блестит.", "Зимá идёт. Лёд блестит."])
def test_latin_homoglyphs_and_accented_latin_are_not_an_escape(text):
    result = evaluate(text)
    assert not result["formal_pass"] and result["latin_characters"]


@pytest.mark.parametrize("hidden", ["\u200b", "\u200d", "\u202e", "\ufeff", "\x00", "\u00a0", "\u2060", "\u034f", "\ufe0f"])
def test_invisible_symbols_and_nonstandard_spaces_are_rejected(hidden):
    result = evaluate(f"Снег{hidden} идёт. Лёд блестит.")
    assert not result["formal_pass"] and result["invisible_characters"]


def test_nfc_normalization_only_does_not_remove_forbidden_letters_or_rewrite_text():
    result = evaluate("Снег иде\u0308т. Ле\u0308д блестит.")
    assert result["formal_pass"] and result["normalized_text"] == "Снег идёт. Лёд блестит."
    forbidden = evaluate("Зима\u0301 идёт. Лёд блестит.")
    assert not forbidden["formal_pass"] and forbidden["forbidden_total"] == 1


def test_formal_pass_does_not_claim_topic_or_coherence_and_other_scripts_are_rejected():
    result = evaluate("Кот спит. Пёс ест.")
    assert result["formal_pass"] and result["semantic_review_required"]
    assert "topic_pass" not in result
    assert not evaluate("Снег идёт α. Лёд блестит.")["formal_pass"]


@pytest.mark.parametrize("text", [None, 1, {}, []])
def test_non_text_is_not_coerced(text):
    with pytest.raises(TypeError):
        evaluate(text)


@pytest.mark.parametrize("kwargs", [{"required_sentences": True}, {"required_sentences": 0}, {"forbidden_characters": []}, {"forbidden_characters": ["аа"]}])
def test_invalid_configuration_rejected(kwargs):
    with pytest.raises(ValueError):
        evaluate("Снег идёт. Лёд блестит.", **kwargs)
