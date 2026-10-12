import asyncio
import copy
import json

import httpx
import pytest

from backend.app import AskRequest
from backend.profiles import BASELINE, LIMITS, OPTIMIZED, OPTIMIZED_SYSTEM, profile
from backend.settings import Settings
from backend.local_llm import NATIVE_STOPS, payload
from test_app import chats, client_for, post, response, transport


FULL = {"temperature": 1.2, "num_predict": 1536, "num_ctx": 6144, "top_k": 0, "top_p": .95, "min_p": .05,
        "repeat_penalty": 1.15, "repeat_last_n": 0, "seed": 123, "stop": [" END ", "\nSTOP"], "system": "Ответь по-русски и объясни ограничения.", "presence_penalty": 1.25, "format": "json"}


def test_complete_payload_system_role_baseline_frozen_and_full_snapshot():
    calls = []
    before = copy.deepcopy(BASELINE)
    result = post({"question": "Напиши пример", "settings": FULL}, provider=transport(calls=calls)).json()
    assert result["settings"] == FULL
    assert result["baseline"]["parameters"] == {**before, "presence_penalty": 0, "format": None, "keep_alive": "1m"}
    assert result["baseline"]["system"] == ""
    assert result["optimized"]["system"] == FULL["system"]
    assert result["optimized"]["parameters"] == {**{k: v for k, v in FULL.items() if k != "system"}, "keep_alive": "1m"}
    payloads = chats(calls)
    assert payloads[0]["options"] == payloads[1]["options"] == {**before, "presence_penalty": 0, "stop": list(NATIVE_STOPS)}
    expected = {k: v for k, v in FULL.items() if k not in ("system", "format")}
    expected["stop"] = [*NATIVE_STOPS, *FULL["stop"]]
    assert payloads[2]["options"] == payloads[3]["options"] == expected
    assert payloads[3]["messages"] == [{"role": "system", "content": FULL["system"]}, {"role": "user", "content": "Напиши пример"}]
    assert [row["think"] for row in payloads] == [False, False, False, False]
    assert "think" not in payloads[3]["options"]
    assert "system" not in payloads[3]["options"] and payloads[3]["keep_alive"] == "1m"
    assert result["optimized"]["context_warning"] and "RAM" in result["optimized"]["context_warning"]
    assert BASELINE == before


def test_partial_legacy_settings_complete_recommended_defaults_and_empty_system():
    legacy = {"temperature": .3, "num_predict": 512, "num_ctx": 4096}
    result = post({"question": "Вопрос", "settings": legacy}).json()
    assert result["settings"] == {**OPTIMIZED, "presence_penalty": 0, "format": None, **legacy, "system": OPTIMIZED_SYSTEM}
    calls = []
    empty = post({"question": "Вопрос", "settings": {"system": ""}}, provider=transport(calls=calls), route="ask").json()
    assert empty["optimized"]["system"] == ""
    assert chats(calls)[1]["messages"] == [{"role": "user", "content": "Вопрос"}]


def test_deep_stop_snapshots_and_default_factories():
    source = {"stop": ["STOP"]}
    snapshot = profile("optimized", source)
    source["stop"].append("LATER")
    assert snapshot["parameters"]["stop"] == ["STOP"]
    snapshot["parameters"]["stop"].append("ANOTHER")
    assert OPTIMIZED["stop"] == [] and BASELINE["stop"] == []
    a, b = Settings(), Settings()
    a.stop.append("ONE")
    assert b.stop == []
    c, d = AskRequest(question="A"), AskRequest(question="B")
    c.settings.stop.append("TWO")
    assert d.settings.stop == []
    async def run():
        async with client_for() as (client, _):
            old = (await client.post("/api/ask", json={"question": "A", "settings": {"stop": ["STOP"]}})).json()
            await client.post("/api/ask", json={"question": "B", "settings": {"stop": ["NEXT"]}})
            assert old["settings"]["stop"] == old["optimized"]["parameters"]["stop"] == ["STOP"]
            state = (await client.get("/api/state")).json()
            assert state["profiles"]["baseline"]["parameters"]["stop"] == []
    asyncio.run(run())


@pytest.mark.parametrize("settings", [
    {"presence_penalty": -.01}, {"presence_penalty": 2.01}, {"presence_penalty": "1.5"}, {"presence_penalty": True}, {"presence_penalty": None}, {"presence_penalty": float("nan")}, {"presence_penalty": float("inf")}, {"presence_penalty": float("-inf")},
    {"temperature": 2.0001}, {"top_k": -1}, {"top_k": 101}, {"top_k": 20.0}, {"top_k": True}, {"top_k": "20"},
    {"top_p": -0.01}, {"top_p": 1.01}, {"top_p": "0.8"}, {"top_p": False}, {"top_p": float("inf")},
    {"min_p": -1}, {"min_p": 2}, {"min_p": float("nan")}, {"min_p": True},
    {"repeat_penalty": .79}, {"repeat_penalty": 1.51}, {"repeat_penalty": "1.0"}, {"repeat_penalty": True},
    {"repeat_last_n": -1}, {"repeat_last_n": -2}, {"repeat_last_n": 2049}, {"repeat_last_n": 64.0}, {"repeat_last_n": False},
    {"seed": -2}, {"seed": 2147483648}, {"seed": 10**400}, {"seed": 2.0}, {"seed": "1"}, {"seed": True},
    {"num_predict": 257}, {"num_ctx": 16384}, {"stop": "STOP"}, {"stop": [1]}, {"stop": [True]},
    {"stop": [""]}, {"stop": ["A", "A"]}, {"stop": ["A", "B", "C", "D", "E"]},
    {"stop": ["я" * 81]}, {"stop": ["😀" * 65]}, {"system": None}, {"system": False},
    {"system": "a" * 3001}, {"system": "€" * 2667}, {"keep_alive": "10m"}, {"think": "true"},
])
def test_bad_advanced_settings_rejected_before_model(settings):
    calls = []
    async def run():
        async with client_for(transport(calls=calls)) as (client, _):
            result = await client.post("/api/ask", content=json.dumps({"question": "Вопрос", "settings": settings}).encode(), headers={"Content-Type": "application/json"})
            assert result.status_code == 422 and result.json()["error"]["code"] == "invalid_request"
    asyncio.run(run())
    assert calls == []


@pytest.mark.parametrize("settings", [
    {"presence_penalty": 0, "temperature": 0, "top_k": 0, "top_p": 0, "min_p": 0, "repeat_penalty": .8, "repeat_last_n": 0, "seed": -1, "num_predict": 256, "num_ctx": 3072},
    {"presence_penalty": 2, "temperature": 2, "top_k": 100, "top_p": 1, "min_p": 1, "repeat_penalty": 1.5, "repeat_last_n": 2048, "seed": 2147483647, "num_predict": 1536, "num_ctx": 8192},
    {"stop": [" A ", " B ", " C ", " D "]}, {"stop": ["я" * 80]}, {"stop": ["😀" * 64]},
    {"system": "a" * 3000}, {"system": "€" * 2666}, {"system": ""},
])
def test_advanced_boundaries_and_stop_whitespace_preserved(settings):
    calls = []
    data = post({"question": "Вопрос", "settings": settings}, provider=transport(calls=calls), route="ask").json()
    assert data["optimized"]["status"] == "answered"
    for key, value in settings.items():
        assert data["settings"][key] == value
    if "stop" in settings:
        assert chats(calls)[1]["options"]["stop"] == list(dict.fromkeys([*NATIVE_STOPS, *settings["stop"]]))


@pytest.mark.parametrize("field", ["system", "stop"])
def test_invalid_unicode_in_settings(field):
    async def run():
        async with client_for() as (client, _):
            value = '\\ud800' if field == "system" else '["\\ud800"]'
            content = ('{"question":"Q","settings":{"' + field + '":' + ('"' + value + '"' if field == "system" else value) + '}}').encode()
            result = await client.post("/api/ask", content=content, headers={"Content-Type": "application/json"})
            assert result.status_code == 422
    asyncio.run(run())


def test_state_full_limits_and_large_context_two_warnings():
    async def run():
        async with client_for(transport(chat=lambda _: httpx.Response(200, json=response(prompt_eval_count=5000)))) as (client, _):
            state = (await client.get("/api/state")).json()
            assert state["tuning_limits"] == LIMITS
            assert state["profiles"]["baseline"]["parameters"] == {**BASELINE, "presence_penalty": 0, "format": None, "keep_alive": "1m"}
            result = (await client.post("/api/ask", json={"question": "Вопрос", "settings": {"num_ctx": 6144, "num_predict": 2048}})).json()
            assert "RAM" in result["optimized"]["context_warning"] and "запас" in result["optimized"]["context_warning"]
    asyncio.run(run())


def test_native_stops_always_preserved_and_user_extras_deduplicated_without_mutation():
    empty = copy.deepcopy(BASELINE)
    assert payload(empty, [])["options"]["stop"] == list(NATIVE_STOPS)
    assert empty["stop"] == []
    custom = {**copy.deepcopy(OPTIMIZED), "stop": [" END ", NATIVE_STOPS[0]]}
    assert payload(custom, [])["options"]["stop"] == [*NATIVE_STOPS, " END "]
    assert custom["stop"] == [" END ", NATIVE_STOPS[0]]
