"""Создаёт локальный секрет и самоподписанный TLS, не печатая секреты."""
import argparse
import ipaddress
import os
from pathlib import Path
import secrets
import subprocess
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.app import Config

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', required=True, help='IPv4/IPv6 адрес Mac в домашней сети')
    args = parser.parse_args()
    host = str(ipaddress.ip_address(args.host))
    data = ROOT / 'data'
    data.mkdir(mode=0o700, exist_ok=True)
    os.chmod(data, 0o700)
    token = data / 'access-token'
    if not token.exists():
        fd = os.open(token, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as file:
            file.write(secrets.token_hex(32) + '\n')
    Config.from_file(token)
    key, cert, host_file = data / 'tls-key.pem', data / 'tls-cert.pem', data / 'tls-host'
    if not (key.exists() and cert.exists() and host_file.exists() and host_file.read_text().strip() == host):
        # Temporary private file is created under a private directory and restrictive umask.
        temp_key, temp_cert = data / 'tls-key.pending', data / 'tls-cert.pending'
        old_umask = os.umask(0o077)
        try:
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '365',
                            '-keyout', str(temp_key), '-out', str(temp_cert), '-subj', '/CN=AI Challenge Day30',
                            '-addext', f'subjectAltName=DNS:localhost,IP:127.0.0.1,IP:{host}'],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            os.chmod(temp_key, 0o600)
            temp_key.replace(key)
            temp_cert.replace(cert)
            host_file.write_text(host + '\n')
        except subprocess.CalledProcessError:
            raise RuntimeError('Не удалось создать TLS-сертификат с openssl.') from None
        finally:
            os.umask(old_umask)
            temp_key.unlink(missing_ok=True)
            temp_cert.unlink(missing_ok=True)
    os.chmod(key, 0o600)
    print('Настройка завершена. Токен и TLS сохранены в локальной папке data; токен не выводится.')
    print(f'Запуск: npm start -- --host {host}')

if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
