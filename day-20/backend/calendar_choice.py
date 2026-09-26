"""Validate an explicit chat choice against the current discovered catalogue."""
import re


def _normalize(value):
    value=str(value).casefold().translate(str.maketrans('', '', '\"\'«»“”„‘’'))
    return ' '.join(value.split()).strip()


def resolve_calendar_selection(prompt,calendars):
    text=_normalize(prompt)
    if not text or any(mark in text for mark in ('?','？')):return None
    if re.fullmatch(r'\d+|(?:первый|второй|третий|четвёртый|четвертый|последний)',text):return None
    # Match the known label as a whole, so a label such as «Не пропустить» is
    # not mistaken for an instruction to refuse it. Negatives outside a known
    # exact target cannot match this positive, anchored grammar.
    prefix=r'(?:(?:да[, ]+)?(?:я\s+)?(?:используй|используйте|используем|выбираю|выбери|выберите|возьми|возьмите|беру|мой выбор|хочу использовать)\s+(?:календарь\s+)?)'
    variants=[text]
    if text.endswith(('.', '!')):variants.append(text[:-1].rstrip())
    for variant in variants:
        targets={variant,re.sub('^'+prefix,'',variant,count=1)}
        candidates=[calendar for calendar in calendars if targets & {_normalize(calendar['name']),_normalize(calendar['id'])}]
        if candidates:return candidates[0]['id'] if len(candidates)==1 else None
    return None
