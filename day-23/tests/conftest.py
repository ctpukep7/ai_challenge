import json
import re
import sys
import time
from pathlib import Path

import pytest

# pytest invoked from repository root must still import this day's own backend.
DAY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DAY))


class FakeEmbedder:
    model_name = "test-model"
    dimension = 3
    max_tokens = 126
    fastembed_version = None

    def __init__(self, cache):
        self.cache = cache

    def offsets(self, text):
        return [(match.start(), match.end()) for match in re.finditer(r"\S+", text)]

    def count(self, text):
        return len(self.offsets(text))

    def embed(self, texts):
        return [[float(len(text) % 11 + 1), float(text.lower().count("архитектура") + 1), 1.0]
                for text in texts]


@pytest.fixture
def corpus(tmp_path):
    root = tmp_path / "corpus"
    root.mkdir()
    import subprocess
    (root / "README.md").write_text("# Project\n\nАвторский тестовый пример: архитектура и код, зависимости и расширение.\n## Главная\nДокументация проекта.")
    day = root / "day-16"
    day.mkdir()
    (day / "README.md").write_text("# День 16\n\nАвторский тестовый пример: архитектура и код, зависимости и расширение.\n## Локальный MCP server\nMCP stdio протокол инструментов.")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "README.md", "day-16/README.md"], check=True)
    return root


@pytest.fixture
def db(tmp_path, corpus):
    from backend.day21_adapter import build_project_index
    path = tmp_path / "index.sqlite"
    build_project_index(path, corpus, tmp_path / "cache", min_words=0, embedder_factory=FakeEmbedder)
    return path


def completed(client, identifier):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = client.get(f"/api/runs/{identifier}").json()
        if result["status"] not in {"queued", "running"}:
            return result
        time.sleep(0.005)
    raise AssertionError("Job did not finish")
