from __future__ import annotations

import json
import sqlite3
from threading import Event, Thread
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from .agent import PersonalizedAgent
from .main import create_app
from .store import SQLiteMemoryStore
from .task_machine import MachineState, RunStatus, TaskEvent, TaskPhase, InvalidTransition, allowed_events, transition


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


def move(client,event,current_step=None,expected_action=None):
    payload={"event":event}
    if event in ("advance","approve","request_changes"):
        payload.update(
            next_current_step=current_step or f"Checkpoint после {event}",
            next_expected_action=expected_action or f"Действие после {event}",
        )
    return client.post("/api/task/transition",json=payload)


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
            "SELECT 1 FROM schema_migrations WHERE version='day13_builtin_profiles_v1'"
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

    reopened=SQLiteMemoryStore(database_path)
    assert [profile["name"] for profile in reopened.profiles()]==["Новичок","Обычный","Эксперт","Руководитель"]
    assert reopened.active_profile()["name"]=="Новичок"
    assert all(len(reopened.sessions(profile["id"]))==1 for profile in reopened.profiles())


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


def test_profile_is_separate_second_system_message_and_snapshot(bundle):
    _,fake,client=bundle
    response=ask(client)
    assert response.status_code==200
    call=fake.chat.completions.calls[0]
    assert call["messages"][0]["content"].startswith("Неизменяемые константы")
    assert "ACTIVE_PROFILE_DATA" in call["messages"][1]["content"]
    assert json.dumps("Обычный",ensure_ascii=False) in call["messages"][1]["content"]
    assert "TASK_STATE_MACHINE" in call["messages"][2]["content"]
    preview=response.json()["preview"]
    assert preview["profile_snapshot"]["name"]=="Обычный"


@pytest.mark.parametrize("phase,phase_guidance",[
    ("execution","В execution при описании готовности говори только «готово к проверке» или «можно переходить в validation»"),
    ("validation","В validation при описании готовности говори только «готово к подтверждению завершения»"),
])
def test_composed_task_state_message_prevents_premature_completion(bundle,phase,phase_guidance):
    store,fake,_=bundle
    state=store.state()
    state["task"].update(
        phase=phase,
        current_step=f"Checkpoint {phase}",
        expected_action=f"Expected action {phase}",
    )
    agent=PersonalizedAgent(store,client=fake)

    messages=agent.build_messages("Можно считать задачу завершённой?",agent.controls_from({}),state)
    task_message=next(message for message in messages if "<TASK_STATE_MACHINE" in message["content"])

    assert task_message["role"]=="system"
    assert f'"phase": "{phase}"' in task_message["content"]
    assert "не меняй phase/run_status даже словесно" in task_message["content"]
    assert "Пока phase != done, не утверждай, что задача формально завершена, выполнена или закрыта" in task_message["content"]
    assert phase_guidance in task_message["content"]
    assert "Финальное завершение после перехода в done отображает UI" in task_message["content"]
    assert fake.chat.completions.calls==[]


def test_switch_uses_new_profile_and_only_its_history(bundle):
    _,fake,client=bundle
    assert ask(client,"NOVICE_ONLY").status_code==200
    expert=client.post("/api/profiles",json=profile_payload("Специалист")).json()["active_profile"]
    assert ask(client,"EXPERT_QUESTION").status_code==200
    messages=fake.chat.completions.calls[-1]["messages"]
    joined="\n".join(item["content"] for item in messages)
    assert "Специалист" in joined and "NOVICE_ONLY" not in joined


def test_profile_sessions_are_isolated_and_restore_last_selected(bundle):
    _,_,client=bundle
    novice=client.get("/api/state").json()["active_profile"]
    first=client.get("/api/state").json()["active_session"]["id"]
    second=client.post("/api/sessions").json()["active_session"]["id"]
    client.post("/api/profiles",json=profile_payload("Специалист"))
    assert client.post(f"/api/sessions/{second}/activate").status_code==404
    client.post(f"/api/profiles/{novice['id']}/activate")
    state=client.get("/api/state").json()
    assert state["active_session"]["id"]==second and {x["id"] for x in state["sessions"]}=={first,second}


def test_working_and_longterm_are_shared_across_profiles(bundle):
    _,_,client=bundle
    client.put("/api/task",json={"goal":"Выбрать архитектуру","hard_constraints":"Бюджет 1 млн","task_data":"100 документов","open_questions":"SLA"})
    client.post("/api/long-term",json={"entry_type":"decision","content":"Используем PostgreSQL"})
    state=client.post("/api/profiles",json=profile_payload("Специалист")).json()
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
    other=client.post("/api/profiles",json=profile_payload("Специалист")).json()["active_session"]
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
    foreign=client.post("/api/profiles",json=profile_payload("Специалист")).json()["active_session"]["id"]
    client.post(f"/api/profiles/{novice['id']}/activate")

    response=client.delete(f"/api/sessions/{foreign}")

    assert response.status_code==404
    assert store.session(foreign) is not None


@pytest.mark.parametrize("secret",["sk_secretsecretsecret","sk-secretsecretsecret"])
def test_provider_failure_does_not_persist_half_turn(tmp_path,secret):
    store=SQLiteMemoryStore(tmp_path/"x.sqlite3")
    app=create_app(store,PersonalizedAgent(store,client=FakeClient(error=RuntimeError(f"provider rejected {secret}"))))
    response=TestClient(app).post("/api/ask",json={"prompt":"test","controls":{}})
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


def test_full_reset_restores_four_clean_profiles_with_ordinary_active(bundle):
    store,_,client=bundle
    client.post("/api/profiles",json=profile_payload())
    state=client.post("/api/memory/clear").json()
    assert [profile["name"] for profile in state["profiles"]]==["Новичок","Обычный","Эксперт","Руководитель"]
    assert state["active_profile"]["name"]=="Обычный"
    assert len(state["sessions"])==1 and state["sessions"][0]["is_active"]==1
    assert all(store.active_session(profile["id"])["label"]=="Диалог 1" for profile in state["profiles"])
    assert state["active_session"]["messages"]==[] and state["task"]["phase"]=="planning" and state["long_term"]==[]
    assert state["task"]["run_status"]=="active" and state["task_transition_events"]==[]
    assert state["events"]==[]


def test_pure_task_machine_routes_and_rejects_invalid_events():
    planning=MachineState(TaskPhase.PLANNING,RunStatus.ACTIVE)
    execution=transition(planning,TaskEvent.ADVANCE)
    validation=transition(execution,TaskEvent.ADVANCE)
    assert transition(validation,TaskEvent.REQUEST_CHANGES).phase is TaskPhase.EXECUTION
    done=transition(validation,TaskEvent.APPROVE)
    assert done==MachineState(TaskPhase.DONE,RunStatus.ACTIVE)
    assert allowed_events(done)==[]
    with pytest.raises(InvalidTransition):transition(planning,TaskEvent.APPROVE)
    paused=transition(execution,TaskEvent.PAUSE)
    assert allowed_events(paused)==[TaskEvent.RESUME]
    assert transition(paused,TaskEvent.RESUME)==execution


def test_task_api_updates_checkpoint_without_direct_state_mutation(bundle):
    _,_,client=bundle
    payload={"goal":"Собрать демо","hard_constraints":"Без секретов","task_data":"Day 13","open_questions":"Нет","current_step":"Написать тесты","expected_action":"Запустить pytest","phase":"done","run_status":"paused"}
    state=client.put("/api/task",json=payload).json()
    assert state["task"]["current_step"]=="Написать тесты"
    assert state["task"]["expected_action"]=="Запустить pytest"
    assert state["task"]["phase"]=="planning" and state["task"]["run_status"]=="active"
    assert state["task"]["revision"]==1


def test_transition_api_and_invalid_transition_are_atomic(bundle):
    store,_,client=bundle
    state=move(client,"advance","Выполнить план","Зафиксировать результат").json()
    assert state["task"]["phase"]=="execution"
    assert state["task"]["current_step"]=="Выполнить план"
    assert state["task"]["expected_action"]=="Зафиксировать результат"
    assert state["task"]["revision"]==1
    assert state["task_transition_events"][0]["event"]=="advance"
    before=store.task_transition_events()
    response=move(client,"approve")
    assert response.status_code==409
    assert store.task()["phase"]=="execution" and store.task_transition_events()==before


@pytest.mark.parametrize("advance_count,phase",[(0,"planning"),(1,"execution"),(2,"validation")])
def test_pause_resume_preserves_checkpoint_and_messages(bundle,advance_count,phase):
    store,_,client=bundle
    for index in range(advance_count):move(client,"advance",f"Шаг {index}",f"Действие {index}")
    client.put("/api/task",json={"goal":"Продолжить работу","current_step":"Шаг X","expected_action":"Действие Y"})
    assert ask(client,"Контекст до паузы").status_code==200
    before_messages=store.session_messages(store.active_session()["id"])
    paused=client.post("/api/task/transition",json={"event":"pause"}).json()
    assert paused["task"]["phase"]==phase and paused["task"]["run_status"]=="paused"
    calls_before=len(bundle[1].chat.completions.calls)
    blocked=ask(client,"Не должен уйти провайдеру")
    assert blocked.status_code==409 and len(bundle[1].chat.completions.calls)==calls_before
    assert store.session_messages(store.active_session()["id"])==before_messages
    resumed=client.post("/api/task/transition",json={"event":"resume"}).json()
    assert resumed["task"]["phase"]==phase and resumed["task"]["current_step"]=="Шаг X"
    assert resumed["task"]["expected_action"]=="Действие Y"
    response=ask(client,"Продолжай")
    assert response.status_code==200
    preview=response.json()["preview"]
    assert preview["task_state_snapshot"]=={"phase":phase,"run_status":"active","current_step":store.task()["current_step"],"expected_action":store.task()["expected_action"],"revision":store.task()["revision"]}
    sent="\n".join(item["content"] for item in bundle[1].chat.completions.calls[-1]["messages"])
    assert "Контекст до паузы" in sent and "Продолжай" in sent and "Не должен уйти" not in sent


def test_done_is_terminal_and_blocks_ask(bundle):
    store,fake,client=bundle
    for event in ("advance","advance","approve"):
        assert move(client,event).status_code==200
    state=client.get("/api/state").json()
    assert state["task"]["phase"]=="done" and state["allowed_events"]==[]
    calls=len(fake.chat.completions.calls)
    assert ask(client).status_code==409
    assert len(fake.chat.completions.calls)==calls and store.session_messages(store.active_session()["id"])==[]


def test_phase_transition_requires_and_atomically_saves_next_checkpoint(bundle):
    store,_,client=bundle
    before=store.task().copy()
    missing=client.post("/api/task/transition",json={"event":"advance"})
    assert missing.status_code==409
    assert store.task()==before and store.task_transition_events()==[]

    changed=move(client,"advance","Начать реализацию","Собрать рабочий результат").json()["task"]
    assert (changed["phase"],changed["current_step"],changed["expected_action"])==(
        "execution","Начать реализацию","Собрать рабочий результат"
    )
    assert changed["revision"]==before["revision"]+1


@pytest.mark.parametrize("terminal",[False,True])
def test_task_edits_are_rejected_while_paused_or_done(bundle,terminal):
    store,_,client=bundle
    if terminal:
        for event in ("advance","advance","approve"):
            assert move(client,event).status_code==200
    else:
        assert move(client,"pause").status_code==200
    before=store.task().copy()

    response=client.put("/api/task",json={"goal":"Перезаписать","current_step":"Новый","expected_action":"Новое"})

    assert response.status_code==409
    assert response.json()["detail"]["category"]=="task_locked"
    assert store.task()==before


@pytest.mark.parametrize("finish",[False,True],ids=["pause","done"])
def test_answer_is_discarded_if_task_changes_during_provider_call(bundle,finish):
    store,fake,client=bundle
    if finish:
        assert move(client,"advance").status_code==200
        assert move(client,"advance").status_code==200
    started=Event(); release=Event()
    original_create=fake.chat.completions.create

    def blocking_create(**options):
        started.set()
        assert release.wait(5)
        return original_create(**options)

    fake.chat.completions.create=blocking_create
    result={}
    thread=Thread(target=lambda:result.setdefault("response",ask(client,"Долгий запрос")))
    thread.start()
    assert started.wait(5)
    transition_response=move(client,"approve" if finish else "pause")
    assert transition_response.status_code==200
    release.set();thread.join(5)

    assert not thread.is_alive()
    response=result["response"]
    assert response.status_code==409
    assert response.json()["detail"]["category"]=="stale_task_state"
    assert store.session_messages(store.active_session()["id"])==[]
