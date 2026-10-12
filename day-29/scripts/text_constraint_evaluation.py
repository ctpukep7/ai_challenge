"""Offline checks for short plain Russian sentences; never generate or repair text.

The sentence counter supports simple sentences terminated by one '.', '!' or '?'.
Abbreviations, decimals and ellipses are outside its scope. Topic and coherence
need a separate human review: a formal pass is not a complete quality judgment.
"""
import re
import unicodedata


SCOPE = "Короткие простые предложения с одиночной точкой, ! или ?; без аббревиатур, десятичных чисел и многоточий. Тема и связность проверяются вручную."


def evaluate(text, forbidden_characters=("а", "А"), required_sentences=2):
    """Evaluate the entire NFC-normalized answer, including any extra text."""
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if isinstance(required_sentences, bool) or not isinstance(required_sentences, int) or required_sentences < 1:
        raise ValueError("required_sentences must be a positive integer")
    if not isinstance(forbidden_characters, (tuple, list)) or not forbidden_characters:
        raise ValueError("forbidden_characters must contain individual characters")
    if any(not isinstance(char, str) or len(char) != 1 for char in forbidden_characters):
        raise ValueError("forbidden_characters must contain individual characters")
    forbidden = tuple(dict.fromkeys(forbidden_characters))
    normalized = unicodedata.normalize("NFC", text)
    forbidden_counts = {char: normalized.count(char) for char in forbidden}
    latin, invisible, other_letters = [], [], []
    for index, char in enumerate(normalized):
        name = unicodedata.name(char, "")
        category = unicodedata.category(char)
        item = {"index": index, "character": char, "codepoint": f"U+{ord(char):04X}"}
        if category.startswith("L") and "LATIN" in name:
            latin.append(item)
        elif category.startswith("L") and "CYRILLIC" not in name:
            other_letters.append(item)
        if (category in ("Cf", "Cs", "Co", "Cn", "Mn", "Me") or
                (category == "Cc" and char not in "\n\r\t") or
                (char.isspace() and char not in " \n\r\t")):
            invisible.append(item)

    format_errors = []
    if not normalized.strip():
        format_errors.append("empty_text")
    if any(char in normalized for char in "#*_`[]{}:"):
        format_errors.append("markup_or_heading")
    if re.search(r"(?m)^\s*(?:[-+•]|\d+[.)])\s+", normalized):
        format_errors.append("list")
    if re.search(r"[.!?]{2,}", normalized):
        format_errors.append("ambiguous_terminal_punctuation")
    if re.search(r"\d\.\d", normalized):
        format_errors.append("decimal_outside_counter_scope")

    # Do not select a successful prefix: all fragments and the unfinished tail count.
    sentences = []
    cursor = 0
    for match in re.finditer(r"([^.!?]*)([.!?])", normalized):
        fragment = match.group(1).strip()
        if not fragment or not any("CYRILLIC" in unicodedata.name(char, "") for char in fragment):
            format_errors.append("empty_or_non_russian_sentence")
        sentences.append((fragment + match.group(2)).strip())
        cursor = match.end()
    if normalized[cursor:].strip():
        format_errors.append("unfinished_or_extra_text")
    if len(sentences) != required_sentences:
        format_errors.append("wrong_sentence_count")
    format_errors = list(dict.fromkeys(format_errors))
    forbidden_total = sum(forbidden_counts.values())
    return {
        "normalization": "NFC", "normalized_text": normalized, "counter_scope": SCOPE,
        "required_sentences": required_sentences, "sentence_count": len(sentences), "sentences": sentences,
        "forbidden_counts": forbidden_counts, "forbidden_total": forbidden_total,
        "latin_characters": latin, "invisible_characters": invisible, "other_letters": other_letters,
        "format_errors": format_errors,
        "formal_pass": not (forbidden_total or latin or invisible or other_letters or format_errors),
        "semantic_review_required": True,
    }
