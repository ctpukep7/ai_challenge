"""Export only the known historical fixture answers; never invoke a provider."""
import argparse
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
ARCHIVE = HERE.parent / "day-24/data/benchmark-live-reviewed.json"
NOTE = "Архивный прогон Day24; другой prompt и смысловой verifier. Время не сохранено; это не контролируемое A/B-сравнение моделей."


def export_baseline(archive=ARCHIVE, destination=HERE / "data/cloud-baseline.json", fixtures_path=HERE / "questions.json"):
    try:
        raw = Path(archive).read_bytes()
        data = json.loads(raw)
        fixtures = json.loads(Path(fixtures_path).read_text())
        allowed = {row["id"]: row["question"] for row in fixtures}
        rows, seen = [], set()
        for row in data["results"]:
            identifier, question = row.get("question_id"), row.get("question")
            if identifier not in allowed or question != allowed[identifier] or identifier in seen:
                continue
            result = row.get("result", {})
            if result.get("status") not in {"answered", "unknown"} or not isinstance(result.get("answer"), str):
                continue
            rows.append({"question": question, "answer": result["answer"], "status": result["status"],
                         "model": "DeepSeek (архив)", "metrics": None})
            seen.add(identifier)
        if not rows:
            return 0
        baseline = {"source": "Day24", "historical": True, "note": NOTE,
                    "archive_sha256": hashlib.sha256(raw).hexdigest(), "rows": rows}
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(baseline, ensure_ascii=False, indent=2) + "\n")
        return len(rows)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return 0


def run():
    parser = argparse.ArgumentParser(description="Экспорт сохранённых ответов Day24 без облачных запросов.")
    parser.add_argument("--output", type=Path, default=HERE / "data/cloud-baseline.json")
    args = parser.parse_args()
    destination = args.output.resolve()
    if not destination.is_relative_to((HERE / "data").resolve()):
        parser.error("--output должен находиться внутри day-28/data")
    count = export_baseline(destination=destination)
    print(f"Экспортировано ответов: {count}." if count else "Архив Day24 отсутствует или не содержит подходящих ответов. Локальный RAG работает без сравнения.")


if __name__ == "__main__":
    run()
