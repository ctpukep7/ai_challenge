"""Явный foreground запуск, без управления процессами Ollama."""
import argparse
import ipaddress
from pathlib import Path
import socket
import sys
import uvicorn
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.app import Config, create_app

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', required=True, help='Адрес этого Mac в домашней сети')
    args = parser.parse_args()
    host = str(ipaddress.ip_address(args.host))
    config = Config.from_file()
    data = ROOT / 'data'
    cert, key = data / 'tls-cert.pem', data / 'tls-key.pem'
    if not cert.is_file() or not key.is_file() or not (data / 'tls-host').is_file():
        raise RuntimeError('Сначала выполните npm run setup -- --host <IP Mac>.')
    if (data / 'tls-host').read_text().strip() != host:
        raise RuntimeError('Сертификат создан для другого IP. Повторите setup с текущим --host.')
    if key.stat().st_mode & 0o077:
        raise RuntimeError('TLS-ключ должен иметь права 0600.')
    family = socket.AF_INET6 if ':' in host else socket.AF_INET
    with socket.socket(family) as check:
        try:
            check.bind((host, 5029))
        except OSError:
            raise RuntimeError('Адрес недоступен или порт 5029 занят.') from None
    print(f'Приватный чат: https://{host}:5029 (Ctrl+C — остановка)', flush=True)
    uvicorn.run(create_app(config), host=host, port=5029, workers=1, proxy_headers=False,
                access_log=False, ssl_keyfile=str(key), ssl_certfile=str(cert))

if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
