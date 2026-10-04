import json

import httpx
import pytest

from backend import llm


def service(monkeypatch, handler):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-key")
    client = httpx.Client(transport=httpx.MockTransport(handler), timeout=llm.TIMEOUT)
    pauses = []
    return llm.DeepSeekClient(client=client, sleep=pauses.append), pauses


def success(request):
    return httpx.Response(200, json={
        "choices": [{"message": {"content": "Три человека и телескоп [1]"}}],
        "usage": {"total_tokens": 12},
    })


def test_reusable_client_and_equal_generation_parameters(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        return success(request)

    api, pauses = service(monkeypatch, handler)
    try:
        messages = [{"role": "user", "content": "Что находится на базе?"}]
        assert api(messages)["answer"] == "Три человека и телескоп [1]"
        assert api(messages)["usage"] == {"total_tokens": 12}
        assert len(seen) == 2
        assert pauses == []
        body = json.loads(seen[0].content)
        assert body["messages"] == messages
        assert body["model"] == llm.MODEL
        assert body["temperature"] == 0
        assert body["thinking"] == {"type": "disabled"}
        assert body["max_tokens"] == 400
        assert seen[0].headers["Authorization"] == "Bearer test-only-key"
    finally:
        api.close()
    assert api.client.is_closed


@pytest.mark.parametrize("failure", [httpx.ConnectError, httpx.ConnectTimeout])
def test_connect_failure_retries_only_twice(monkeypatch, failure):
    attempts = []

    def handler(request):
        attempts.append(request)
        if len(attempts) < 3:
            raise failure("connect failed")
        return success(request)

    api, pauses = service(monkeypatch, handler)
    try:
        assert api([{"role": "user", "content": "Что находится на базе?"}])["answer"]
    finally:
        api.close()
    assert len(attempts) == 3
    assert pauses == list(llm.RETRY_DELAYS)


def test_429_and_503_retry_with_bounded_pauses(monkeypatch):
    statuses = iter([429, 503, 200])
    attempts = []

    def handler(request):
        attempts.append(request)
        code = next(statuses)
        return success(request) if code == 200 else httpx.Response(code)

    api, pauses = service(monkeypatch, handler)
    try:
        assert api([{"role": "user", "content": "Что находится на базе?"}])["answer"]
    finally:
        api.close()
    assert len(attempts) == 3
    assert pauses == list(llm.RETRY_DELAYS)


def test_read_timeout_does_not_retry(monkeypatch):
    attempts = []

    def handler(request):
        attempts.append(request)
        raise httpx.ReadTimeout("read timed out")

    api, pauses = service(monkeypatch, handler)
    try:
        with pytest.raises(llm.LLMError, match="ожидания ответа"):
            api([{"role": "user", "content": "Что находится на базе?"}])
    finally:
        api.close()
    assert len(attempts) == 1
    assert pauses == []


def test_non_transient_http_status_does_not_retry(monkeypatch):
    attempts = []

    def handler(request):
        attempts.append(request)
        return httpx.Response(401)

    api, pauses = service(monkeypatch, handler)
    try:
        with pytest.raises(llm.LLMError, match="HTTP 401"):
            api([{"role": "user", "content": "Что находится на базе?"}])
    finally:
        api.close()
    assert len(attempts) == 1
    assert pauses == []
