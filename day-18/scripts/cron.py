"""Show by default. Installation is an explicit operator action only."""
import argparse
from pathlib import Path
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
MARKER = "# AI_CHALLENGE_DAY18_" + __import__('hashlib').sha256(str(ROOT).encode()).hexdigest()[:12]


def cron_line():
    python = shlex.quote(str(ROOT / ".venv" / "bin" / "python"))
    directory = shlex.quote(str(ROOT))
    # cron treats '%' specially even within shell quotes.
    command = f"cd {directory} && {python} -m backend.runner --run-due --quiet > /dev/null 2>&1"
    return "* * * * * " + command.replace("%", "\\%") + " " + MARKER


def current_crontab():
    try:
        result = subprocess.run(["crontab","-l"],capture_output=True,text=True,timeout=5)
    except (OSError,subprocess.TimeoutExpired):
        return None
    if result.returncode == 0:
        return result.stdout
    if "no crontab" in result.stderr.lower():
        return ""
    return None


def status():
    current = current_crontab()
    return {"installed":None if current is None else cron_line() in current.splitlines(),"cron_line":cron_line()}


def rewrite(current,install):
    lines = [line for line in current.splitlines() if not line.endswith(" " + MARKER)]
    if install:
        lines.append(cron_line())
    return "\n".join(lines) + ("\n" if lines else "")


def main():
    parser = argparse.ArgumentParser(description="Системный cron Дня 18; по умолчанию только показать команду")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--show",action="store_true")
    group.add_argument("--status",action="store_true")
    group.add_argument("--install",action="store_true")
    group.add_argument("--remove",action="store_true")
    args = parser.parse_args()
    if args.status:
        print(__import__('json').dumps(status(),ensure_ascii=False,indent=2))
    elif args.install or args.remove:
        current = current_crontab()
        if current is None:
            sys.exit("Не удалось прочитать crontab; установка отменена, существующие записи не изменены")
        if args.install and not (ROOT / ".venv" / "bin" / "python").is_file():
            sys.exit("Сначала создайте day-18/.venv")
        if args.install:
            (ROOT / "data").mkdir(exist_ok=True)
        try:
            result = subprocess.run(["crontab","-"],input=rewrite(current,args.install),text=True,capture_output=True,timeout=10)
        except (OSError,subprocess.TimeoutExpired):
            sys.exit("Не удалось обновить crontab")
        if result.returncode:
            sys.exit("Системный cron отклонил запись")
        print("Cron установлен" if args.install else "Запись Дня 18 удалена")
    else:
        print(cron_line())


if __name__ == "__main__":
    main()
