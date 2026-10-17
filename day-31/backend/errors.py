"""Classify safe public errors through nested AnyIO exception groups."""
from assistant_core import ProviderError


def public_errors(exc):
    """Keep every independent cause, never expose unknown exception messages.

    Base exceptions intentionally escape: cancellation and process shutdown are
    control flow, including when grouped with an ordinary failure.
    """
    if isinstance(exc, BaseExceptionGroup):
        errors = []
        for child in exc.exceptions:
            errors.extend(public_errors(child))
        return list(dict.fromkeys(errors))
    if not isinstance(exc, Exception):
        raise exc
    if isinstance(exc, ProviderError):
        message = str(exc)
        if 'DEEPSEEK_API_KEY' in message:
            code = 'configuration'
        elif any(word in message.lower() for word in ('цитат', 'ссылк', 'json', 'формат', 'сообщени', 'статус', 'утверждени', 'обрезан')):
            code = 'model_output'
        elif 'DeepSeek' in message:
            code = 'provider'
        elif 'MCP' in message:
            code = 'mcp'
        else:
            code = 'agent'
        return [(code, message)]
    if isinstance(exc, TimeoutError):
        return [('timeout', 'Истекло время выполнения запроса. Уточните вопрос или повторите его вручную.')]
    return [('internal', 'MCP или локальный агент недоступен. Проверьте журнал сервера.')]


def error_result(exc):
    errors = public_errors(exc)
    return {'status': 'error', 'answer': '\n\n'.join(message for _, message in errors),
            'sources': [], 'error_codes': list(dict.fromkeys(code for code, _ in errors))}
