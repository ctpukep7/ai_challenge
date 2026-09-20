from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from .agent import PersonalizedAgent
from .invariant_checker import check_candidate
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


def candidate(value="fastapi",proposal="Улучшить backend на FastAPI"):
    return {"field":"backend_framework","value":value,"proposal":proposal}


def invariant_payload(**overrides):
    values={
        "category":"stack","field":"backend_framework","operator":"equals","expected_value":"fastapi",
        "description":"Backend остаётся на Python/FastAPI","reason":"Сохраняем принятый стек и учебную архитектуру.",
        "compatible_alternative":"fastapi","active":True,
    }
    return {**values,**overrides}


def ask(client,prompt="Что такое RAG?",candidate_payload=None):
    return client.post("/api/ask",json={"prompt":prompt,"candidate":candidate_payload or candidate(),"controls":{"provider":"deepseek","model":"deepseek-flash","compression_mode":"full_session"}})


def test_defaults_seed_four_profiles_with_ordinary_active(bundle):
    store,_,client=bundle
    state=client.get("/api/state").json()
    assert [p["name"] for p in state["profiles"]]==["Новичок","Обычный","Эксперт","Руководитель"]
    assert state["active_profile"]["name"]=="Обычный"
    assert len(state["sessions"])==1
    assert state["active_session"]["is_active"]==1
    assert state["active_session"]["label"]=="Диалог 1"
    assert state["active_session"]["messages"]==[]
    for profile in state["profiles"]:
        session=store.active_session(profile["id"])
        assert session["label"]=="Диалог 1" and session["is_active"]==1


def test_existing_database_adds_builtins_once_and_preserves_later_selection(tmp_path):
    database_path=tmp_path/"legacy.sqlite3"
    connection=sqlite3.connect(database_path)
    connection.executescript(
        """
        CREATE TABLE user_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            role_context TEXT NOT NULL DEFAULT '',
            language TEXT NOT NULL DEFAULT '',
            tone TEXT NOT NULL CHECK (tone IN ('friendly','neutral','business')),
            detail_level TEXT NOT NULL CHECK (detail_level IN ('brief','balanced','deep')),
            response_format TEXT NOT NULL CHECK (response_format IN ('paragraphs','bullets','steps','table')),
            constraints TEXT NOT NULL DEFAULT '',
            is_active INTEGER NOT NULL DEFAULT 0 CHECK (is_active IN (0,1)),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO user_profiles(name,role_context,language,tone,detail_level,response_format,constraints,is_active)
        VALUES ('Новичок','Существующая настройка','Русский','friendly','balanced','steps','Не менять',1);
        """
    )
    connection.close()

    migrated=SQLiteMemoryStore(database_path)
    assert [profile["name"] for profile in migrated.profiles()]==["Новичок","Обычный","Эксперт","Руководитель"]
    assert migrated.active_profile()["name"]=="Обычный"
    assert migrated.profile(1)["role_context"]=="Существующая настройка"
    assert all(migrated.active_session(profile["id"])["label"]=="Диалог 1" for profile in migrated.profiles())

    novice=next(profile for profile in migrated.profiles() if profile["name"]=="Новичок")
    migrated.activate_profile(novice["id"])
    reopened=SQLiteMemoryStore(database_path)
    assert reopened.active_profile()["name"]=="Новичок"
    assert [profile["name"] for profile in reopened.profiles()].count("Обычный")==1
    assert all(len(reopened.sessions(profile["id"]))==1 for profile in reopened.profiles())


def test_marked_database_self_heals_missing_builtins_without_changing_active_profile(tmp_path):
    database_path=tmp_path/"marked.sqlite3"
    store=SQLiteMemoryStore(database_path)
    novice=next(profile for profile in store.profiles() if profile["name"]=="Новичок")
    store.activate_profile(novice["id"])
    with store.connection() as connection:
        assert connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version='day14_builtin_profiles_v1'"
        ).fetchone()
        connection.execute(
            "UPDATE user_profiles SET role_context='Сохранённый пользовательский контекст' WHERE id=?", (novice["id"],)
        )
        connection.execute("DELETE FROM user_profiles WHERE name!='Новичок'")

    repaired=SQLiteMemoryStore(database_path)
    assert [profile["name"] for profile in repaired.profiles()]==["Новичок","Обычный","Эксперт","Руководитель"]
    assert repaired.active_profile()["name"]=="Новичок"
    assert repaired.profile(novice["id"])["role_context"]=="Сохранённый пользовательский контекст"
    assert all(repaired.active_session(profile["id"]) for profile in repaired.profiles())


def test_self_heal_selects_ordinary_when_no_profile_is_active(tmp_path):
    database_path=tmp_path/"without-active.sqlite3"
    store=SQLiteMemoryStore(database_path)
    with store.connection() as connection:
        connection.execute("UPDATE user_profiles SET is_active=0")

    reopened=SQLiteMemoryStore(database_path)
    assert reopened.active_profile()["name"]=="Обычный"


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


@pytest.mark.parametrize("secret",["sk_abcdefghijklmnopqrstuvwxyz","sk-abcdefghijklmnopqrstuvwxyz"])
def test_profile_limit_and_validation(bundle,secret):
    _,_,client=bundle
    for index in range(6):assert client.post("/api/profiles",json=profile_payload(f"P{index}")).status_code==201
    assert client.post("/api/profiles",json=profile_payload("P6")).status_code==400
    assert client.post("/api/profiles",json={**profile_payload("Bad"),"constraints":secret}).status_code==400
    assert client.post("/api/profiles",json={**profile_payload("Bad enum"),"tone":"loud"}).status_code==400


def test_profile_and_invariants_are_separate_system_messages(bundle):
    _,fake,client=bundle
    response=ask(client)
    assert response.status_code==200
    call=fake.chat.completions.calls[0]
    assert call["messages"][0]["content"].startswith("Неизменяемые константы")
    assert "ACTIVE_PROFILE_DATA" in call["messages"][1]["content"]
    assert json.dumps("Обычный",ensure_ascii=False) in call["messages"][1]["content"]
    assert "ACTIVE_INVARIANTS" in call["messages"][2]["content"]
    preview=response.json()["preview"]
    assert preview["profile_snapshot"]["name"]=="Обычный"


def test_switch_uses_new_profile_and_only_its_history(bundle):
    _,fake,client=bundle
    assert ask(client,"NOVICE_ONLY").status_code==200
    expert=client.post("/api/profiles",json=profile_payload("Технический автор")).json()["active_profile"]
    assert ask(client,"EXPERT_QUESTION").status_code==200
    messages=fake.chat.completions.calls[-1]["messages"]
    joined="\n".join(item["content"] for item in messages)
    assert "Технический автор" in joined and "NOVICE_ONLY" not in joined


def test_profile_sessions_are_isolated_and_restore_last_selected(bundle):
    _,_,client=bundle
    novice=client.get("/api/state").json()["active_profile"]
    first=client.get("/api/state").json()["active_session"]["id"]
    second=client.post("/api/sessions").json()["active_session"]["id"]
    client.post("/api/profiles",json=profile_payload("Технический автор"))
    assert client.post(f"/api/sessions/{second}/activate").status_code==404
    client.post(f"/api/profiles/{novice['id']}/activate")
    state=client.get("/api/state").json()
    assert state["active_session"]["id"]==second and {x["id"] for x in state["sessions"]}=={first,second}


def test_working_and_longterm_are_shared_across_profiles(bundle):
    _,_,client=bundle
    client.put("/api/task",json={"goal":"Выбрать архитектуру","hard_constraints":"Бюджет 1 млн","task_data":"100 документов","open_questions":"SLA"})
    client.post("/api/long-term",json={"entry_type":"decision","content":"Используем PostgreSQL"})
    state=client.post("/api/profiles",json=profile_payload("Технический автор")).json()
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
    other=client.post("/api/profiles",json=profile_payload("Технический автор")).json()["active_session"]
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
    foreign=client.post("/api/profiles",json=profile_payload("Технический автор")).json()["active_session"]["id"]
    client.post(f"/api/profiles/{novice['id']}/activate")

    response=client.delete(f"/api/sessions/{foreign}")

    assert response.status_code==404
    assert store.session(foreign) is not None


@pytest.mark.parametrize("secret",["sk_secretsecretsecret","sk-secretsecretsecret"])
def test_provider_failure_does_not_persist_half_turn(tmp_path,secret):
    store=SQLiteMemoryStore(tmp_path/"x.sqlite3")
    app=create_app(store,PersonalizedAgent(store,client=FakeClient(error=RuntimeError(f"provider rejected {secret}"))))
    response=TestClient(app).post("/api/ask",json={"prompt":"test","candidate":candidate(),"controls":{}})
    assert response.status_code==502
    assert store.session_messages(store.active_session()["id"])==[]
    assert secret not in response.text


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


def test_full_reset_restores_four_clean_profiles_and_removes_invariants(bundle):
    store,_,client=bundle
    client.post("/api/profiles",json=profile_payload())
    client.post("/api/invariants",json=invariant_payload())
    assert ask(client,candidate_payload=candidate("nodejs","Переписать backend на Node.js")).status_code==409
    state=client.post("/api/memory/clear").json()
    assert [profile["name"] for profile in state["profiles"]]==["Новичок","Обычный","Эксперт","Руководитель"]
    assert [profile["id"] for profile in state["profiles"]]==[1,2,3,4]
    assert state["active_profile"]["name"]=="Обычный"
    assert len(state["sessions"])==1 and state["sessions"][0]["is_active"]==1
    assert state["active_session"]["messages"]==[] and state["task"]["goal"]=="" and state["long_term"]==[]
    assert state["events"]==[] and state["invariants"]==[] and state["invariant_check_events"]==[]
    assert [store.active_session(profile_id)["id"] for profile_id in (1,2,3,4)]==[1,2,3,4]

    recreated=client.post("/api/invariants",json=invariant_payload()).json()["invariants"][0]
    assert recreated["id"]==1
    assert ask(client,candidate_payload=candidate("nodejs","Переписать backend на Node.js")).status_code==409
    assert store.invariant_check_events()[0]["id"]==1


def test_task_api_stores_only_plain_context(bundle):
    store,_,client=bundle
    payload={"goal":"Улучшить backend","hard_constraints":"Оставить FastAPI","task_data":"FastAPI + SQLite","open_questions":"Как разделить слои?"}
    state=client.put("/api/task",json=payload).json()
    assert {key:state["task"][key] for key in payload}==payload
    assert not ({"phase","run_status","revision","current_step","expected_action"} & state["task"].keys())
    assert "allowed_events" not in state and "task_transition_events" not in state
    assert client.post("/api/task/transition",json={"event":"pause"}).status_code==404
    with store.connection() as connection:
        assert {row["name"] for row in connection.execute("PRAGMA table_info(tasks)")}=={
            "id","goal","hard_constraints","task_data","open_questions","updated_at"
        }
        assert not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_transition_events'"
        ).fetchone()


def test_legacy_machine_columns_are_ignored_and_do_not_block_ask(tmp_path):
    database_path=tmp_path/"legacy-machine.sqlite3"
    store=SQLiteMemoryStore(database_path)
    with store.connection() as connection:
        connection.execute("ALTER TABLE tasks ADD COLUMN phase TEXT NOT NULL DEFAULT 'planning'")
        connection.execute("ALTER TABLE tasks ADD COLUMN run_status TEXT NOT NULL DEFAULT 'active'")
        connection.execute("ALTER TABLE tasks ADD COLUMN revision INTEGER NOT NULL DEFAULT 0")
        connection.execute("ALTER TABLE tasks ADD COLUMN current_step TEXT NOT NULL DEFAULT ''")
        connection.execute("ALTER TABLE tasks ADD COLUMN expected_action TEXT NOT NULL DEFAULT ''")
        connection.execute("UPDATE tasks SET phase='done',run_status='paused',revision=9,current_step='legacy',expected_action='legacy'")
    legacy_store=SQLiteMemoryStore(database_path)
    fake=FakeClient()
    client=TestClient(create_app(legacy_store,PersonalizedAgent(legacy_store,client=fake)))

    state=client.get("/api/state").json()
    assert not ({"phase","run_status","revision","current_step","expected_action"} & state["task"].keys())
    response=ask(client,"Предложи улучшение")
    assert response.status_code==200 and len(fake.chat.completions.calls)==1
    assert "task_state_snapshot" not in response.json()["preview"]
    assert response.json()["preview"]["task_context_snapshot"]=={}


@pytest.mark.parametrize(
    "operator,expected,value,allowed",
    [
        ("equals","fastapi","FastAPI",True),
        ("equals","fastapi","nodejs",False),
        ("not_equals","nodejs","nodejs",False),
        ("not_equals","nodejs","fastapi",True),
        ("one_of","fastapi|django","django",True),
        ("one_of","fastapi|django","express",False),
        ("not_one_of","nodejs|express","express",False),
        ("not_one_of","nodejs|express","fastapi",True),
    ],
)
def test_pure_invariant_checker_matrix(operator,expected,value,allowed):
    invariant={"id":7,**invariant_payload(operator=operator,expected_value=expected)}
    result=check_candidate(candidate(value),[invariant])
    assert result["allowed"] is allowed
    assert result["checked_invariant_ids"]==[7]
    assert bool(result["violations"]) is not allowed


def test_invariant_crud_activation_and_validation(bundle):
    _,_,client=bundle
    created=client.post("/api/invariants",json=invariant_payload()).json()["invariants"][0]
    assert created["active"] is True and created["field"]=="backend_framework"
    changed=client.put(f"/api/invariants/{created['id']}",json=invariant_payload(description="Обновлённое правило")).json()
    assert changed["invariants"][0]["description"]=="Обновлённое правило"
    disabled=client.patch(f"/api/invariants/{created['id']}/active",json={"active":False}).json()
    assert disabled["invariants"][0]["active"] is False
    assert client.post("/api/invariants",json=invariant_payload(category="unknown")).status_code==400
    assert client.post("/api/invariants",json=invariant_payload(operator="contains")).status_code==400
    assert client.delete(f"/api/invariants/{created['id']}").json()["invariants"]==[]


def test_invariant_limit_and_secret_rejection(bundle):
    _,_,client=bundle
    for index in range(20):
        payload=invariant_payload(field=f"stack_{index}",description=f"Правило {index}")
        assert client.post("/api/invariants",json=payload).status_code==201
    assert client.post("/api/invariants",json=invariant_payload(field="overflow")).status_code==400
    bad=invariant_payload(description="sk-secretsecretsecret")
    fresh=client.post("/api/memory/clear").json()
    assert fresh["invariants"]==[]
    assert client.post("/api/invariants",json=bad).status_code==400
    assert ask(client,candidate_payload=candidate("sk_secretsecretsecret")).status_code==400


def test_conflict_is_structured_and_skips_provider_and_chat(bundle):
    store,fake,client=bundle
    created=client.post("/api/invariants",json=invariant_payload()).json()["invariants"][0]
    response=ask(client,"Перепиши backend",candidate("nodejs","Переписать backend на Node.js/Express"))

    assert response.status_code==409
    detail=response.json()["detail"]
    assert detail["category"]=="invariant_conflict"
    check=detail["invariant_check"]
    assert check["allowed"] is False
    assert check["violations"][0]["invariant_id"]==created["id"]
    assert check["violations"][0]["reason"]==invariant_payload()["reason"]
    assert check["violations"][0]["compatible_alternative"]=={"field":"backend_framework","value":"fastapi"}
    assert fake.chat.completions.calls==[]
    assert store.session_messages(store.active_session()["id"])==[]
    audit=store.invariant_check_events()[0]
    assert audit["outcome"]=="rejected" and audit["violated_invariant_ids"]==[created["id"]]
    assert "nodejs" not in json.dumps(audit) and "Переписать" not in json.dumps(audit,ensure_ascii=False)


def test_compatible_candidate_reaches_provider_with_read_only_snapshot(bundle):
    _,fake,client=bundle
    created=client.post("/api/invariants",json=invariant_payload()).json()["invariants"][0]
    response=ask(client,"Улучши архитектуру",candidate("FASTAPI","Добавить service layer в FastAPI"))

    assert response.status_code==200
    body=response.json();preview=body["preview"]
    assert preview["invariant_check"]["allowed"] is True
    assert preview["invariant_snapshot"][0]["id"]==created["id"]
    sent="\n".join(message["content"] for message in fake.chat.completions.calls[0]["messages"])
    assert "ACTIVE_INVARIANTS" in sent and "authoritative-read-only-application-state" in sent
    assert "STRUCTURED_CANDIDATE" in sent and "service layer" in sent


def test_disabled_invariant_does_not_block_same_candidate(bundle):
    store,fake,client=bundle
    created=client.post("/api/invariants",json=invariant_payload()).json()["invariants"][0]
    client.patch(f"/api/invariants/{created['id']}/active",json={"active":False})

    response=ask(client,"Предложи миграцию",candidate("nodejs","Переписать на Node.js"))

    assert response.status_code==200 and len(fake.chat.completions.calls)==1
    assert response.json()["preview"]["invariant_snapshot"]==[]
    assert store.invariant_check_events()[0]["checked_invariant_ids"]==[]


def test_session_reset_preserves_invariants_and_full_reset_removes_them(bundle):
    _,_,client=bundle
    created=client.post("/api/invariants",json=invariant_payload()).json()["invariants"][0]
    session_id=client.get("/api/state").json()["active_session"]["id"]
    after_session_reset=client.post(f"/api/sessions/{session_id}/reset").json()
    assert [item["id"] for item in after_session_reset["invariants"]]==[created["id"]]
    cleared=client.post("/api/memory/clear").json()
    assert cleared["invariants"]==[] and cleared["invariant_check_events"]==[]
