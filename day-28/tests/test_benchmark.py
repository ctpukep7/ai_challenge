import importlib.util
import json
from pathlib import Path


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parents[1] / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_export_only_known_fixture_questions_and_answer_fields(tmp_path):
    module = load_script("export_baseline")
    fixtures, archive, output = tmp_path / "questions.json", tmp_path / "archive.json", tmp_path / "result.json"
    fixtures.write_text(json.dumps([{"id": "q01", "question": "Контрольный вопрос"}]))
    archive.write_text(json.dumps({"results": [
        {"question_id": "q01", "question": "Контрольный вопрос", "result": {"answer": "Ответ", "status": "answered", "private_extra": "not exported"}},
        {"question_id": "personal", "question": "Личная беседа", "result": {"answer": "Не экспортировать", "status": "answered"}},
        {"question_id": "q01", "question": "Изменённый вопрос", "result": {"answer": "Не экспортировать", "status": "answered"}},
    ]}))
    assert module.export_baseline(archive, output, fixtures) == 1
    baseline = json.loads(output.read_text())
    assert baseline["source"] == "Day24" and baseline["historical"] is True
    assert baseline["rows"] == [{"question": "Контрольный вопрос", "answer": "Ответ", "status": "answered", "model": "DeepSeek (архив)", "metrics": None}]
    assert "private_extra" not in output.read_text() and "Личная беседа" not in output.read_text()


def test_missing_archive_is_graceful_and_does_not_write(tmp_path):
    module = load_script("export_baseline")
    output = tmp_path / "result.json"
    assert module.export_baseline(tmp_path / "missing", output) == 0
    assert not output.exists()


def test_alias_evaluation_is_explicitly_heuristic():
    module = load_script("benchmark")
    heuristic = module.alias_heuristic({"expected": [{"aliases": ["SQLite"]}, {"aliases": ["история", "контекст"]}]}, "История не хранится в SQLITE")
    assert heuristic["hit_count"] == 2
    assert heuristic["semantic_verification"] is False


def test_compare_ids_preserve_exact_sequence_without_extra_calls(tmp_path, monkeypatch):
    module = load_script("benchmark")
    (tmp_path / "questions.json").write_text(json.dumps([
        {"id": "q01", "question": "Первый"}, {"id": "q03", "question": "Повторяемый", "expected": []}]))
    monkeypatch.setattr(module, "HERE", tmp_path)
    monkeypatch.setattr("sys.argv", ["benchmark.py", "--compare", "--ids", "q03,q01,q03", "--output", str(tmp_path / "data/report.json")])
    calls = []
    def fake_call(path, body=None):
        if path == "/api/state":
            return {"status": "ready"}
        calls.append((path, body["question"]))
        return {"status": "answered", "answer": "Ответ", "cloud": {"called": True, "status": "error", "answer": "Ошибка"}}
    monkeypatch.setattr(module, "local_call", fake_call)
    module.run()
    assert calls == [("/api/compare", "Повторяемый"), ("/api/compare", "Первый"), ("/api/compare", "Повторяемый")]
    report = json.loads((tmp_path / "data/report.json").read_text())
    assert report["cloud_calls"] == 3 and len(report["rows"]) == 3
    assert all(row["cloud_heuristic"] is None for row in report["rows"])
