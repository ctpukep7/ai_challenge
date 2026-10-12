"""The pair-only contract preserves exact input and complete immutable native settings."""
import asyncio
import copy
import hashlib
import json
from pathlib import Path

import pytest

from backend import app as module
from backend.profiles import BASELINE, OPTIMIZED, OPTIMIZED_SYSTEM, OPTION_KEYS, profile
from test_app import chats, client_for, post, transport


def test_frozen_baseline_and_single_optimized_function():
    frozen = json.loads((Path(__file__).parent / "fixtures/baseline-frozen.json").read_text())
    assert BASELINE == frozen
    assert profile("baseline", {"temperature": 0, "presence_penalty": 2, "system": "Changed"}) == {
        "id": "baseline", "label": "Исходная модель", "system": "", "parameters": {**frozen, "presence_penalty": 0, "format": None, "keep_alive": "1m"}}
    assert profile("optimized")["label"] == "Настроенная модель"
    assert set(profile("optimized")["parameters"]) == {*OPTION_KEYS, "format", "keep_alive"}
    with pytest.raises(ValueError): profile("editor")
    with pytest.raises(ValueError): profile("logic")


@pytest.mark.parametrize("extra", [
    {"task_profile": "logic"}, {"task_profile": "editor"}, {"task_profiles": {}},
    {"default_task_profile": "creative"}, {"settings": {"task_profile": "logic"}},
])
def test_removed_task_selection_rejected_before_native_call(extra):
    calls = []
    result = post({"question": "Логический вопрос", **extra}, provider=transport(calls=calls))
    assert result.status_code == 422 and calls == []


@pytest.mark.parametrize("overrides", [{}, {"presence_penalty": 1.5}, {"system": ""}, {"temperature": .2, "stop": ["Конец"]}])
def test_one_editable_configuration_full_snapshot_and_same_input(overrides):
    calls = []
    question = "Условие: 2 > 1. Объясни вывод."
    result = post({"question": question, "settings": overrides}, provider=transport(calls=calls)).json()
    expected = {**OPTIMIZED, "format": None, **{key:value for key,value in overrides.items() if key != "system"}, "system": overrides.get("system", OPTIMIZED_SYSTEM)}
    assert result["settings"] == expected
    assert result["comparison"] == {"same_input": True, "input_sha256": hashlib.sha256(question.encode()).hexdigest()}
    assert not any(key.startswith("task_profile") for key in result)
    assert all(not any(key.startswith("task_profile") for key in result[identifier]) for identifier in ("baseline", "optimized"))
    generated = [payload for payload in chats(calls) if payload["messages"]]
    assert len(generated) == 2
    assert generated[0]["messages"] == [{"role":"user","content":question}]
    assert generated[1]["messages"][-1] == generated[0]["messages"][0]
    assert generated[1]["options"]["presence_penalty"] == expected["presence_penalty"]
    assert generated[0]["options"]["presence_penalty"] == 0


@pytest.mark.parametrize("catalog_content", [None, "{invalid JSON"])
def test_state_ignores_missing_or_invalid_catalog_and_does_not_generate(monkeypatch, tmp_path, catalog_content):
    if catalog_content is not None:
        (tmp_path / "questions.json").write_text(catalog_content)
    monkeypatch.setattr(module, "HERE", tmp_path)
    calls = []
    async def run():
        async with client_for(transport(calls=calls)) as (client, _):
            state = (await client.get("/api/state")).json()
            assert "questions" not in state
            assert set(state["profiles"]) == {"baseline","optimized"}
            assert all(key not in state for key in ("task_profiles","default_task_profile","index"))
            assert state["profiles"]["optimized"]["parameters"]["presence_penalty"] == 0
    asyncio.run(run())
    assert chats(calls) == []


def test_later_settings_and_refresh_do_not_mutate_saved_response_or_server_defaults():
    async def run():
        async with client_for() as (client, _):
            first = (await client.post("/api/ask", json={"question":"A","settings":{"system":"ONLY A","presence_penalty":1.5,"stop":["END"]}})).json()
            frozen = copy.deepcopy(first)
            second = (await client.post("/api/ask", json={"question":"B","settings":{"temperature":.25}})).json()
            state = (await client.get("/api/state")).json()
            assert second["settings"] == {**OPTIMIZED,"format":None,"temperature":.25,"system":OPTIMIZED_SYSTEM}
            assert state["profiles"]["optimized"] == profile("optimized")
            assert first == frozen
    asyncio.run(run())



def test_default_native_request_has_no_system_role_and_random_seed():
    calls = []
    result = post({"question":"Свободный вопрос"},provider=transport(calls=calls)).json()
    assert OPTIMIZED_SYSTEM == "" and OPTIMIZED["seed"] == -1
    assert result["settings"]["system"] == "" and result["settings"]["seed"] == -1
    payloads = chats(calls)
    assert len(payloads) == 4
    assert all(row["options"]["seed"] == -1 for row in payloads)
    assert payloads[0]["messages"] == payloads[2]["messages"] == []
    assert payloads[1]["messages"] == payloads[3]["messages"] == [{"role":"user","content":"Свободный вопрос"}]
    assert result["baseline"]["system"] == result["optimized"]["system"] == ""


def test_explicit_user_system_and_seed_still_delivered_without_changing_defaults():
    calls = []
    custom = {"system":"Следуй моей инструкции буквально.","seed":42}
    result = post({"question":"Мой вопрос","settings":custom},provider=transport(calls=calls)).json()
    payloads = chats(calls)
    assert payloads[2]["options"]["seed"] == payloads[3]["options"]["seed"] == 42
    assert payloads[3]["messages"] == [{"role":"system","content":custom["system"]},{"role":"user","content":"Мой вопрос"}]
    assert payloads[1]["messages"] == [{"role":"user","content":"Мой вопрос"}]
    assert payloads[0]["options"]["seed"] == payloads[1]["options"]["seed"] == -1
    assert result["settings"]["system"] == custom["system"] and result["settings"]["seed"] == 42
    assert profile("optimized")["system"] == "" and profile("optimized")["parameters"]["seed"] == -1
