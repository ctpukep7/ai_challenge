"""Sequential loopback comparisons; all raw answers retained, no cloud calls."""
import argparse
import datetime
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
BASE_URL = "http://127.0.0.1:5028"
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
from backend.settings import Settings
from pydantic import ValidationError


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def local_call(path, body=None):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    request = urllib.request.Request(BASE_URL + path, data=None if body is None else json.dumps(body, ensure_ascii=False).encode(),
                                     headers={"Content-Type": "application/json"}, method="GET" if body is None else "POST")
    with opener.open(request, timeout=245) as response:
        data = response.read(2 * 1024 * 1024 + 1)
        if len(data) > 2 * 1024 * 1024:
            raise ValueError("Слишком большой ответ API")
        result = json.loads(data)
        if not isinstance(result, dict):
            raise ValueError("Некорректный JSON API")
        return result


def command_output(command):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=5, check=False)
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def host_snapshot():
    """System counters and process RSS: never attribute them to one model."""
    process_rows = []
    raw = command_output(["ps", "-axo", "pid=,rss=,comm="])
    if raw:
        for line in raw.splitlines():
            values = line.strip().split(None, 2)
            if len(values) == 3 and any(name in Path(values[2]).name.casefold() for name in ("ollama", "python", "llama")):
                try:
                    process_rows.append({"pid": int(values[0]), "rss_kib": int(values[1]), "executable": Path(values[2]).name})
                except ValueError:
                    pass
    return {"created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "memory_pressure": command_output(["memory_pressure", "-Q"]),
            "vm_stat": command_output(["vm_stat"]), "swap": command_output(["sysctl", "vm.swapusage"]),
            "process_rss": process_rows,
            "note": "Общесистемные показатели и RSS процессов. /api/ps описывает размещение runner, не общий пик RAM. Накопленный swap не приписывается прогону."}


def disk_snapshot():
    def files_size(root, excluded=()):
        if not root.is_dir() or root.is_symlink():
            return None
        size, pending = 0, [root]
        while pending:
            directory = pending.pop()
            try:
                for item in directory.iterdir():
                    try:
                        if item.is_symlink() or (directory == root and item.name in excluded):
                            continue
                        if item.is_dir():
                            pending.append(item)
                        elif item.is_file():
                            size += item.stat().st_size
                    except OSError:
                        pass
            except OSError:
                pass
        return size

    paths = {"day29": HERE, "day26": HERE.parent / "day-26", "day27": HERE.parent / "day-27",
             "day28": HERE.parent / "day-28", "ollama_runtime": HERE.parent / ".local-llm/runtime",
             "ollama_models": HERE.parent / ".local-llm/models"}
    result = {name: files_size(root) for name, root in paths.items()}
    # Count caches/dependencies/root files exactly once, outside the two named subtrees.
    result["ollama_other"] = files_size(HERE.parent / ".local-llm", excluded={"runtime", "models"})
    return {"bytes": result, "new_week_bytes": sum(value for value in result.values() if value is not None), "budget_bytes": 8_000_000_000, "note": "Логические размеры файлов без следования symlink. ollama_other включает остальные файлы .local-llm, включая cli-deps и npm-cache, без повторного учёта runtime/models. Day29 не использует RAG-индекс, embeddings или кэш. Python runtime Day21 переиспользован без новых пакетов. Размер файлов не является расходом RAM."}


def run_fixture(fixture, index, settings=None):
    started = time.perf_counter()
    row = {"id": fixture["id"], "question": fixture["question"],
           "first_profile": "baseline" if index % 2 == 0 else "optimized"}
    body = {"question": fixture["question"], "first_profile": row["first_profile"]}
    if settings is not None:
        body["settings"] = settings
    try:
        result = local_call("/api/compare", body)
        row["result"] = result
    except urllib.error.HTTPError as exc:
        try:
            error = json.loads(exc.read(65536)).get("error", {})
            row["error"] = {"status": exc.code, "code": error.get("code", "http_error"), "message": error.get("message", "Ошибка локального API")}
        except (ValueError, AttributeError):
            row["error"] = {"status": exc.code, "code": "http_error", "message": "Ошибка локального API"}
    except (OSError, ValueError, urllib.error.URLError):
        row["error"] = {"code": "transport_error", "message": "Локальный API недоступен, ответ некорректен или истёк таймаут."}
    row["wall_seconds"] = time.perf_counter() - started
    return row


def run():
    parser = argparse.ArgumentParser(description="Day29: свежие сравнения исходной и настроенной модели AB/BA с отдельным warmup.")
    parser.add_argument("--output", type=Path, help="По умолчанию data/logic-benchmark.json.")
    parser.add_argument("--ids", help="ID задач через запятую; по умолчанию весь каталог. Повторы разрешены.")
    parser.add_argument("--repeats", type=int, default=1, help="Число повторов всей указанной последовательности.")
    parser.add_argument("--skip-warmup", action="store_true", help="Не делать отдельный подготовительный запрос.")
    parser.add_argument("--temperature", type=float)
    parser.add_argument("--num-predict", type=int, choices=[256, 512, 768, 1024, 1536, 2048])
    parser.add_argument("--num-ctx", type=int, choices=[3072, 4096, 6144, 8192])
    parser.add_argument("--settings-file", type=Path, help="JSON объект настроек внутри day-29/data; одиночные CLI параметры имеют приоритет.")
    args = parser.parse_args()
    destination = (args.output or HERE / "data/logic-benchmark.json").resolve()
    if not destination.is_relative_to((HERE / "data").resolve()):
        parser.error("--output должен находиться внутри day-29/data")
    if not 1 <= args.repeats <= 20:
        parser.error("--repeats: от 1 до 20")
    if args.temperature is not None and not 0 <= args.temperature <= 2:
        parser.error("--temperature: конечное число от 0 до 2")
    fixtures = json.loads((HERE / "questions.json").read_text())
    by_id = {row["id"]: row for row in fixtures}
    if not fixtures:
        parser.error("В questions.json пока нет подготовленных задач")
    identifiers = [part.strip() for part in args.ids.split(",")] if args.ids is not None else list(by_id)
    if any(identifier not in by_id for identifier in identifiers):
        parser.error("--ids должен содержать только ID из questions.json")
    settings = {}
    if args.settings_file:
        settings_path = args.settings_file.resolve()
        if not settings_path.is_relative_to((HERE / "data").resolve()):
            parser.error("--settings-file должен находиться внутри day-29/data")
        try:
            if settings_path.stat().st_size > 32768:
                raise ValueError("settings file too large")
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
            if not isinstance(settings, dict):
                raise ValueError("settings must be an object")
        except (OSError, ValueError, UnicodeError):
            parser.error("--settings-file: нужен корректный JSON объект настроек")
    settings.update({key: value for key, value in (("temperature", args.temperature), ("num_predict", args.num_predict), ("num_ctx", args.num_ctx)) if value is not None})
    try:
        Settings.model_validate(settings)
    except ValidationError:
        parser.error("Настройки не прошли проверку: проверьте диапазоны, системную инструкцию, стоп-строки и неизвестные поля")

    report = {"profile": "day29-logic-only-v3", "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "cloud_calls": 0,
              "quality_note": "Правильность ответа и обоснования оценивается отдельно по условиям задачи.",
              "host_before": host_snapshot(), "disk": disk_snapshot(), "warmup": None, "rows": []}
    try:
        report["state"] = local_call("/api/state")
    except (OSError, ValueError, urllib.error.URLError):
        report["state"] = {"status": "unavailable"}
    if not args.skip_warmup:
        report["warmup"] = run_fixture(fixtures[0], 0, settings)
        print("Отдельный warmup завершён; не включён в rows.", flush=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    for index, identifier in enumerate(identifiers * args.repeats):
        row = run_fixture(by_id[identifier], index, settings)
        report["rows"].append(row)
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        result = row.get("result", {})
        print(f'{identifier}: {result.get("baseline", {}).get("status", "error")} / {result.get("optimized", {}).get("status", "error")} · {row["wall_seconds"]:.2f} с', flush=True)
    report["host_after"] = host_snapshot()
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(f"Результаты сохранены: {destination}")


if __name__ == "__main__":
    run()
