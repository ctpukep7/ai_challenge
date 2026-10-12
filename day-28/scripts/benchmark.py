"""Sequential local HTTP benchmark; alias hits are a heuristic, not truth."""
import argparse
import datetime
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
BASE_URL = "http://127.0.0.1:5027"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def local_call(path, body=None):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    request = urllib.request.Request(BASE_URL + path, data=None if body is None else json.dumps(body, ensure_ascii=False).encode(),
                                     headers={"Content-Type": "application/json"}, method="GET" if body is None else "POST")
    with opener.open(request, timeout=125) as response:
        data = response.read(2 * 1024 * 1024 + 1)
        if len(data) > 2 * 1024 * 1024:
            raise ValueError("Слишком большой ответ API")
        result = json.loads(data)
        if not isinstance(result, dict):
            raise ValueError("Некорректный JSON API")
        return result


def alias_heuristic(question, answer):
    text = " ".join(answer.casefold().split())
    expected = question.get("expected", [])
    hits = [any(" ".join(alias.casefold().split()) in text for alias in fact["aliases"]) for fact in expected]
    return {"kind": "expected_alias_substrings", "semantic_verification": False,
            "hit_count": sum(hits), "fact_count": len(hits), "hits": hits}


def run():
    parser = argparse.ArgumentParser(description="Day28: 10 вопросов, отказ и три повтора q03. Только локальный API.")
    parser.add_argument("--output", type=Path, help="Сохранить JSON только внутри day-28/data (исключено из Git).")
    parser.add_argument("--compare", action="store_true", help="Явно отправить вопросы и README-фрагменты также в DeepSeek.")
    parser.add_argument("--ids", help="Последовательность ID через запятую, с повторами: q01,q03,q06,q08,q03,q03. Без дополнительных запросов.")
    args = parser.parse_args()
    destination = None
    if args.output:
        destination = args.output.resolve()
        if not destination.is_relative_to((HERE / "data").resolve()):
            parser.error("--output должен находиться внутри day-28/data")
    fixtures = json.loads((HERE / "questions.json").read_text())
    queries = [*fixtures, {"id": "outside", "question": "Как в дне 99 настроен полёт космического корабля?", "unanswerable": True}]
    repeated = next(row for row in fixtures if row["id"] == "q03")
    queries += [{**repeated, "id": f"q03-repeat-{number}"} for number in range(1, 4)]
    if args.ids is not None:
        by_id = {row["id"]: row for row in fixtures}
        identifiers = [part.strip() for part in args.ids.split(",")]
        if any(identifier not in by_id for identifier in identifiers):
            parser.error("--ids должен содержать только ID из questions.json")
        queries = [by_id[identifier] for identifier in identifiers]
    report = {"profile": "day28-local-rag-v1", "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "mode": "compare" if args.compare else "local", "cloud_calls": 0,
              "heuristic_note": "Совпадение подстрок aliases не доказывает фактическую корректность.", "rows": []}
    try:
        report["state"] = local_call("/api/state")
    except (OSError, ValueError, urllib.error.URLError):
        report["state"] = {"status": "unavailable"}
    for fixture in queries:
        started = time.perf_counter()
        row = {"id": fixture["id"], "question": fixture["question"]}
        try:
            result = local_call("/api/compare" if args.compare else "/api/ask", {"question": fixture["question"]})
            row.update(result=result, heuristic=alias_heuristic(fixture, result.get("answer", "")) if result.get("status") != "error" else None)
            cloud = result.get("cloud")
            if args.compare and isinstance(cloud, dict):
                report["cloud_calls"] += int(cloud.get("called") is True)
                row["cloud_heuristic"] = alias_heuristic(fixture, cloud.get("answer", "")) if cloud.get("status") != "error" else None
        except urllib.error.HTTPError as exc:
            try:
                error = json.loads(exc.read(65536)).get("error", {})
                row["error"] = {"status": exc.code, "code": error.get("code", "http_error"), "message": error.get("message", "Ошибка локального API")}
            except (ValueError, AttributeError):
                row["error"] = {"status": exc.code, "code": "http_error", "message": "Ошибка локального API"}
        except (OSError, ValueError, urllib.error.URLError):
            row["error"] = {"code": "transport_error", "message": "Локальный API недоступен, ответ некорректен или истёк таймаут."}
        row["wall_seconds"] = time.perf_counter() - started
        report["rows"].append(row)
        print(f'{row["id"]}: {row.get("result", {}).get("status", "error")} · {row["wall_seconds"]:.2f} с', flush=True)
    if destination:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(f"Результаты сохранены: {destination}")


if __name__ == "__main__":
    run()
