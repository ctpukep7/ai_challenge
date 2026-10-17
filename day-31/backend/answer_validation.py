"""Day31-only strict draft inspection; outputs contain valid claims or safe codes."""
from assistant_core import ProviderError, validate_answer

PARTIAL_WARNING = 'Ответ частичный: неподтверждённые части не показаны. Уточните вопрос для полного ответа.'


def inspect_answer(draft, ledger, quote_options=None):
    """Keep shared exact-substring rules, classify per-claim failures safely."""
    if not isinstance(draft, dict):
        raise ProviderError('Ответ DeepSeek должен быть объектом')
    if draft.get('status') not in ('answered', 'unknown'):
        raise ProviderError('Неверный статус ответа')
    claims = draft.get('claims', [])
    if not isinstance(claims, list):
        raise ProviderError('Неверный формат claims')
    if len(claims) > 7:
        raise ProviderError('Превышен лимит утверждений')
    if draft['status'] == 'unknown' or not claims:
        return validate_answer(draft, ledger), []
    known = {source['id']: source for source in ledger}
    valid = []; issues = []
    for index, claim in enumerate(claims):
        code = None
        if not isinstance(claim, dict):
            code = 'malformed_claim'
        elif not isinstance(claim.get('text'), str) or not claim['text'].strip():
            code = 'empty_text'
        elif not isinstance(claim.get('source_id'), str) or claim['source_id'] not in known:
            code = 'unknown_source'
        elif 'quote_id' in claim:
            reference = (quote_options or {}).get(claim['quote_id']) if isinstance(claim['quote_id'], str) else None
            if reference is None:
                code = 'unknown_quote_id'
            elif reference['source_id'] != claim['source_id']:
                code = 'quote_source_mismatch'
            elif 'quote' in claim and claim['quote'] != reference['quote']:
                code = 'quote_conflict'
            else:
                # Materialize only the model's explicit catalog selection.
                # The unchanged fragment must still pass the shared validator.
                claim = dict(claim, quote=reference['quote'])
        if not code and (not isinstance(claim.get('quote'), str) or not claim['quote'].strip()):
            code = 'missing_quote'
        elif not code and claim['quote'] not in known[claim['source_id']]['text']:
            code = 'invalid_quote'
        if code:
            issue = {'claim_index': index, 'code': code}
            if isinstance(claim, dict) and isinstance(claim.get('source_id'), str) and claim['source_id'] in known:
                issue['source_id'] = claim['source_id']
            issues.append(issue)
        else:
            # Shared validator remains the final authority; no quote replacement.
            validate_answer({'status': 'answered', 'claims': [claim]}, ledger)
            valid.append(claim)
    if not issues:
        return validate_answer(dict(draft, claims=valid), ledger), []
    if valid:
        result = validate_answer({'status': 'answered', 'claims': valid}, ledger)
    else:
        result = {'status': 'unknown', 'answer': 'Не знаю: не удалось подтвердить утверждения точными цитатами. Уточните вопрос или запросите конкретный файл.', 'sources': []}
    return dict(result, partial=True), issues


def repair_feedback(issues, ledger, draft=None, question=''):
    """Return <=2000 UTF-8 bytes and an immutable exact-fragment catalog.

    Ranking only orders candidate fragments; it never accepts an approximate
    quote or guesses a source for a model claim.
    """
    import json
    import re

    def words(text):
        return set(re.findall(r'[\w-]{3,}', text.casefold()))

    claims = draft.get('claims', []) if isinstance(draft, dict) else []
    affected = []
    keywords = words(question)
    for issue in issues:
        index = issue['claim_index']
        if index < len(claims) and isinstance(claims[index], dict):
            claim = claims[index]
            if isinstance(claim.get('source_id'), str): affected.append(claim['source_id'])
            keywords |= words(str(claim.get('text', ''))) | words(str(claim.get('quote', '')))

    def candidates(source):
        options = []
        for line in source['text'].splitlines():
            if len(line.strip()) < 3:
                continue
            # Prefix is still one unchanged contiguous substring, never normalized.
            fragment = line[:200]
            while len(fragment.encode()) > 200:
                fragment = fragment[:-1]
            if len(fragment.strip()) < 3:
                continue
            options.append(fragment)
        options = list(dict.fromkeys(options))
        options.sort(key=lambda text: -len(words(text) & keywords))
        # A file inventory should represent distinct folders rather than fill
        # every candidate slot with the first backend files.
        if source.get('path') is None and source.get('kind') == 'code':
            groups = {}; diversified = []
            for text in options:
                group = '/'.join(text.split('/')[:2]) if '/' in text else text
                groups.setdefault(group, []).append(text)
            while any(groups.values()):
                for group in groups.values():
                    if group: diversified.append(group.pop(0))
            options = diversified
        return options

    ranked = sorted(ledger, key=lambda source: (
        source['id'] not in affected,
        -len(words(source['text']) & keywords)))
    payload = {'type': 'answer_validation_feedback', 'issues': issues,
        'available_source_ids': [source['id'] for source in ranked[:24]],
        'instruction': 'Исправь JSON один раз. claims: {text,source_id,quote_id} с явным выбором quote_options; допустим прежний точный quote. Не меняй цитаты и не угадывай ID. Раздели backend, frontend и запуск на разные claims, если фрагмент подтверждает только часть. Нет доказательства — удали claim. Инструменты запрещены.',
        'quote_options': []}
    encode = lambda: json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
    # Reserve a bounded feedback even for unusually large evidence ledgers.
    while len(encode().encode()) > 2000 and payload['available_source_ids']:
        payload['available_source_ids'].pop()
    catalog = {}
    for source in ranked:
        for fragment in candidates(source)[:6]:
            quote_id = f'q{len(catalog)+1}'
            option = {'quote_id': quote_id, 'source_id': source['id'], 'quote': fragment}
            payload['quote_options'].append(option)
            if len(encode().encode()) > 2000:
                payload['quote_options'].pop()
                continue
            catalog[quote_id] = {'source_id': source['id'], 'quote': fragment}
            if len(catalog) >= 12:
                return encode(), catalog
    return encode(), catalog
