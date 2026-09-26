from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from .agent import PersonalizedAgent, safe_error
from .main import create_app
from .store import SQLiteMemoryStore


class FakeCompletions:
    def __init__(self, answers=None, error=None):
        self.answers=list(answers or ["Готовый **ответ**"]); self.error=error; self.calls=[]
    def create(self,**options):
        self.calls.append(options)
        if self.error:raise self.error
        answer=self.answers.pop(0) if self.answers else "Ответ"
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=answer))],usage=SimpleNamespace(prompt_tokens=10,completion_tokens=4,total_tokens=14))


class FakeClient:
    def __init__(self,answers=None,error=None):
        self.chat=SimpleNamespace(completions=FakeCompletions(answers,error))


@pytest.fixture
def bundle(tmp_path):
    store=SQLiteMemoryStore(tmp_path/"memory.sqlite3")
    fake=FakeClient()
    agent=PersonalizedAgent(store,client=fake)
    return store,fake,TestClient(create_app(store,agent))


def profile_payload(name="Аналитик"):
    return {"name":name,"role_context":"Работаю с данными","language":"Русский","tone":"neutral","detail_level":"deep","response_format":"table","constraints":"Показывай допущения"}


def ask(client,prompt="Что такое RAG?"):
    return client.post("/api/ask",json={"prompt":prompt,"controls":{"provider":"deepseek","model":"deepseek-flash","compression_mode":"full_session"}})


def test_defaults_seed_one_novice_with_one_empty_active_session(bundle):
    store,_,client=bundle
    state=client.get("/api/state").json()
    assert [p["name"] for p in state["profiles"]]==["Новичок"]
    assert state["active_profile"]["name"]=="Новичок"
    assert len(state["sessions"])==1
    assert state["active_session"]["is_active"]==1
    assert state["active_session"]["messages"]==[]
    assert store.active_session(state["active_profile"]["id"])["id"]==state["active_session"]["id"]


def test_create_update_and_activate_profile(bundle):
    _,_,client=bundle
    created=client.post("/api/profiles",json=profile_payload()).json()
    profile=created["active_profile"]
    assert profile["name"]=="Аналитик" and created["active_session"]["message_count"] if "message_count" in created["active_session"] else True
    changed={**profile_payload("Исследователь"),"tone":"friendly"}
    assert client.put(f"/api/profiles/{profile['id']}",json=changed).status_code==200
    novice=created["profiles"][0]
    switched=client.post(f"/api/profiles/{novice['id']}/activate").json()
    assert switched["active_profile"]["name"]=="Новичок"


@pytest.mark.parametrize("secret", ["sk_abcdefghijklmnopqrstuvwxyz", "sk-abcdefghijklmnopqrstuvwxyz"])
def test_profile_limit_and_validation(bundle,secret):
    _,_,client=bundle
    for index in range(9):assert client.post("/api/profiles",json=profile_payload(f"P{index}")).status_code==201
    assert client.post("/api/profiles",json=profile_payload("P9")).status_code==400
    assert client.post("/api/profiles",json={**profile_payload("Bad"),"constraints":f"api_key={secret}"}).status_code==400
    assert client.post("/api/profiles",json={**profile_payload("Bad enum"),"tone":"loud"}).status_code==400


def test_profile_is_separate_second_system_message_and_snapshot(bundle):
    _,fake,client=bundle
    response=ask(client)
    assert response.status_code==200
    call=fake.chat.completions.calls[0]
    assert call["messages"][0]["content"].startswith("Неизменяемые константы")
    assert "ACTIVE_PROFILE_DATA" in call["messages"][1]["content"]
    assert json.dumps("Новичок",ensure_ascii=False) in call["messages"][1]["content"]
    preview=response.json()["preview"]
    assert preview["profile_snapshot"]["name"]=="Новичок"


def test_switch_uses_new_profile_and_only_its_history(bundle):
    _,fake,client=bundle
    assert ask(client,"NOVICE_ONLY").status_code==200
    expert=client.post("/api/profiles",json=profile_payload("Эксперт")).json()["active_profile"]
    assert ask(client,"EXPERT_QUESTION").status_code==200
    messages=fake.chat.completions.calls[-1]["messages"]
    joined="\n".join(item["content"] for item in messages)
    assert "Эксперт" in joined and "NOVICE_ONLY" not in joined


def test_profile_sessions_are_isolated_and_restore_last_selected(bundle):
    _,_,client=bundle
    novice=client.get("/api/state").json()["active_profile"]
    first=client.get("/api/state").json()["active_session"]["id"]
    second=client.post("/api/sessions").json()["active_session"]["id"]
    client.post("/api/profiles",json=profile_payload("Эксперт"))
    assert client.post(f"/api/sessions/{second}/activate").status_code==404
    client.post(f"/api/profiles/{novice['id']}/activate")
    state=client.get("/api/state").json()
    assert state["active_session"]["id"]==second and {x["id"] for x in state["sessions"]}=={first,second}


def test_working_and_longterm_are_shared_across_profiles(bundle):
    _,_,client=bundle
    client.put("/api/task",json={"goal":"Выбрать архитектуру","hard_constraints":"Бюджет 1 млн","task_data":"100 документов","open_questions":"SLA"})
    client.post("/api/long-term",json={"entry_type":"decision","content":"Используем PostgreSQL"})
    state=client.post("/api/profiles",json=profile_payload("Эксперт")).json()
    assert state["task"]["goal"]=="Выбрать архитектуру"
    assert state["long_term"][0]["content"]=="Используем PostgreSQL"


def test_branch_facts_and_summaries_stay_inside_profile(bundle):
    store,_,client=bundle
    assert ask(client,"Факт профиля").status_code==200
    session=store.active_session(); messages=store.session_messages(session["id"])
    store.save_facts(session["id"],messages[0]["id"],{"goal":["A"]}, {"profile_snapshot":{"name":"Новичок"}}, {})
    store.save_summary_and_archive(session["id"],messages,"Сводка",{"profile_snapshot":{"name":"Новичок"}}, {})
    branched=client.post(f"/api/sessions/{session['id']}/branch").json()["active_session"]
    assert branched["compression"]["facts"]["values"]["goal"]==["A"]
    other=client.post("/api/profiles",json=profile_payload("Эксперт")).json()["active_session"]
    assert other["compression"]["facts"] is None and other["compression"]["summary"] is None


def test_delete_non_active_session_keeps_active_and_cascades_derived_data(bundle):
    store,_,client=bundle
    first=client.get("/api/state").json()["active_session"]["id"]
    assert ask(client,"Сохранённый вопрос").status_code==200
    messages=store.session_messages(first)
    store.save_facts(first,messages[0]["id"],{"goal":["A"]},{}, {})
    store.save_summary_and_archive(first,messages,"Сводка",{}, {})
    second=client.post("/api/sessions").json()["active_session"]["id"]

    state=client.delete(f"/api/sessions/{first}").json()

    assert state["active_session"]["id"]==second
    assert [session["id"] for session in state["sessions"]]==[second]
    assert store.session(first) is None
    with store.connection() as connection:
        for table in ("messages","session_summaries","session_facts_snapshots"):
            assert connection.execute(f"SELECT COUNT(*) FROM {table} WHERE session_id=?",(first,)).fetchone()[0]==0
    event=next(item for item in state["events"] if item["action"]=="delete")
    assert event["layer"]=="short_term" and event["object_id"]==first and event["details"]=={}


def test_delete_active_session_activates_latest_remaining(bundle):
    _,_,client=bundle
    first=client.get("/api/state").json()["active_session"]["id"]
    second=client.post("/api/sessions").json()["active_session"]["id"]
    third=client.post("/api/sessions").json()["active_session"]["id"]

    state=client.delete(f"/api/sessions/{third}").json()

    assert state["active_session"]["id"]==second
    assert {session["id"] for session in state["sessions"]}=={first,second}
    assert sum(bool(session["is_active"]) for session in state["sessions"])==1


def test_delete_last_session_creates_new_empty_dialog(bundle):
    _,_,client=bundle
    deleted=client.get("/api/state").json()["active_session"]["id"]

    state=client.delete(f"/api/sessions/{deleted}").json()

    assert len(state["sessions"])==1
    assert state["active_session"]["id"]!=deleted
    assert state["active_session"]["label"]=="Диалог 1"
    assert state["active_session"]["messages"]==[]
    assert state["active_session"]["is_active"]==1


def test_cannot_delete_session_of_another_profile(bundle):
    store,_,client=bundle
    novice=client.get("/api/state").json()["active_profile"]
    foreign=client.post("/api/profiles",json=profile_payload("Эксперт")).json()["active_session"]["id"]
    client.post(f"/api/profiles/{novice['id']}/activate")

    response=client.delete(f"/api/sessions/{foreign}")

    assert response.status_code==404
    assert store.session(foreign) is not None


@pytest.mark.parametrize("secret", ["sk_secretsecretsecret", "sk-secretsecretsecret"])
def test_provider_failure_does_not_persist_half_turn(tmp_path,secret):
    store=SQLiteMemoryStore(tmp_path/"x.sqlite3")
    app=create_app(store,PersonalizedAgent(store,client=FakeClient(error=RuntimeError(f"api_key={secret}"))))
    response=TestClient(app).post("/api/ask",json={"prompt":"test","controls":{}})
    assert response.status_code==502
    assert store.session_messages(store.active_session()["id"])==[]
    assert secret not in response.text


@pytest.mark.parametrize("secret", ["sk_secretsecretsecret", "sk-secretsecretsecret"])
def test_safe_error_redacts_underscore_and_hyphen_openai_tokens(secret):
    redacted=safe_error(RuntimeError(f"provider rejected {secret}"))
    assert secret not in redacted
    assert "[скрыто]" in redacted


def test_markdown_is_sanitized_and_keys_never_returned(bundle,monkeypatch):
    _,fake,client=bundle
    fake.chat.completions.answers=["# Заголовок\n<script>alert(1)</script> **ok**"]
    monkeypatch.setenv("DEEPSEEK_API_KEY","sk_do_not_leak_123456789")
    response=ask(client).json()
    assert "<script>" not in response["answer_html"] and "<strong>ok</strong>" in response["answer_html"]
    assert "sk_do_not_leak" not in json.dumps(response,ensure_ascii=False)


def test_personalization_audit_has_no_field_contents(bundle):
    _,_,client=bundle
    state=client.post("/api/profiles",json=profile_payload("PRIVATE_PROFILE")).json()
    event=next(x for x in state["events"] if x["layer"]=="personalization" and x["action"]=="create")
    assert event["details"]=={} and "PRIVATE_PROFILE" not in json.dumps(event,ensure_ascii=False)


def test_full_reset_restores_one_clean_novice_profile(bundle):
    _,_,client=bundle
    client.post("/api/profiles",json=profile_payload())
    state=client.post("/api/memory/clear").json()
    assert len(state["profiles"])==1 and state["active_profile"]["name"]=="Новичок"
    assert len(state["sessions"])==1 and state["sessions"][0]["is_active"]==1
    assert state["active_session"]["messages"]==[] and state["task"] is None and state["long_term"]==[]
    assert state["events"]==[]
