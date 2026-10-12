"""Ручная HTTPS-проверка с другого устройства, только стандартная библиотека."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import ssl
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPSHandler, HTTPRedirectHandler, ProxyHandler, Request, build_opener
ROOT = Path(__file__).resolve().parents[1]

class SafeClientError(Exception):
    pass

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

def validate_url(url):
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise SafeClientError('Некорректный адрес сервиса.') from None
    if parts.scheme != 'https' or not parts.hostname or parts.username is not None or parts.password is not None or parts.query or parts.fragment or parts.path not in ('', '/'):
        raise SafeClientError('Нужен HTTPS-адрес без пути, токена, параметров и фрагмента.')
    if any(char.isspace() or ord(char)<32 for char in url) or '?' in url or '#' in url or port == 0:
        raise SafeClientError('Некорректный адрес сервиса.')
    return url.rstrip('/')

def request(url, cert, token_file, command, prompt=None, *, opener_factory=build_opener, context_factory=ssl.create_default_context):
    base = validate_url(url)
    try:
        token = Path(token_file).read_text().strip()
    except (OSError, UnicodeError):
        raise SafeClientError('Не удалось прочитать локальный файл токена.') from None
    if len(token) != 64 or any(char not in '0123456789abcdef' for char in token):
        raise SafeClientError('Некорректный локальный токен доступа.')
    try:
        context = context_factory(cafile=str(cert))
        opener = opener_factory(ProxyHandler({}), HTTPSHandler(context=context), NoRedirect())
        if command == 'health':
            endpoint, payload = '/api/health', None
        elif command == 'ask':
            if not isinstance(prompt,str) or not prompt.strip():
                raise SafeClientError('Введите непустой вопрос.')
            endpoint = '/api/chat'
            payload = json.dumps({'prompt':prompt,'history':[]},ensure_ascii=True).encode()
        else:
            raise SafeClientError('Неизвестная команда клиента.')
        req = Request(base + endpoint, data=payload, headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'}, method='POST' if payload else 'GET')
        with opener.open(req, timeout=125) as response:
            # Reject unexpected oversized replies without displaying their contents.
            raw = response.read(1024*1024+1)
            if len(raw)>1024*1024:
                raise SafeClientError('Ответ сервиса превышает допустимый размер.')
            result = json.loads(raw)
            if not isinstance(result,dict):
                raise SafeClientError('Сервис вернул некорректный JSON.')
            return result
    except HTTPError as exc:
        messages={401:'Токен не принят.',409:'Модель занята.',413:'Контекст слишком большой.',422:'Некорректный запрос.',429:'Достигнут лимит запросов.',502:'Ошибка локальной модели.',503:'Сервис или модель недоступны.',504:'Модель не ответила вовремя.'}
        raise SafeClientError(messages.get(exc.code,'HTTP-ошибка сервиса.')) from None
    except (OSError, URLError, ssl.SSLError, ValueError, UnicodeError, RecursionError):
        raise SafeClientError('Не удалось выполнить HTTPS-запрос. Проверьте сеть, сертификат и адрес.') from None

def main(argv=None):
    parser=argparse.ArgumentParser(description='Проверка приватного AI-сервиса с доверенным сертификатом')
    parser.add_argument('--url',default='https://192.168.0.171:5029')
    parser.add_argument('--cert',type=Path,default=ROOT/'data'/'tls-cert.pem')
    parser.add_argument('--token-file',type=Path,default=ROOT/'data'/'access-token')
    commands=parser.add_subparsers(dest='command',required=True)
    commands.add_parser('health')
    ask=commands.add_parser('ask')
    ask.add_argument('prompt')
    args=parser.parse_args(argv)
    try:
        result=request(args.url,args.cert,args.token_file,args.command,getattr(args,'prompt',None))
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 0
    except SafeClientError as exc:
        print(str(exc),file=sys.stderr)
        return 1

if __name__=='__main__':
    sys.exit(main())
