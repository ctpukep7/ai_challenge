import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("day29_benchmark", ROOT / "scripts/benchmark.py")
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)


def test_fixed_loopback_no_proxy_redirect_and_timeout(monkeypatch):
    captured = {}
    class Open:
        def open(self, request, timeout):
            captured.update(url=request.full_url, body=json.loads(request.data), timeout=timeout)
            return self
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read(self, count): return b'{"ok":true}'
    def opener(*handlers):
        captured["handlers"] = handlers
        return Open()
    monkeypatch.setattr(benchmark.urllib.request, "build_opener", opener)
    assert benchmark.local_call("/api/compare", {"question": "Привет"}) == {"ok": True}
    assert captured["url"] == "http://127.0.0.1:5028/api/compare"
    assert captured["timeout"] == 245
    assert captured["handlers"][0].proxies == {}
    assert isinstance(captured["handlers"][1], benchmark.NoRedirect)
    assert benchmark.NoRedirect().redirect_request(None, None, 302, "", {}, "https://cloud.example") is None


def test_ab_ba_and_no_expected_sent_to_api(monkeypatch):
    requests = []
    def fake(path, body):
        requests.append((path, body))
        return {"baseline": {"status": "answered", "answer": "SQLite"}, "optimized": {"status": "answered", "answer": "SQLite"}}
    monkeypatch.setattr(benchmark, "local_call", fake)
    fixture = {"id": "q01", "question": "Вопрос", "expected": [{"aliases": ["SQLite"]}]}
    a, b = benchmark.run_fixture(fixture, 0), benchmark.run_fixture(fixture, 1, {"temperature": .1})
    assert a["first_profile"] == "baseline" and b["first_profile"] == "optimized"
    assert requests == [("/api/compare", {"question": "Вопрос", "first_profile": "baseline"}),
                        ("/api/compare", {"question": "Вопрос", "first_profile": "optimized", "settings": {"temperature": .1}})]
    assert "heuristics" not in a and "expected" not in a


def test_report_warmup_separate_repeats_partial_save_and_host(monkeypatch, tmp_path):
    root = tmp_path / "day-29"
    root.mkdir()
    (root / "questions.json").write_text(json.dumps([{"id": "q01", "question": "Первый"}, {"id": "q03", "question": "Третий"}]))
    monkeypatch.setattr(benchmark, "HERE", root)
    monkeypatch.setattr(benchmark, "host_snapshot", lambda: {"note": "system"})
    monkeypatch.setattr(benchmark, "disk_snapshot", lambda: {"note": "disk"})
    requests = []
    def call(path, body=None):
        requests.append((path, body))
        if body is None: return {"status": "ready"}
        return {"baseline": {"status": "answered", "answer": "A"}, "optimized": {"status": "answered", "answer": "B"}}
    monkeypatch.setattr(benchmark, "local_call", call)
    output = root / "data/report.json"
    monkeypatch.setattr("sys.argv", ["benchmark.py", "--ids", "q03", "--repeats", "2", "--output", str(output)])
    benchmark.run()
    report = json.loads(output.read_text())
    assert len(report["rows"]) == 2 and report["warmup"]["id"] == "q01"
    assert report["profile"] == "day29-logic-only-v3"
    assert "task_profile" not in report
    assert [row["first_profile"] for row in report["rows"]] == ["baseline", "optimized"]
    assert report["cloud_calls"] == 0
    assert report["host_before"] == report["host_after"] == {"note": "system"}
    assert len(requests) == 4


@pytest.mark.parametrize("extra", [["--output", "/tmp/outside-day29.json"], ["--repeats", "0"], ["--ids", "unknown"], ["--temperature", "nan"], ["--task-profile", "unknown"]])
def test_invalid_benchmark_arguments_before_calls(monkeypatch, extra):
    monkeypatch.setattr("sys.argv", ["benchmark.py", *extra])
    monkeypatch.setattr(benchmark, "local_call", lambda *_: pytest.fail("must not call"))
    with pytest.raises(SystemExit) as exc:
        benchmark.run()
    assert exc.value.code == 2


def test_disk_actual_runtime_roots_and_shared_symlink_not_duplicated(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    for folder in ["day-26", "day-27", "day-28", "day-29", ".local-llm/runtime", ".local-llm/models"]:
        p = root / folder
        p.mkdir(parents=True)
        (p / "file").write_bytes(b"123")
    (root / "day-27/node_modules").mkdir()
    (root / "day-27/node_modules/pkg").write_bytes(b"12345")
    (root / "day-29/node_modules").symlink_to(root / "day-27/node_modules", target_is_directory=True)
    (root / ".local-llm/cli-deps").mkdir()
    (root / ".local-llm/cli-deps/pkg").write_bytes(b"1234567")
    (root / ".local-llm/npm-cache").mkdir()
    (root / ".local-llm/npm-cache/cache").write_bytes(b"12345678901")
    (root / ".local-llm/note").write_bytes(b"1234")
    # Internal links and an external directory must not be followed or counted twice.
    (root / ".local-llm/runtime-link").symlink_to(root / ".local-llm/runtime", target_is_directory=True)
    (root / ".local-llm/npm-cache/shared").symlink_to(root / "day-27/node_modules", target_is_directory=True)
    outside = tmp_path / "external"
    outside.mkdir()
    (outside / "large").write_bytes(b"x" * 100)
    (root / ".local-llm/external-link").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(benchmark, "HERE", root / "day-29")
    disk = benchmark.disk_snapshot()
    assert disk["bytes"]["ollama_runtime"] == 3 and disk["bytes"]["ollama_models"] == 3
    assert disk["bytes"]["day29"] == 3
    assert disk["bytes"]["ollama_other"] == 22
    assert disk["new_week_bytes"] == 45
    assert disk["budget_bytes"] == 8_000_000_000


def test_host_snapshot_no_command_args_or_secrets(monkeypatch):
    def output(command):
        if command[0] == "ps":
            return "123 200 /path/ollama\n124 300 /path/python3\n125 400 /path/browser\n126 3148 /private/project/.local-llm/runtime/llama-server\n127 99 /folder/llama/browser"
        return "system counters"
    monkeypatch.setattr(benchmark, "command_output", output)
    result = benchmark.host_snapshot()
    assert result["process_rss"] == [{"pid": 123, "rss_kib": 200, "executable": "ollama"}, {"pid": 124, "rss_kib": 300, "executable": "python3"},
                                      {"pid": 126, "rss_kib": 3148, "executable": "llama-server"}]
    assert "RSS процессов" in result["note"] and "не общий пик RAM" in result["note"]


def test_settings_file_full_options_and_cli_override_before_transmission(monkeypatch, tmp_path):
    root = tmp_path / "day-29"
    (root / "data").mkdir(parents=True)
    (root / "questions.json").write_text(json.dumps([{"id": "q01", "question": "Вопрос"}]))
    settings = {"system": "Кратко по-русски", "temperature": .1, "num_predict": 1536, "num_ctx": 6144,
                "top_k": 15, "top_p": .9, "min_p": .1, "repeat_penalty": 1.1, "repeat_last_n": 128,
                "seed": 42, "stop": [" END "], "presence_penalty": 1.5}
    path = root / "data/settings.json"
    path.write_text(json.dumps(settings))
    monkeypatch.setattr(benchmark, "HERE", root)
    monkeypatch.setattr(benchmark, "host_snapshot", lambda: {})
    monkeypatch.setattr(benchmark, "disk_snapshot", lambda: {})
    requests = []
    def call(route, body=None):
        requests.append((route, body))
        return {"baseline": {"status": "answered", "answer": "A"}, "optimized": {"status": "answered", "answer": "B"}}
    monkeypatch.setattr(benchmark, "local_call", call)
    monkeypatch.setattr("sys.argv", ["benchmark.py", "--ids", "q01", "--skip-warmup", "--settings-file", str(path), "--temperature", "1.3", "--num-ctx", "8192"])
    benchmark.run()
    assert requests[1][1]["settings"] == {**settings, "temperature": 1.3, "num_ctx": 8192}


@pytest.mark.parametrize("content", ["[]", "{", '{"system": true}', '{"top_k": 101}', '{"stop": ["A", "A"]}', '{"unknown": 1}', '{"presence_penalty": true}', '{"presence_penalty": "1.5"}', '{"presence_penalty": 2.1}', '{"presence_penalty": NaN}', '{"repeat_last_n": -1}'])
def test_invalid_settings_file_before_any_api_call(monkeypatch, tmp_path, content):
    root = tmp_path / "day-29"
    (root / "data").mkdir(parents=True)
    (root / "questions.json").write_text(json.dumps([{"id": "q01", "question": "Вопрос"}]))
    path = root / "data/settings.json"
    path.write_text(content)
    monkeypatch.setattr(benchmark, "HERE", root)
    monkeypatch.setattr(benchmark, "local_call", lambda *_: pytest.fail("must not call"))
    monkeypatch.setattr("sys.argv", ["benchmark.py", "--ids", "q01", "--settings-file", str(path)])
    with pytest.raises(SystemExit) as exc:
        benchmark.run()
    assert exc.value.code == 2


def test_settings_file_rejects_outside_data_and_symlink_escape(monkeypatch, tmp_path):
    root = tmp_path / "day-29"
    (root / "data").mkdir(parents=True)
    (root / "questions.json").write_text(json.dumps([{"id": "q01", "question": "Вопрос"}]))
    outside = tmp_path / "external.json"
    outside.write_text('{"seed": 1}')
    alias = root / "data/link.json"
    alias.symlink_to(outside)
    monkeypatch.setattr(benchmark, "HERE", root)
    monkeypatch.setattr(benchmark, "local_call", lambda *_: pytest.fail("must not call"))
    for path in (outside, alias):
        monkeypatch.setattr("sys.argv", ["benchmark.py", "--ids", "q01", "--settings-file", str(path)])
        with pytest.raises(SystemExit) as exc:
            benchmark.run()
        assert exc.value.code == 2





def test_pair_benchmark_defaults_to_entire_catalog_without_task_selection(monkeypatch, tmp_path):
    root=tmp_path / "day-29"
    root.mkdir()
    cases=[{"id":"arbitrary-b","label":"B","question":"Условие B"},{"id":"arbitrary-a","label":"A","question":"Условие A"}]
    (root / "questions.json").write_text(json.dumps(cases))
    monkeypatch.setattr(benchmark,"HERE",root)
    monkeypatch.setattr(benchmark,"host_snapshot",lambda:{})
    monkeypatch.setattr(benchmark,"disk_snapshot",lambda:{})
    calls=[]
    def fake(path,body=None):
        calls.append((path,body))
        if body is None:return {"status":"ready"}
        return {"baseline":{"status":"answered","answer":"A"},"optimized":{"status":"answered","answer":"B"}}
    monkeypatch.setattr(benchmark,"local_call",fake)
    monkeypatch.setattr("sys.argv",["benchmark.py"])
    benchmark.run()
    report=json.loads((root / "data/logic-benchmark.json").read_text())
    assert report["profile"]=="day29-logic-only-v3"
    assert report["warmup"]["id"]=="arbitrary-b"
    assert [row["id"] for row in report["rows"]]==["arbitrary-b","arbitrary-a"]
    assert [row["first_profile"] for row in report["rows"]]==["baseline","optimized"]
    assert all(set(body)=={"question","first_profile","settings"} for _,body in calls if body)
    assert "task_profile" not in report and all("task_profile" not in row for row in report["rows"])
