from __future__ import annotations

import json
import uuid
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from .agent import PersonalizedAgent
from .agent_actions import consent_decision, explicit_transition_target
from .invariant_checker import check_candidate
from .main import create_app
from .store import SQLiteMemoryStore, StaleTaskStateError
from .task_machine import MachineState, RunStatus, TaskEvent, TaskPhase, InvalidTransition, allowed_events, transition


class FakeCompletions:
    def __init__(self, answers=None, error=None):
        self.answers=list(answers or ["Готовый **ответ**"]); self.error=error; self.calls=[]
    def create(self,**options):
        self.calls.append(options)
        if self.error:raise self.error
        answer=self.answers.pop(0) if self.answers else "Ответ"
        if isinstance(answer,Exception):raise answer
        message=answer if isinstance(answer,SimpleNamespace) else SimpleNamespace(content=answer,tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)],usage=SimpleNamespace(prompt_tokens=10,completion_tokens=4,total_tokens=14))


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


def tool_answer(name,arguments,tool_call_id="call-1"):
    if name=="define_task" and "planning_ready" not in arguments:
        arguments={**arguments,"planning_ready":True}
    return SimpleNamespace(
        content=None,
        tool_calls=[SimpleNamespace(id=tool_call_id,function=SimpleNamespace(name=name,arguments=json.dumps(arguments,ensure_ascii=False)))],
    )


def ready_plan_answer(plan="Паспорт и билеты",tool_call_id="call-1"):
    return tool_answer("define_task",{
        "goal":"Подготовиться к поездке","proposed_plan":plan,"open_questions":"",
        "planning_ready":True,"current_step":"Согласовать готовый план",
        "expected_action":"Подтвердить план подготовки",
    },tool_call_id)


def agent_ask(client,prompt="Продолжай",request_id=None):
    request_id=request_id or str(uuid.uuid4())
    return client.post("/api/ask",json={
        "prompt":prompt,"request_id":request_id,
        "controls":{"provider":"deepseek","model":"deepseek-flash","compression_mode":"full_session"},
    })


def test_natural_consent_presets_match_phase_specific_subject_rules():
    preset_source=(Path(__file__).parents[1]/"frontend"/"src"/"demo-presets.js").read_text(encoding="utf-8")
    phrases={
        "implementation_ready":"Результат сборов меня устраивает, давай дальше проверять.",
        "validation_passed":"Проверка пройдена, всё верно, давай завершать.",
    }
    for approval_kind,phrase in phrases.items():
        assert f"'{phrase}'" in preset_source
        assert consent_decision(phrase,approval_kind)==(True,"consent_explicit")


@pytest.mark.parametrize(("phrase","target"),[
    ("Переходим к проверке.","validation"),
    ("Давай начнём собираться.","execution"),
    ("Хочу завершить подготовку и выезжать прямо сейчас.","done"),
])
def test_explicit_transition_target_is_derived_from_beneficial_user_phrases(phrase,target):
    assert explicit_transition_target(phrase)==target


def test_explicit_transition_target_ignores_questions_and_negated_requests():
    assert explicit_transition_target("Можно ли перейти к проверке?") is None
    assert explicit_transition_target("Не переходим к проверке.") is None


def test_explicit_transition_target_leaves_atomic_consent_to_approval_path():
    assert explicit_transition_target("План мне нравится, давай выполнять дальше.") is None
    assert explicit_transition_target("Проверка пройдена, всё верно, давай завершать.") is None


def save_evidence(client,**overrides):
    task=client.get("/api/state").json()["task"]
    payload={
        "plan_artifact":task["plan_artifact"],"execution_result":task["execution_result"],
        "blocking_issues":task["blocking_issues"],"expected_revision":task["revision"],**overrides,
    }
    return client.put("/api/task/evidence",json=payload)


def decide(client,kind,value=True,expected_revision=None):
    revision=client.get("/api/state").json()["task"]["revision"] if expected_revision is None else expected_revision
    return client.post("/api/task/evidence/decision",json={"kind":kind,"value":value,"expected_revision":revision})


def move(client,event,target=None,current_step=None,expected_action=None,expected_revision=None):
    task=client.get("/api/state").json()["task"]
    default_target={
        ("planning","advance"):"execution",("execution","advance"):"validation",
        ("validation","approve"):"done",
        (task["phase"],"pause"):task["phase"],(task["phase"],"resume"):task["phase"],
    }.get((task["phase"],event),"done")
    payload={"event":event,"target":target or default_target,"expected_revision":task["revision"] if expected_revision is None else expected_revision}
    if event in ("advance","approve"):
        payload.update(
            next_current_step=current_step or f"Checkpoint после {event}",
            next_expected_action=expected_action or f"Действие после {event}",
        )
    return client.post("/api/task/transition",json=payload)


def satisfy_current_guard(client):
    task=client.get("/api/state").json()["task"]
    if task["phase"]=="planning":
        assert save_evidence(client,plan_artifact="1. Подготовить API\n2. Прогнать тесты").status_code==200
        assert decide(client,"plan_approved").status_code==200
    elif task["phase"]=="execution":
        assert save_evidence(client,execution_result="API готов, тесты проходят").status_code==200
        assert decide(client,"implementation_ready").status_code==200
    elif task["phase"]=="validation":
        if task["blocking_issues"]:
            assert save_evidence(client,blocking_issues="").status_code==200
        assert decide(client,"validation_passed").status_code==200


def guarded_move(client,event="advance",**kwargs):
    satisfy_current_guard(client)
    return move(client,event,**kwargs)


def test_defaults_seed_four_builtins_with_ordinary_active(bundle):
    store,_,client=bundle
    state=client.get("/api/state").json()
    assert [p["name"] for p in state["profiles"]]==["Новичок","Обычный","Эксперт","Руководитель"]
    assert [p["id"] for p in state["profiles"]]==[1,2,3,4]
    assert state["active_profile"]["name"]=="Обычный"
    assert len(state["sessions"])==1
    assert state["active_session"]["is_active"]==1
    assert state["active_session"]["id"]==2
    assert state["active_session"]["messages"]==[]
    assert store.active_session(state["active_profile"]["id"])["id"]==state["active_session"]["id"]


def test_builtin_profile_migration_preserves_existing_edits_and_activates_ordinary(tmp_path):
    database=tmp_path/"legacy.sqlite3"
    store=SQLiteMemoryStore(database)
    with store.connection() as connection:
        novice_id=connection.execute("SELECT id FROM user_profiles WHERE name='Новичок'").fetchone()["id"]
        connection.execute("UPDATE user_profiles SET constraints='Мои сохранённые настройки',is_active=0")
        connection.execute("UPDATE user_profiles SET is_active=1 WHERE id=?",(novice_id,))
        connection.execute("DELETE FROM user_profiles WHERE name IN ('Обычный','Эксперт','Руководитель')")
        connection.execute("DELETE FROM schema_migrations WHERE version='day15_builtin_profiles_v2'")

    migrated=SQLiteMemoryStore(database)
    assert [profile["name"] for profile in migrated.profiles()]==[
        "Новичок","Обычный","Эксперт","Руководитель",
    ]
    assert migrated.active_profile()["name"]=="Обычный"
    assert migrated.profile(novice_id)["constraints"]=="Мои сохранённые настройки"
    assert all(migrated.active_session(profile["id"]) for profile in migrated.profiles())
    with migrated.connection() as connection:
        assert connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version='day15_builtin_profiles_v2'"
        ).fetchone()

    novice=migrated.profile(novice_id)
    migrated.activate_profile(novice["id"])
    reopened=SQLiteMemoryStore(database)
    assert reopened.active_profile()["name"]=="Новичок"


def test_builtin_profile_self_heal_is_idempotent_and_uses_ordinary_if_no_active(tmp_path):
    database=tmp_path/"self-heal.sqlite3"
    store=SQLiteMemoryStore(database)
    with store.connection() as connection:
        connection.execute("UPDATE user_profiles SET is_active=0")
        connection.execute("DELETE FROM user_profiles WHERE name='Эксперт'")

    healed=SQLiteMemoryStore(database)
    healed_again=SQLiteMemoryStore(database)
    assert healed_again.active_profile()["name"]=="Обычный"
    assert [profile["name"] for profile in healed_again.profiles()].count("Эксперт")==1
    expert=next(profile for profile in healed.profiles() if profile["name"]=="Эксперт")
    assert healed.active_session(expert["id"])["is_active"]==1


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


def test_switch_uses_new_profile_and_only_its_history(bundle):
    _,fake,client=bundle
    assert ask(client,"NOVICE_ONLY").status_code==200
    expert=next(profile for profile in client.get("/api/state").json()["profiles"] if profile["name"]=="Эксперт")
    client.post(f"/api/profiles/{expert['id']}/activate")
    assert ask(client,"EXPERT_QUESTION").status_code==200
    messages=fake.chat.completions.calls[-1]["messages"]
    joined="\n".join(item["content"] for item in messages)
    assert "Эксперт" in joined and "NOVICE_ONLY" not in joined


def test_profile_sessions_are_isolated_and_restore_last_selected(bundle):
    _,_,client=bundle
    novice=client.get("/api/state").json()["active_profile"]
    first=client.get("/api/state").json()["active_session"]["id"]
    second=client.post("/api/sessions").json()["active_session"]["id"]
    expert=next(profile for profile in client.get("/api/state").json()["profiles"] if profile["name"]=="Эксперт")
    client.post(f"/api/profiles/{expert['id']}/activate")
    assert client.post(f"/api/sessions/{second}/activate").status_code==404
    client.post(f"/api/profiles/{novice['id']}/activate")
    state=client.get("/api/state").json()
    assert state["active_session"]["id"]==second and {x["id"] for x in state["sessions"]}=={first,second}


def test_working_and_longterm_are_shared_across_profiles(bundle):
    _,_,client=bundle
    client.put("/api/task",json={"goal":"Выбрать архитектуру","hard_constraints":"Бюджет 1 млн","task_data":"100 документов","open_questions":"SLA"})
    client.post("/api/long-term",json={"entry_type":"decision","content":"Используем PostgreSQL"})
    expert=next(profile for profile in client.get("/api/state").json()["profiles"] if profile["name"]=="Эксперт")
    state=client.post(f"/api/profiles/{expert['id']}/activate").json()
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
    expert=next(profile for profile in client.get("/api/state").json()["profiles"] if profile["name"]=="Эксперт")
    other=client.post(f"/api/profiles/{expert['id']}/activate").json()["active_session"]
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
    expert=next(profile for profile in client.get("/api/state").json()["profiles"] if profile["name"]=="Эксперт")
    foreign=client.post(f"/api/profiles/{expert['id']}/activate").json()["active_session"]["id"]
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


def test_full_reset_restores_builtins_and_resets_all_app_sequences(bundle):
    store,_,client=bundle
    client.post("/api/profiles",json=profile_payload())
    client.post("/api/invariants",json=invariant_payload())
    assert ask(client).status_code==200
    assert guarded_move(client,"advance").status_code==200
    state=client.post("/api/memory/clear").json()
    assert [profile["id"] for profile in state["profiles"]]==[1,2,3,4]
    assert state["active_profile"]["name"]=="Обычный"
    assert len(state["sessions"])==1 and state["sessions"][0]["is_active"]==1
    assert state["active_session"]["id"]==2
    assert state["active_session"]["messages"]==[] and state["task"]["phase"]=="planning" and state["long_term"]==[]
    assert state["task"]["run_status"]=="active" and state["task_transition_events"]==[]
    assert state["task_transition_attempts"]==[] and state["task"]["plan_artifact"]==""
    assert state["task"]["planning_ready"] is False
    assert state["events"]==[]

    invariant=client.post("/api/invariants",json=invariant_payload()).json()["invariants"][0]
    assert invariant["id"]==1
    assert ask(client).status_code==200
    assert store.invariant_check_events()[0]["id"]==1
    assert guarded_move(client,"advance").status_code==200
    assert store.task_transition_attempts()[0]["id"]==1
    assert store.task_transition_events()[0]["id"]==1
    with store.connection() as connection:
        assert connection.execute(
                "SELECT 1 FROM schema_migrations WHERE version='day15_builtin_profiles_v2'"
        ).fetchone()
        assert connection.execute(
                "SELECT 1 FROM schema_migrations WHERE version='day15_planning_ready_v1'"
        ).fetchone()


def test_planning_ready_migration_preserves_already_advanced_existing_database(tmp_path):
    database=tmp_path/"existing.sqlite3"
    store=SQLiteMemoryStore(database)
    with store.connection() as connection:
        connection.execute("DELETE FROM schema_migrations WHERE version='day15_planning_ready_v1'")
        connection.execute(
            """UPDATE tasks SET phase='validation',plan_artifact='Старый утверждённый план',
               open_questions='Исторический вопрос',planning_ready=0 WHERE id=1"""
        )

    reopened=SQLiteMemoryStore(database)

    assert reopened.task()["phase"]=="validation"
    assert reopened.task()["planning_ready"] is True
    with reopened.connection() as connection:
        assert connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version='day15_planning_ready_v1'"
        ).fetchone()


def test_planning_ready_migration_expires_legacy_pending_for_unready_draft(tmp_path):
    database=tmp_path/"legacy-pending.sqlite3"
    store=SQLiteMemoryStore(database)
    session_id=store.active_session()["id"]
    with store.connection() as connection:
        action_id=connection.execute(
            """INSERT INTO agent_actions(request_id,tool_call_id,session_id,action,outcome,reason_code,
               phase_before,phase_after,revision_before,revision_after)
               VALUES ('legacy-request','legacy-call',?,'define_task','accepted','accepted','planning','planning',0,1)""",
            (session_id,),
        ).lastrowid
        connection.execute(
            """INSERT INTO pending_agent_approvals(session_id,action_id,kind,expected_phase,expected_revision,display_summary)
               VALUES (?,?, 'plan_approved','planning',1,'Legacy plan')""",
            (session_id,action_id),
        )
        connection.execute(
            """UPDATE tasks SET revision=1,plan_artifact='Черновик',open_questions='Критичный вопрос',
               planning_ready=0 WHERE id=1"""
        )
        connection.execute("DELETE FROM schema_migrations WHERE version='day15_planning_ready_v1'")

    reopened=SQLiteMemoryStore(database)

    assert reopened.task()["planning_ready"] is False
    assert reopened.pending_agent_approvals()[0]["status"]=="expired"


def test_pure_task_machine_routes_and_rejects_invalid_events():
    planning=MachineState(TaskPhase.PLANNING,RunStatus.ACTIVE)
    execution=transition(planning,TaskEvent.ADVANCE)
    validation=transition(execution,TaskEvent.ADVANCE)
    with pytest.raises(InvalidTransition):
        transition(validation,TaskEvent.ADVANCE)
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
    before_revision=store.task()["revision"]
    state=guarded_move(client,"advance",current_step="Выполнить план",expected_action="Зафиксировать результат").json()
    assert state["task"]["phase"]=="execution"
    assert state["task"]["current_step"]=="Выполнить план"
    assert state["task"]["expected_action"]=="Зафиксировать результат"
    assert state["task"]["revision"]==before_revision+3
    assert state["task_transition_events"][0]["event"]=="advance"
    before=store.task_transition_events()
    response=move(client,"approve")
    assert response.status_code==409
    assert store.task()["phase"]=="execution" and store.task_transition_events()==before


@pytest.mark.parametrize("advance_count,phase",[(0,"planning"),(1,"execution"),(2,"validation")])
def test_pause_resume_preserves_checkpoint_and_messages(bundle,advance_count,phase):
    store,_,client=bundle
    for index in range(advance_count):guarded_move(client,"advance",current_step=f"Шаг {index}",expected_action=f"Действие {index}")
    client.put("/api/task",json={"goal":"Продолжить работу","current_step":"Шаг X","expected_action":"Действие Y"})
    assert ask(client,"Контекст до паузы").status_code==200
    before_messages=store.session_messages(store.active_session()["id"])
    evidence_before={key:store.task()[key] for key in ("plan_artifact","plan_approved","execution_result","implementation_ready","validation_passed","blocking_issues")}
    paused=move(client,"pause").json()
    assert paused["task"]["phase"]==phase and paused["task"]["run_status"]=="paused"
    calls_before=len(bundle[1].chat.completions.calls)
    blocked=ask(client,"Не должен уйти провайдеру")
    assert blocked.status_code==409 and len(bundle[1].chat.completions.calls)==calls_before
    assert store.session_messages(store.active_session()["id"])==before_messages
    resumed=move(client,"resume").json()
    assert resumed["task"]["phase"]==phase and resumed["task"]["current_step"]=="Шаг X"
    assert resumed["task"]["expected_action"]=="Действие Y"
    response=ask(client,"Продолжай")
    assert response.status_code==200
    preview=response.json()["preview"]
    assert {key:resumed["task"][key] for key in evidence_before}==evidence_before
    assert preview["task_state_snapshot"]["phase"]==phase and preview["task_state_snapshot"]["revision"]==store.task()["revision"]
    sent="\n".join(item["content"] for item in bundle[1].chat.completions.calls[-1]["messages"])
    assert "Контекст до паузы" in sent and "Продолжай" in sent and "Не должен уйти" not in sent


def test_done_is_terminal_and_blocks_ask(bundle):
    store,fake,client=bundle
    for event in ("advance","advance","approve"):
        assert guarded_move(client,event).status_code==200
    state=client.get("/api/state").json()
    assert state["task"]["phase"]=="done" and state["allowed_events"]==[]
    calls=len(fake.chat.completions.calls)
    assert ask(client).status_code==409
    assert len(fake.chat.completions.calls)==calls and store.session_messages(store.active_session()["id"])==[]


def test_phase_transition_requires_and_atomically_saves_next_checkpoint(bundle):
    store,_,client=bundle
    satisfy_current_guard(client)
    before=store.task().copy()
    missing=client.post("/api/task/transition",json={"event":"advance","target":"execution","expected_revision":before["revision"]})
    assert missing.status_code==409
    assert store.task()==before and store.task_transition_events()==[]

    changed=move(client,"advance",current_step="Начать реализацию",expected_action="Собрать рабочий результат").json()["task"]
    assert (changed["phase"],changed["current_step"],changed["expected_action"])==(
        "execution","Начать реализацию","Собрать рабочий результат"
    )
    assert changed["revision"]==before["revision"]+1


@pytest.mark.parametrize("terminal",[False,True])
def test_task_edits_are_rejected_while_paused_or_done(bundle,terminal):
    store,_,client=bundle
    if terminal:
        for event in ("advance","advance","approve"):
            assert guarded_move(client,event).status_code==200
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
        assert guarded_move(client,"advance").status_code==200
        assert guarded_move(client,"advance").status_code==200
        satisfy_current_guard(client)
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


def test_transition_options_expose_unmet_requirements(bundle):
    _,_,client=bundle
    state=client.get("/api/state").json()
    advance=next(item for item in state["transition_options"] if item["event"]=="advance")
    pause=next(item for item in state["transition_options"] if item["event"]=="pause")
    assert advance["target"]=="execution" and advance["enabled"] is False
    assert {item["code"] for item in advance["unmet_requirements"]}=={
        "plan_artifact_missing","planning_not_ready","plan_not_approved"
    }
    assert pause["enabled"] is True and state["allowed_events"]==["pause"]


def test_invalid_skip_is_rejected_and_safely_audited(bundle):
    store,_,client=bundle
    before=store.task().copy()
    response=move(client,"advance",target="validation")

    assert response.status_code==409
    assert response.json()["detail"]["reason_code"]=="invalid_target"
    assert store.task()==before and store.task_transition_events()==[]
    attempt=store.task_transition_attempts()[0]
    assert attempt["outcome"]=="rejected" and attempt["requested_target"]=="validation"
    assert attempt["unmet_requirements"]==[]


def test_planning_guard_requires_saved_and_explicitly_approved_plan(bundle):
    store,_,client=bundle
    before=store.task().copy()
    empty=move(client,"advance")
    assert empty.status_code==409
    assert {item["code"] for item in empty.json()["detail"]["unmet_requirements"]}=={
        "plan_artifact_missing","planning_not_ready","plan_not_approved"
    }
    assert store.task()==before

    assert save_evidence(client,plan_artifact="План релиза").status_code==200
    unapproved=move(client,"advance")
    assert [item["code"] for item in unapproved.json()["detail"]["unmet_requirements"]]==["plan_not_approved"]
    assert decide(client,"plan_approved").status_code==200
    assert move(client,"advance").status_code==200


def test_execution_guard_requires_result_and_readiness(bundle):
    _,_,client=bundle
    assert guarded_move(client,"advance").status_code==200
    empty=move(client,"advance")
    assert {item["code"] for item in empty.json()["detail"]["unmet_requirements"]}=={"execution_result_missing","implementation_not_ready"}
    assert save_evidence(client,execution_result="Реализация готова").status_code==200
    not_ready=move(client,"advance")
    assert [item["code"] for item in not_ready.json()["detail"]["unmet_requirements"]]==["implementation_not_ready"]
    assert decide(client,"implementation_ready").status_code==200
    assert move(client,"advance").status_code==200


def test_validation_guard_requires_pass_and_no_blocking_issues(bundle):
    _,_,client=bundle
    assert guarded_move(client,"advance").status_code==200
    assert guarded_move(client,"advance").status_code==200
    assert save_evidence(client,blocking_issues="Не пройден smoke test").status_code==200
    blocked=move(client,"approve")
    assert {item["code"] for item in blocked.json()["detail"]["unmet_requirements"]}=={"validation_not_passed","blocking_issues_present"}
    assert decide(client,"validation_passed").status_code==409
    assert save_evidence(client,blocking_issues="").status_code==200
    not_passed=move(client,"approve")
    assert [item["code"] for item in not_passed.json()["detail"]["unmet_requirements"]]==["validation_not_passed"]
    assert decide(client,"validation_passed").status_code==200
    assert move(client,"approve").status_code==200


def test_stale_revision_rejects_transition_without_task_mutation(bundle):
    store,_,client=bundle
    satisfy_current_guard(client)
    before=store.task().copy()
    response=move(client,"advance",expected_revision=before["revision"]-1)

    assert response.status_code==409
    assert response.json()["detail"]["reason_code"]=="stale_revision"
    assert store.task()==before
    attempt=store.task_transition_attempts()[0]
    assert attempt["expected_revision"]==before["revision"]-1 and attempt["actual_revision"]==before["revision"]


def test_transition_attempt_audit_never_stores_user_artifacts(bundle):
    store,_,client=bundle
    secret_plan="PRIVATE PLAN CONTENT"
    assert save_evidence(client,plan_artifact=secret_plan).status_code==200
    response=move(client,"advance")
    assert response.status_code==409
    serialized=json.dumps(store.task_transition_attempts(),ensure_ascii=False)
    assert secret_plan not in serialized
    assert "PRIVATE" not in serialized


def test_backward_transition_is_rejected_without_mutating_evidence(bundle):
    store,_,client=bundle
    assert guarded_move(client,"advance").status_code==200
    assert guarded_move(client,"advance").status_code==200
    assert save_evidence(client,blocking_issues="Исправить паспорт").status_code==200
    before=store.task().copy()

    rejected=move(client,"request_changes",target="execution")

    assert rejected.status_code==409
    assert rejected.json()["detail"]["reason_code"]=="invalid_transition"
    assert store.task()==before


def test_full_guarded_path_with_pause_resume(bundle):
    _,_,client=bundle
    assert save_evidence(client,plan_artifact="Согласованный план").status_code==200
    assert decide(client,"plan_approved").status_code==200
    assert move(client,"advance",current_step="Реализовать",expected_action="Сохранить результат").status_code==200
    assert save_evidence(client,execution_result="Функция реализована, тесты зелёные").status_code==200
    assert decide(client,"implementation_ready").status_code==200
    before_pause=client.get("/api/state").json()["task"]
    assert move(client,"pause").status_code==200
    resumed=move(client,"resume").json()["task"]
    for key in ("phase","plan_artifact","plan_approved","execution_result","implementation_ready","blocking_issues"):
        assert resumed[key]==before_pause[key]
    assert move(client,"advance",current_step="Валидировать",expected_action="Зафиксировать verdict").status_code==200
    assert decide(client,"validation_passed").status_code==200
    done=move(client,"approve",current_step="Завершено",expected_action="Нет").json()
    assert done["task"]["phase"]=="done" and done["allowed_events"]==[]
    assert [item["outcome"] for item in done["task_transition_attempts"][:5]]==["accepted"]*5


def test_evidence_actions_require_current_revision_and_correct_phase(bundle):
    _,_,client=bundle
    revision=client.get("/api/state").json()["task"]["revision"]
    assert save_evidence(client,plan_artifact="План").status_code==200
    stale=decide(client,"plan_approved",expected_revision=revision)
    assert stale.status_code==409 and stale.json()["detail"]["category"]=="stale_revision"
    assert decide(client,"plan_approved").status_code==200
    assert move(client,"advance").status_code==200
    assert save_evidence(client,plan_artifact="Изменённый план").status_code==409


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


def test_lifecycle_ask_without_candidate_skips_invariant_check_and_fake_candidate(bundle):
    store,fake,client=bundle
    created=client.post("/api/invariants",json=invariant_payload()).json()["invariants"][0]

    response=client.post(
        "/api/ask",
        json={
            "prompt":"Объясни текущее состояние задачи",
            "controls":{"provider":"deepseek","model":"deepseek-flash","compression_mode":"full_session"},
        },
    )

    assert response.status_code==200
    preview=response.json()["preview"]
    assert preview["invariant_check"] is None
    assert preview["invariant_snapshot"][0]["id"]==created["id"]
    assert store.invariant_check_events()==[]
    sent="\n".join(message["content"] for message in fake.chat.completions.calls[0]["messages"])
    assert "ACTIVE_INVARIANTS" in sent
    assert "STRUCTURED_CANDIDATE" not in sent


def test_lifecycle_ask_without_candidate_omits_empty_invariant_context(bundle):
    store,fake,client=bundle

    response=client.post("/api/ask",json={"prompt":"Что нужно сделать дальше?","controls":{}})

    assert response.status_code==200
    assert response.json()["preview"]["invariant_snapshot"]==[]
    assert response.json()["preview"]["invariant_check"] is None
    assert store.invariant_check_events()==[]
    sent="\n".join(message["content"] for message in fake.chat.completions.calls[0]["messages"])
    assert "ACTIVE_INVARIANTS" not in sent
    assert "STRUCTURED_CANDIDATE" not in sent


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


def test_agent_define_task_uses_real_tool_call_and_request_id_is_idempotent(bundle):
    store,fake,client=bundle
    fake.chat.completions.answers=[tool_answer("define_task",{
        "goal":"Собрать чемодан","hard_constraints":"Не забыть документы","task_data":"Поездка на выходные",
        "open_questions":"","proposed_plan":"Паспорт, одежда, лекарства, зарядка; дождевик берём как резерв",
        "current_step":"Составить план","expected_action":"Сохранить план подготовки",
    })]
    request_id=str(uuid.uuid4())

    first=agent_ask(client,"Помоги начать сборы",request_id)
    revision=store.task()["revision"]
    replay=agent_ask(client,"Этот текст не должен обрабатываться",request_id)

    assert first.status_code==200 and first.json()["action"]["action"]=="define_task"
    assert first.json()["action"]["outcome"]=="accepted" and store.task()["goal"]=="Собрать чемодан"
    assert "tools" in fake.chat.completions.calls[0] and fake.chat.completions.calls[0]["tool_choice"]=="required"
    serialized_tools=json.dumps(fake.chat.completions.calls[0]["tools"])
    assert "expected_revision" not in serialized_tools and '"target"' not in serialized_tools
    assert "next_current_step" not in serialized_tools and "next_expected_action" not in serialized_tools
    assert replay.status_code==200 and replay.json()["idempotent_replay"] is True
    assert len(fake.chat.completions.calls)==2 and store.task()["revision"]==revision


def test_planning_draft_has_no_pending_and_guard_reports_not_ready(bundle):
    store,fake,client=bundle
    fake.chat.completions.answers=[tool_answer("define_task",{
        "goal":"Организовать поездку","proposed_plan":"Черновик: выбрать безопасный транспорт",
        "open_questions":"Есть ли доступный транспорт?; есть ли обязательные лекарства?",
        "planning_ready":False,"current_step":"Уточнить критичные ограничения",
        "expected_action":"Ответить одним сообщением на два вопроса",
    }),"Нужно уточнить только два критичных пункта."]

    response=agent_ask(client,"Помоги организовать поездку").json()

    assert response["action"]["outcome"]=="accepted"
    assert response["pending_approval"] is None
    assert store.task()["planning_ready"] is False
    advance=next(item for item in store.task_transition_options() if item["event"]=="advance")
    assert "planning_not_ready" in {item["code"] for item in advance["unmet_requirements"]}


def test_planning_refinement_reuses_define_task_and_ready_plan_creates_one_pending(bundle):
    store,fake,client=bundle
    draft={
        "goal":"Организовать поездку","proposed_plan":"Черновик",
        "open_questions":"Есть ли автомобиль?","planning_ready":False,
        "current_step":"Уточнить транспорт","expected_action":"Ответить про транспорт",
    }
    fake.chat.completions.answers=[tool_answer("define_task",draft),"Уточните транспорт."]
    agent_ask(client,"Помоги с поездкой")
    fake.chat.completions.answers=[tool_answer("define_task",{
        **draft,"task_data":"Едем на машине","open_questions":"","planning_ready":True,
        "proposed_plan":"Проверить документы; собрать вещи; заложить резерв времени",
        "current_step":"Согласовать готовый план","expected_action":"Подтвердить план",
    }),"План готов к подтверждению."]

    refined=agent_ask(client,"Едем на машине, других ограничений нет").json()

    assert refined["action"]["outcome"]=="accepted"
    assert store.task()["planning_ready"] is True and store.task()["open_questions"]==""
    assert len(store.pending_agent_approvals(pending_only=True))==1
    assert refined["pending_approval"]["kind"]=="plan_approved"


def test_ready_plan_with_blocking_questions_is_rejected_without_mutation(bundle):
    store,fake,client=bundle
    before=store.task().copy()
    fake.chat.completions.answers=[tool_answer("define_task",{
        "goal":"Поездка","proposed_plan":"Черновик","open_questions":"Где ночуем?",
        "planning_ready":True,"current_step":"Согласовать","expected_action":"Подтвердить",
    }),"План пока не готов."]

    response=agent_ask(client,"Составь план").json()

    assert response["action"]["outcome"]=="rejected"
    assert response["action"]["reason_code"]=="planning_questions_open"
    assert store.task()==before and store.pending_agent_approvals(pending_only=True)==[]


def test_planning_not_ready_requires_nonempty_blocking_questions(bundle):
    store,fake,client=bundle
    before=store.task().copy()
    fake.chat.completions.answers=[tool_answer("define_task",{
        "goal":"Поездка","proposed_plan":"Черновик","open_questions":"",
        "planning_ready":False,"current_step":"Уточнить","expected_action":"Ответить",
    }),"Черновик не сохранён."]

    response=agent_ask(client,"Сделай черновик").json()

    assert response["action"]["outcome"]=="rejected"
    assert response["action"]["reason_code"]=="planning_questions_missing"
    assert store.task()==before and store.pending_agent_approvals(pending_only=True)==[]


def test_actual_forest_preset_is_saved_as_ready_plan_without_question_loop(bundle):
    store,fake,client=bundle
    preset_source=(Path(__file__).parents[1]/"frontend"/"src"/"demo-presets.js").read_text(encoding="utf-8")
    assert "общий бюджет — 12 000 рублей" in preset_source and "По прогнозу возможен дождь" in preset_source
    fake.chat.completions.answers=[tool_answer("define_task",{
        "goal":"Подготовить поездку четырёх друзей в лес на два дня",
        "hard_constraints":"Бюджет 12 000 рублей; без мяса и острой еды; учесть дождь",
        "task_data":"Едем на машине, ночуем в палатках",
        "open_questions":"","planning_ready":True,
        "proposed_plan":"Распределить палатки и аптечку; купить подходящую еду; взять дождевики; проверить документы; заложить резерв бюджета",
        "current_step":"Согласовать готовый план","expected_action":"Подтвердить план подготовки",
    }),"План готов; необязательные детали учтены допущениями."]

    response=agent_ask(client,"Мы вчетвером едем на машине в лес на два дня, бюджет 12000, возможен дождь.").json()

    assert response["action"]["outcome"]=="accepted"
    assert store.task()["planning_ready"] is True and store.task()["open_questions"]==""
    assert response["pending_approval"]["kind"]=="plan_approved"


def test_planning_save_current_evidence_is_rejected_on_empty_task_without_mutation(bundle):
    store,fake,client=bundle
    before=store.task().copy()
    fake.chat.completions.answers=[
        tool_answer("save_current_evidence",{"content":"Попытка обойти typed planning"}),
        "Сначала нужно определить задачу и готовность плана.",
    ]

    response=agent_ask(client,"Сохрани этот план").json()

    assert response["action"]["outcome"]=="rejected"
    assert response["action"]["reason_code"]=="planning_requires_define_task"
    assert store.task()==before and store.pending_agent_approvals(pending_only=True)==[]


def test_planning_save_current_evidence_cannot_overwrite_existing_draft_or_create_pending(bundle):
    store,fake,client=bundle
    fake.chat.completions.answers=[tool_answer("define_task",{
        "goal":"Организовать поездку","proposed_plan":"Черновой безопасный маршрут",
        "open_questions":"Есть ли автомобиль?","planning_ready":False,
        "current_step":"Уточнить транспорт","expected_action":"Ответить про транспорт",
    }),"Уточните транспорт."]
    agent_ask(client,"Помоги с поездкой")
    before=store.task().copy()
    fake.chat.completions.answers=[
        tool_answer("save_current_evidence",{"content":"Готовый план в обход planning_ready"}),
        "Черновик можно уточнить только через планирование.",
    ]

    response=agent_ask(client,"Замени черновик и сразу дай подтвердить").json()

    assert response["action"]["outcome"]=="rejected"
    assert response["action"]["reason_code"]=="planning_requires_define_task"
    assert store.task()==before and store.pending_agent_approvals(pending_only=True)==[]


def test_agent_tool_system_prompt_states_authority_boundary(bundle):
    _,fake,client=bundle
    fake.chat.completions.answers=["Показываю текущее состояние без изменений."]

    response=agent_ask(client,"Что сейчас происходит?")

    assert response.status_code==200
    system_text="\n".join(
        message["content"] for message in fake.chat.completions.calls[0]["messages"] if message["role"]=="system"
    )
    assert "Агент может лишь запросить allowlisted tool" in system_text
    assert "не пишет state напрямую" in system_text
    assert "никогда не подтверждает approval за пользователя" in system_text
    assert "В обычном чате только объясняй snapshot" in system_text
    assert "backend повторно проверит phase, revision и guards" in system_text


def test_agent_ready_plan_creates_human_pending_approval_and_confirm_is_idempotent(bundle):
    store,fake,client=bundle
    private_evidence="Паспорт, билеты, лекарства и PRIVATE зарядка"
    fake.chat.completions.answers=[ready_plan_answer(private_evidence)]

    response=agent_ask(client,"Сохрани мой план подготовки")
    body=response.json();pending=body["pending_approval"]

    assert response.status_code==200 and body["action"]["outcome"]=="accepted"
    assert body["action"]["safe_details"]=={"evidence_kind":"plan_artifact","planning_ready":True}
    assert store.task()["plan_artifact"]==private_evidence and store.task()["plan_approved"] is False
    assert pending["kind"]=="plan_approved" and pending["status"]=="pending"
    assert pending["display_summary"]=="Подтвердить предложенный план подготовки"
    assert private_evidence not in json.dumps(store.agent_actions(),ensure_ascii=False)
    before=store.task()["revision"]
    confirmed=client.post(f"/api/agent/approvals/{pending['id']}/confirm")
    after=store.task()["revision"]
    repeated=client.post(f"/api/agent/approvals/{pending['id']}/confirm")
    assert confirmed.status_code==200 and confirmed.json()["outcome"]=="confirmed"
    assert store.task()["plan_approved"] is True and after==before+1
    assert repeated.status_code==200 and repeated.json()["outcome"]=="already_resolved"
    assert store.task()["revision"]==after

    fake.chat.completions.answers=[tool_answer("request_transition",{
        "event":"advance","next_current_step":"Собрать чемодан","next_expected_action":"Сохранить результат сборов",
    })]
    transitioned=agent_ask(client,"Переходи к сбору чемодана").json()
    assert transitioned["action"]["outcome"]=="accepted" and store.task()["phase"]=="execution"
    assert transitioned["preview"]["task_state_snapshot"]["revision"]==store.task()["revision"]
    assert store.task_transition_events()[0]["to_phase"]=="execution"


def test_agent_pending_approval_reject_stale_and_cross_session_are_safe(bundle):
    store,fake,client=bundle
    fake.chat.completions.answers=[ready_plan_answer()]
    pending=agent_ask(client,"Сохрани список").json()["pending_approval"]
    expert=next(item for item in client.get("/api/state").json()["profiles"] if item["name"]=="Эксперт")
    client.post(f"/api/profiles/{expert['id']}/activate")
    assert client.post(f"/api/agent/approvals/{pending['id']}/confirm").status_code==404
    ordinary=next(item for item in client.get("/api/state").json()["profiles"] if item["name"]=="Обычный")
    client.post(f"/api/profiles/{ordinary['id']}/activate")
    client.put("/api/task",json={"goal":"Изменённая задача","current_step":"Шаг","expected_action":"Действие"})
    stale=client.post(f"/api/agent/approvals/{pending['id']}/confirm")
    assert stale.status_code==200 and stale.json()["outcome"]=="already_resolved"
    assert store.task()["plan_approved"] is False
    assert store.pending_agent_approvals()[0]["status"]=="expired"


def test_agent_pending_approval_can_be_rejected_without_changing_task_flag(bundle):
    store,fake,client=bundle
    fake.chat.completions.answers=[ready_plan_answer()]
    pending=agent_ask(client,"Сохрани список").json()["pending_approval"]
    revision=store.task()["revision"]

    rejected=client.post(f"/api/agent/approvals/{pending['id']}/reject")
    repeated=client.post(f"/api/agent/approvals/{pending['id']}/reject")

    assert rejected.status_code==200 and rejected.json()["outcome"]=="rejected"
    assert repeated.json()["outcome"]=="already_resolved"
    assert store.task()["plan_approved"] is False and store.task()["revision"]==revision


def test_shared_task_revision_expires_pending_from_another_profile_session(bundle):
    store,fake,client=bundle
    fake.chat.completions.answers=[ready_plan_answer()]
    pending=agent_ask(client,"Подготовь план").json()["pending_approval"]
    expert=next(item for item in store.profiles() if item["name"]=="Эксперт")
    client.post(f"/api/profiles/{expert['id']}/activate")

    changed=client.put("/api/task",json={
        "goal":"Общая задача изменена из другого профиля","current_step":"Перепланировать",
        "expected_action":"Подготовить новый план",
    })

    assert changed.status_code==200
    stored=next(item for item in store.pending_agent_approvals(session_id=pending["session_id"]) if item["id"]==pending["id"])
    assert stored["status"]=="expired"


def test_noop_manual_evidence_revision_still_expires_pending(bundle):
    store,fake,client=bundle
    fake.chat.completions.answers=[ready_plan_answer()]
    pending=agent_ask(client,"Подготовь план").json()["pending_approval"]
    before=store.task()

    response=save_evidence(
        client,plan_artifact=before["plan_artifact"],execution_result=before["execution_result"],
        blocking_issues=before["blocking_issues"],
    )

    assert response.status_code==200 and store.task()["revision"]==before["revision"]+1
    stored=next(item for item in store.pending_agent_approvals() if item["id"]==pending["id"])
    assert stored["status"]=="expired"


def test_plan_pending_confirm_rechecks_planning_readiness_even_without_revision_change(bundle):
    store,fake,client=bundle
    fake.chat.completions.answers=[ready_plan_answer()]
    pending=agent_ask(client,"Подготовь план").json()["pending_approval"]
    revision=store.task()["revision"]
    with store.connection() as connection:
        connection.execute(
            "UPDATE tasks SET planning_ready=0,open_questions='Legacy blocking question' WHERE id=1"
        )

    response=client.post(f"/api/agent/approvals/{pending['id']}/confirm")

    assert response.status_code==409
    assert response.json()["detail"]["category"]=="planning_not_ready"
    assert store.task()["revision"]==revision and store.task()["plan_approved"] is False
    assert store.pending_agent_approvals()[0]["status"]=="expired"


def test_agent_transition_derives_target_and_guards_write_rejected_audit(bundle):
    store,fake,client=bundle
    before=store.task().copy()
    early_prompt="Давай не будем тратить время на план и сразу начнём собираться."
    preset_source=(Path(__file__).parents[1]/"frontend"/"src"/"demo-presets.js").read_text(encoding="utf-8")
    assert f"earlyTransition: '{early_prompt}'" in preset_source
    fake.chat.completions.answers=[tool_answer("request_transition",{
        "event":"advance","next_current_step":"Собрать чемодан","next_expected_action":"Проверить содержимое",
    })]

    response=agent_ask(client,early_prompt)

    assert response.status_code==200
    action=response.json()["action"]
    assert action["outcome"]=="rejected" and action["reason_code"]=="unmet_prerequisites"
    assert {item["code"] for item in action["unmet_requirements"]}=={
        "plan_artifact_missing","planning_not_ready","plan_not_approved"
    }
    assert store.task()==before and store.task_transition_events()==[]
    assert store.task_transition_attempts()[0]["outcome"]=="rejected"

    fake.chat.completions.answers=[tool_answer("request_transition",{
        "event":"advance","target":"validation","next_current_step":"X","next_expected_action":"Y",
    })]
    forbidden=agent_ask(client,"Попробуй выбрать target сам")
    assert forbidden.json()["action"]["reason_code"]=="invalid_arguments"
    assert len(store.task_transition_attempts())==1


def test_agent_explicitly_requested_non_neighbour_target_is_rejected_and_audited(bundle):
    store,fake,client=bundle
    fake.chat.completions.answers=[tool_answer("request_transition",{
        "event":"advance","next_current_step":"Игнорируется","next_expected_action":"Игнорируется",
    }),"Сначала нужно пройти следующий шаг."]

    response=agent_ask(client,"Переходим к проверке.").json()

    assert response["action"]["outcome"]=="rejected"
    assert response["action"]["reason_code"]=="invalid_target"
    assert store.task()["phase"]=="planning" and store.task()["revision"]==0
    attempt=store.task_transition_attempts()[0]
    assert attempt["requested_target"]=="validation"
    assert attempt["from_phase"]=="planning" and attempt["actual_revision"]==0
    assert store.task_transition_events()==[]


def test_agent_retries_an_oversized_define_task_once_before_any_mutation(bundle):
    store,fake,client=bundle
    fake.chat.completions.answers=[
        tool_answer("define_task",{
            "goal":"Подготовить поездку","proposed_plan":"x"*2001,"open_questions":"",
            "planning_ready":True,"current_step":"Согласовать план","expected_action":"Подтвердить план",
        },"too-long"),
        tool_answer("define_task",{
            "goal":"Подготовить поездку","proposed_plan":"Проверить документы и собрать вещи.","open_questions":"",
            "planning_ready":True,"current_step":"Согласовать план","expected_action":"Подтвердить план",
        },"compact"),
        "Я подготовил короткий план для подтверждения.",
    ]

    response=agent_ask(client,"Помоги подготовиться к поездке").json()

    assert response["action"]["outcome"]=="accepted"
    assert store.task()["revision"]==1 and store.task()["plan_artifact"]=="Проверить документы и собрать вещи."
    assert len(store.agent_actions())==1 and len(fake.chat.completions.calls)==3


def test_agent_rejects_after_one_invalid_compact_define_task_retry_without_mutation(bundle):
    store,fake,client=bundle
    fake.chat.completions.answers=[
        tool_answer("define_task",{
            "goal":"Подготовить поездку","proposed_plan":"x"*2001,"open_questions":"",
            "planning_ready":True,"current_step":"Согласовать план","expected_action":"Подтвердить план",
        },"too-long"),
        tool_answer("define_task",{
            "goal":"Подготовить поездку","proposed_plan":"y"*2001,"open_questions":"",
            "planning_ready":True,"current_step":"Согласовать план","expected_action":"Подтвердить план",
        },"still-too-long"),
        "Сейчас не удалось безопасно оформить план.",
    ]

    response=agent_ask(client,"Помоги подготовиться к поездке").json()

    assert response["action"]["outcome"]=="rejected"
    assert response["action"]["reason_code"]=="invalid_arguments"
    assert "компактно оформить" in response["action"]["message"]
    assert store.task()["revision"]==0 and store.task_transition_attempts()==[]
    assert len(store.agent_actions())==1 and len(fake.chat.completions.calls)==3


def test_agent_can_pause_and_resume_but_cannot_mutate_evidence_while_paused(bundle):
    store,fake,client=bundle
    client.put("/api/task",json={"goal":"Собрать чемодан","current_step":"Список","expected_action":"Сохранить"})
    checkpoint={key:store.task()[key] for key in ("phase","current_step","expected_action","revision")}
    fake.chat.completions.answers=[tool_answer("pause_task",{})]
    paused=agent_ask(client,"Поставь на паузу").json()
    assert paused["action"]["outcome"]=="accepted" and store.task()["run_status"]=="paused"
    assert store.task()["phase"]==checkpoint["phase"] and store.task()["current_step"]==checkpoint["current_step"]

    fake.chat.completions.answers=[tool_answer("save_current_evidence",{"content":"Нельзя сохранить"})]
    denied=agent_ask(client,"Сохрани evidence на паузе").json()
    assert denied["action"]["reason_code"]=="task_paused" and store.task()["plan_artifact"]==""

    fake.chat.completions.answers=[tool_answer("resume_task",{})]
    resumed=agent_ask(client,"Продолжи задачу").json()
    assert resumed["action"]["outcome"]=="accepted" and store.task()["run_status"]=="active"
    assert store.task()["phase"]==checkpoint["phase"] and store.task()["current_step"]==checkpoint["current_step"]


def test_agent_done_is_read_only_and_multiple_tools_never_mutate(bundle):
    store,fake,client=bundle
    multiple=SimpleNamespace(content=None,tool_calls=[
        SimpleNamespace(id="a",function=SimpleNamespace(name="define_task",arguments=json.dumps({"goal":"A","current_step":"B","expected_action":"C"}))),
        SimpleNamespace(id="b",function=SimpleNamespace(name="pause_task",arguments="{}")),
    ])
    fake.chat.completions.answers=[multiple]
    before=store.task().copy()
    rejected=agent_ask(client,"Сделай сразу два действия").json()
    assert rejected["action"]["reason_code"]=="multiple_actions" and store.task()==before

    for event in ("advance","advance","approve"):
        assert guarded_move(client,event).status_code==200
    fake.chat.completions.answers=[
        tool_answer("respond_without_action",{}),"Задача уже завершена; доступен только просмотр состояния.",
    ]
    explanation=agent_ask(client,"Что получилось?")
    assert explanation.status_code==200 and explanation.json()["action"] is None
    assert store.task()["phase"]=="done"


def test_agent_action_session_reset_and_full_reset_clear_audit_and_restart_ids(bundle):
    store,fake,client=bundle
    fake.chat.completions.answers=[ready_plan_answer("План подготовки")]
    first=agent_ask(client,"Сохрани список").json()
    assert first["action"]["id"]==1 and first["pending_approval"]["id"]==1
    session_id=client.get("/api/state").json()["active_session"]["id"]
    reset_session_state=client.post(f"/api/sessions/{session_id}/reset").json()
    assert reset_session_state["agent_actions"]==[] and reset_session_state["pending_agent_approvals"]==[]

    fake.chat.completions.answers=[ready_plan_answer("Новый список")]
    agent_ask(client,"Сохрани ещё раз")
    cleared=client.post("/api/memory/clear").json()
    assert cleared["agent_actions"]==[] and cleared["pending_agent_approvals"]==[]
    fake.chat.completions.answers=[ready_plan_answer("После reset")]
    after=agent_ask(client,"Сохрани после reset").json()
    assert after["action"]["id"]==1 and after["pending_approval"]["id"]==1


def test_agent_tool_is_rejected_if_revision_changes_during_provider_call(bundle):
    store,fake,client=bundle
    started=Event();release=Event()
    planned=tool_answer("define_task",{
        "goal":"Устаревшая задача","proposed_plan":"Устаревший план",
        "current_step":"Старый шаг","expected_action":"Старое действие",
    })

    def blocking_create(**options):
        fake.chat.completions.calls.append(options)
        started.set();assert release.wait(5)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=planned)],
            usage=SimpleNamespace(prompt_tokens=10,completion_tokens=4,total_tokens=14),
        )

    fake.chat.completions.create=blocking_create
    result={}
    thread=Thread(target=lambda:result.setdefault("response",agent_ask(client,"Определи задачу")))
    thread.start();assert started.wait(5)
    changed=client.put("/api/task",json={"goal":"Актуальная задача","current_step":"Новый шаг","expected_action":"Новое действие"})
    assert changed.status_code==200
    release.set();thread.join(5)

    assert not thread.is_alive() and result["response"].status_code==200
    action=result["response"].json()["action"]
    assert action["outcome"]=="rejected" and action["reason_code"]=="stale_revision"
    assert store.task()["goal"]=="Актуальная задача"


def test_agent_request_recovers_after_action_commit_and_append_failure(bundle,monkeypatch):
    store,fake,client=bundle
    private_goal="PRIVATE_RECOVERY_GOAL"
    fake.chat.completions.answers=[tool_answer("define_task",{
        "goal":private_goal,"proposed_plan":"Приватный план",
        "current_step":"Составить список","expected_action":"Сохранить evidence",
    })]
    request_id=str(uuid.uuid4())
    original_append=store.append_turn
    append_calls=0

    def fail_once(*args,**kwargs):
        nonlocal append_calls
        append_calls+=1
        if append_calls==1:
            raise StaleTaskStateError("Имитированный сбой после commit действия.")
        return original_append(*args,**kwargs)

    monkeypatch.setattr(store,"append_turn",fail_once)
    first=agent_ask(client,"Определи задачу",request_id)
    revision_after_commit=store.task()["revision"]

    assert first.status_code==409 and first.json()["detail"]["category"]=="stale_task_state"
    assert store.task()["goal"]==private_goal
    assert len(fake.chat.completions.calls)==2 and len(store.agent_actions())==1

    replay=agent_ask(client,"Определи задачу",request_id)

    assert replay.status_code==200
    body=replay.json()
    assert body["idempotent_replay"] is True and body["recovered_partial_commit"] is True
    assert body["action"]["id"]==store.agent_actions()[0]["id"]
    assert body["state"]["task"]["goal"]==private_goal
    assert "предложенный план" in body["answer"].lower()
    assert "действие выполнено" not in body["answer"].lower()
    assert len(fake.chat.completions.calls)==2 and len(store.agent_actions())==1
    assert store.task()["revision"]==revision_after_commit and append_calls==2
    assert private_goal not in json.dumps(store.agent_actions(),ensure_ascii=False)


def test_failed_turn_without_action_can_reclaim_same_request_id_after_provider_error(bundle):
    store,fake,client=bundle
    request_id=str(uuid.uuid4())
    before=store.task().copy()
    fake.chat.completions.error=RuntimeError("provider temporarily unavailable")

    failed=agent_ask(client,"Объясни состояние",request_id)

    assert failed.status_code==502
    assert failed.json()["detail"]["category"]=="provider_request"
    assert store.agent_actions()==[] and store.task()==before
    fake.chat.completions.error=None
    fake.chat.completions.answers=[tool_answer("respond_without_action",{}),"Состояние задачи не изменилось."]

    retried=agent_ask(client,"Объясни состояние",request_id)

    assert retried.status_code==200 and retried.json()["answer"]=="Состояние задачи не изменилось."
    assert retried.json()["idempotent_replay"] is False
    assert len(fake.chat.completions.calls)==3 and store.agent_actions()==[] and store.task()==before
    assert len(store.session_messages(store.active_session()["id"]))==2


def test_concurrent_partial_recovery_has_one_owner_and_one_message_pair(bundle,monkeypatch):
    store,fake,client=bundle
    request_id=str(uuid.uuid4())
    fake.chat.completions.answers=[tool_answer("define_task",{
        "goal":"Единственная мутация","proposed_plan":"Единственный план",
        "current_step":"Шаг","expected_action":"Действие",
    })]
    original_append=store.append_turn
    initial_failure=True

    def create_partial_commit(*args,**kwargs):
        nonlocal initial_failure
        if initial_failure:
            initial_failure=False
            raise StaleTaskStateError("Сбой после action commit")
        return original_append(*args,**kwargs)

    monkeypatch.setattr(store,"append_turn",create_partial_commit)
    assert agent_ask(client,"Определи задачу",request_id).status_code==409
    committed_revision=store.task()["revision"]
    assert len(store.agent_actions())==1 and len(fake.chat.completions.calls)==2

    recovery_started=Event();release_recovery=Event()

    def blocking_recovery_append(*args,**kwargs):
        recovery_started.set();assert release_recovery.wait(5)
        return original_append(*args,**kwargs)

    monkeypatch.setattr(store,"append_turn",blocking_recovery_append)
    responses=[]
    first=Thread(target=lambda:responses.append(agent_ask(client,"Определи задачу",request_id)))
    first.start();assert recovery_started.wait(5)
    second=Thread(target=lambda:responses.append(agent_ask(client,"Определи задачу",request_id)))
    second.start();second.join(5)
    assert not second.is_alive()
    release_recovery.set();first.join(5)

    assert not first.is_alive() and sorted(response.status_code for response in responses)==[200,400]
    successful=next(response.json() for response in responses if response.status_code==200)
    assert successful["idempotent_replay"] is True and successful["recovered_partial_commit"] is True
    assert len(fake.chat.completions.calls)==2
    assert len(store.agent_actions())==1 and store.task()["revision"]==committed_revision
    messages=store.session_messages(store.active_session()["id"])
    assert [message["role"] for message in messages]==["user","assistant"]


def test_full_agent_driven_lifecycle_with_human_approvals_and_safe_audits(bundle):
    store,fake,client=bundle

    def run_tool(name,arguments,prompt):
        fake.chat.completions.answers=[tool_answer(name,arguments)]
        response=agent_ask(client,prompt)
        assert response.status_code==200
        return response.json()

    private_plan="PRIVATE_PLAN: паспорт, билеты, лекарства, зарядка"
    private_result="PRIVATE_RESULT: чемодан собран, вес проверен"
    private_blocker="PRIVATE_BLOCKER: паспорт не положен в ручную кладь"

    defined=run_tool("define_task",{
        "goal":"Собрать чемодан для поездки на выходные",
        "hard_constraints":"Не забыть документы",
        "task_data":"Поездка на два дня",
        "open_questions":"",
        "proposed_plan":private_plan,
        "current_step":"Составить план подготовки",
        "expected_action":"Сохранить и подтвердить список",
    },"Начинаем сборы в поездку")
    assert defined["action"]["outcome"]=="accepted" and store.task()["phase"]=="planning"

    early=run_tool("request_transition",{
        "event":"advance","next_current_step":"Собирать чемодан","next_expected_action":"Проверить сборы",
    },"Сразу переходи к сбору чемодана")
    assert early["action"]["outcome"]=="rejected"
    assert early["action"]["reason_code"]=="unmet_prerequisites"
    assert store.task()["phase"]=="planning"

    assert store.task()["plan_approved"] is False
    plan_approval=defined["pending_approval"]
    assert client.post(f"/api/agent/approvals/{plan_approval['id']}/confirm").status_code==200
    assert store.task()["plan_approved"] is True

    to_execution=run_tool("request_transition",{
        "event":"advance","next_current_step":"Собрать чемодан по списку",
        "next_expected_action":"Сохранить результат сборов",
    },"Переходи к выполнению")
    assert to_execution["action"]["outcome"]=="accepted" and store.task()["phase"]=="execution"

    executed=run_tool("save_current_evidence",{"content":private_result},"Сохрани результат сборов")
    execution_approval=executed["pending_approval"]
    assert store.task()["execution_result"]==private_result
    assert store.task()["blocking_issues"]==""
    assert client.post(f"/api/agent/approvals/{execution_approval['id']}/confirm").status_code==200
    before_pause={key:store.task()[key] for key in (
        "phase","current_step","expected_action","plan_artifact","plan_approved",
        "execution_result","implementation_ready","blocking_issues",
    )}

    paused=run_tool("pause_task",{},"Поставь сборы на паузу")
    assert paused["action"]["outcome"]=="accepted" and store.task()["run_status"]=="paused"
    resumed=run_tool("resume_task",{},"Продолжи сборы")
    assert resumed["action"]["outcome"]=="accepted" and store.task()["run_status"]=="active"
    for key,value in before_pause.items():
        assert store.task()[key]==value

    to_validation=run_tool("request_transition",{
        "event":"advance","next_current_step":"Проверить чемодан",
        "next_expected_action":"Зафиксировать замечания проверки",
    },"Переходи к финальной проверке")
    assert to_validation["action"]["outcome"]=="accepted" and store.task()["phase"]=="validation"

    blocked_evidence=run_tool("record_validation_result",{
        "has_blockers":True,"blocking_issues":private_blocker,
    },"Сохрани найденную проблему")
    assert blocked_evidence["pending_approval"] is None
    assert store.task()["blocking_issues"]==private_blocker
    assert store.task()["execution_result"]==private_result
    blocked_done=run_tool("request_transition",{
        "event":"approve","next_current_step":"Завершено","next_expected_action":"Нет",
    },"Заверши сборы")
    assert blocked_done["action"]["outcome"]=="rejected"
    assert {item["code"] for item in blocked_done["action"]["unmet_requirements"]}=={
        "validation_not_passed","blocking_issues_present",
    }
    assert store.task()["phase"]=="validation"

    cleared=run_tool("record_validation_result",{
        "has_blockers":False,"blocking_issues":"",
    },"Паспорт положен, блокирующих замечаний больше нет")
    validation_approval=cleared["pending_approval"]
    assert client.post(f"/api/agent/approvals/{validation_approval['id']}/confirm").status_code==200
    assert store.task()["validation_passed"] is True and store.task()["blocking_issues"]==""

    done=run_tool("request_transition",{
        "event":"approve","next_current_step":"Поездка готова","next_expected_action":"Нет",
    },"Подтверди завершение сборов")
    assert done["action"]["outcome"]=="accepted" and store.task()["phase"]=="done"

    attempts=store.task_transition_attempts(limit=20)
    events=store.task_transition_events(limit=20)
    assert sum(item["outcome"]=="accepted" for item in attempts)==5
    assert sum(item["outcome"]=="rejected" for item in attempts)==2
    assert len(events)==5 and all(item["event"] in {"advance","pause","resume","approve"} for item in events)
    serialized_actions=json.dumps(store.agent_actions(limit=50),ensure_ascii=False)
    assert private_plan not in serialized_actions
    assert private_result not in serialized_actions
    assert private_blocker not in serialized_actions


def test_structured_no_action_is_required_and_natural_completion_has_no_tools(bundle):
    store,fake,client=bundle
    fake.chat.completions.answers=[
        tool_answer("respond_without_action",{}),
        "Сейчас мы составляем план поездки. Расскажите, что хотите взять с собой.",
    ]

    response=agent_ask(client,"Что сейчас происходит?")

    assert response.status_code==200 and response.json()["action"] is None
    assert response.json()["answer"].startswith("Сейчас мы составляем план")
    assert store.agent_actions()==[] and len(fake.chat.completions.calls)==2
    assert fake.chat.completions.calls[0]["tool_choice"]=="required"
    assert "tools" in fake.chat.completions.calls[0]
    assert "tools" not in fake.chat.completions.calls[1] and "tool_choice" not in fake.chat.completions.calls[1]


def test_natural_intro_defines_task_with_proposed_plan_pending_and_natural_reply(bundle):
    store,fake,client=bundle
    proposed_plan="Паспорт и билеты; одежда и дождевик; лекарства; зарядка"
    fake.chat.completions.answers=[
        tool_answer("define_task",{
            "goal":"Собраться вчетвером в лес на два дня",
            "hard_constraints":"Не забыть документы и лекарства",
            "task_data":"Четыре человека, прогноз с дождём",
            "open_questions":"",
            "proposed_plan":proposed_plan,
            "current_step":"Согласовать план подготовки",
            "expected_action":"Получить оценку предложенного списка",
        }),
        "Я сохранил задачу и подготовил список. Посмотрите, всё ли вам подходит; после вашего явного согласия продолжим сборы.",
    ]

    response=agent_ask(client,"Привет! Мы вчетвером хотим на два дня поехать в лес.")

    body=response.json();task=store.task()
    assert response.status_code==200 and body["action"]["outcome"]=="accepted"
    assert task["goal"]=="Собраться вчетвером в лес на два дня" and task["plan_artifact"]==proposed_plan
    assert task["plan_approved"] is False
    assert body["pending_approval"]["kind"]=="plan_approved"
    assert body["pending_approval"]["display_summary"]=="Подтвердить предложенный план подготовки"
    assert "явного согласия" in body["answer"]
    post_call=fake.chat.completions.calls[1]
    assert "tools" not in post_call
    post_text="\n".join(message["content"] for message in post_call["messages"])
    assert '"revision": 1' in post_text and '"plan_artifact":' in post_text


def test_natural_consent_atomically_confirms_pending_and_advances(bundle):
    store,fake,client=bundle
    fake.chat.completions.answers=[
        tool_answer("define_task",{
            "goal":"Собрать чемодан","proposed_plan":"Паспорт, одежда, лекарства, зарядка",
            "current_step":"Согласовать список","expected_action":"Получить согласие",
        }),"Посмотрите предложенный список.",
    ]
    defined=agent_ask(client,"Помоги спланировать поездку").json()
    revision=store.task()["revision"]
    pending_id=defined["pending_approval"]["id"]
    fake.chat.completions.answers=[
        tool_answer("accept_current_evidence_and_advance",{
            "next_current_step":"Собрать чемодан по списку","next_expected_action":"Сообщить результат сборов",
        }),"Отлично, список принят. Теперь собираем чемодан по нему.",
    ]

    accepted=agent_ask(client,"План мне нравится. Давай переходить к подготовке и начнём собирать вещи.").json()

    task=store.task()
    assert accepted["action"]["outcome"]=="accepted"
    assert accepted["action"]["safe_details"]["consent"]=="explicit"
    assert task["plan_approved"] is True and task["phase"]=="execution" and task["revision"]==revision+2
    pending=next(item for item in store.pending_agent_approvals() if item["id"]==pending_id)
    assert pending["status"]=="confirmed"
    assert len(store.task_transition_attempts())==1 and store.task_transition_attempts()[0]["outcome"]=="accepted"
    assert len(store.task_transition_events())==1


def define_and_manually_confirm_plan(store,fake,client):
    fake.chat.completions.answers=[tool_answer("define_task",{
        "goal":"Подготовиться к поездке","proposed_plan":"Проверить документы, еду и снаряжение",
        "current_step":"Согласовать план подготовки","expected_action":"Получить решение пользователя",
    }),"Проверьте предложенный план подготовки."]
    defined=agent_ask(client,"Помоги подготовиться к поездке").json()
    pending=defined["pending_approval"]
    confirmed=client.post(f"/api/agent/approvals/{pending['id']}/confirm")
    assert confirmed.status_code==200 and confirmed.json()["outcome"]=="confirmed"
    assert store.task()["plan_approved"] is True and store.pending_agent_approvals(pending_only=True)==[]
    return pending


def test_manual_confirm_then_natural_continue_uses_transition_context_and_reaches_execution(bundle):
    store,fake,client=bundle
    define_and_manually_confirm_plan(store,fake,client)
    fake.chat.completions.answers=[tool_answer("request_transition",{
        "event":"advance","next_current_step":"Собрать всё по плану",
        "next_expected_action":"Рассказать о результате подготовки",
    }),"План уже подтверждён, переходим к подготовке."]

    response=agent_ask(client,"Подтверждаю план, переходим к реализации")

    assert response.status_code==200 and response.json()["action"]["outcome"]=="accepted"
    assert store.task()["phase"]=="execution"
    assert len(store.task_transition_attempts())==1 and store.task_transition_attempts()[0]["outcome"]=="accepted"
    assert len(store.task_transition_events())==1
    decision_messages=fake.chat.completions.calls[-2]["messages"]
    decision_context=next(item["content"] for item in decision_messages if "<LIFECYCLE_DECISION_CONTEXT" in item["content"])
    assert '"current_pending": []' in decision_context
    assert '"event": "advance"' in decision_context and '"enabled": true' in decision_context
    assert "выбери request_transition" in decision_context
    assert "<TASK_STATE_MACHINE" in decision_messages[-2]["content"]


def test_manual_diagnostics_cannot_approve_plan_with_blocking_questions(bundle):
    store,_,client=bundle
    assert client.put("/api/task",json={
        "goal":"Поездка","open_questions":"Кто отвечает за лекарства?",
        "current_step":"Уточнить ответственность","expected_action":"Ответить на вопрос",
    }).status_code==200
    assert save_evidence(client,plan_artifact="Черновой план поездки").status_code==200
    assert store.task()["planning_ready"] is False

    rejected=decide(client,"plan_approved")

    assert rejected.status_code==409
    assert store.task()["plan_approved"] is False and store.task()["phase"]=="planning"


def test_agent_transition_uses_phase_checkpoint_not_model_planning_checkpoint(bundle):
    store,fake,client=bundle
    define_and_manually_confirm_plan(store,fake,client)
    leaked="Снова уточнить даты, регион, расход топлива и тип горелки"
    fake.chat.completions.answers=[tool_answer("request_transition",{
        "event":"advance","next_current_step":leaked,"next_expected_action":leaked,
    }),"Переходим к выполнению."]

    response=agent_ask(client,"Переходим к реализации").json()

    assert response["action"]["outcome"]=="accepted"
    assert store.task()["phase"]=="execution"
    assert store.task()["current_step"]=="Выполнить утверждённый план подготовки."
    assert store.task()["expected_action"]=="Сообщить, что выполнено и какие возникли препятствия."
    assert leaked not in json.dumps(store.task(),ensure_ascii=False)


def test_phase_policy_prevents_question_loops_and_forest_prompt_is_ready_contract(bundle):
    store,fake,client=bundle
    fake.chat.completions.answers=[tool_answer("respond_without_action",{}),"Сформирую план с допущениями."]
    response=agent_ask(client,"Мы вчетвером едем на машине в лес на два дня, бюджет 12000, возможен дождь.")
    assert response.status_code==200
    decision="\n".join(item["content"] for item in fake.chat.completions.calls[0]["messages"])
    assert "2–3" in decision and "planning_ready=true" in decision
    assert "точной дате, регионе, расходе топлива или типе горелки" in decision

    with store.connection() as connection:
        connection.execute("UPDATE tasks SET phase='execution' WHERE id=1")
    fake.chat.completions.answers=[tool_answer("respond_without_action",{}),"Продолжаем выполнение."]
    agent_ask(client,"Что дальше?")
    execution_prompt="\n".join(item["content"] for item in fake.chat.completions.calls[-2]["messages"])
    assert "Не собирай заново даты, маршрут, бюджет, снаряжение" in execution_prompt


@pytest.mark.parametrize(("phrase","reason_code"),[
    ("План нравится?","transition_intent_ambiguous"),
    ("Мне нравится погода","transition_intent_missing"),
    ("Если план готов, переходим к реализации","transition_intent_conditional"),
    ("Не переходим к реализации","transition_intent_negated"),
    ("Не надо переходить к реализации","transition_intent_negated"),
    ("Не хочу переходить к реализации","transition_intent_negated"),
    ("Давайте пока не будем переходить к реализации","transition_intent_negated"),
    ("Нам не следует переходить к реализации","transition_intent_negated"),
    ("Пока не нужно переходить к реализации","transition_intent_negated"),
    ("Я не готов переходить к реализации","transition_intent_negated"),
])
def test_agent_request_transition_requires_explicit_fail_closed_user_intent_after_manual_confirm(
    bundle,phrase,reason_code
):
    store,fake,client=bundle
    define_and_manually_confirm_plan(store,fake,client)
    before=store.task().copy()
    fake.chat.completions.answers=[tool_answer("request_transition",{
        "event":"advance","next_current_step":"Собрать всё по плану",
        "next_expected_action":"Рассказать о результате подготовки",
    }),"Нужна однозначная просьба перейти дальше."]

    response=agent_ask(client,phrase).json()

    assert response["action"]["outcome"]=="rejected"
    assert response["action"]["reason_code"]==reason_code
    assert store.task()==before and store.task()["phase"]=="planning"
    assert store.task_transition_attempts()==[] and store.task_transition_events()==[]
    rejected=[item for item in store.agent_actions() if item["action"]=="request_transition"]
    assert len(rejected)==1 and rejected[0]["safe_details"]=={"event":"advance"}


def test_composite_without_pending_uses_existing_manual_approval_once_and_is_idempotent(bundle):
    store,fake,client=bundle
    define_and_manually_confirm_plan(store,fake,client)
    request_id=str(uuid.uuid4())
    fake.chat.completions.answers=[tool_answer("accept_current_evidence_and_advance",{
        "next_current_step":"Собрать всё по плану","next_expected_action":"Рассказать о результате",
    }),"План уже был подтверждён, переходим к подготовке."]

    first=agent_ask(client,"Подтверждаю план, переходим к реализации",request_id)
    replay=agent_ask(client,"Подтверждаю план, переходим к реализации",request_id)

    assert first.status_code==200 and first.json()["action"]["outcome"]=="accepted"
    assert first.json()["action"]["safe_details"]["approval_source"]=="existing"
    assert replay.status_code==200 and replay.json()["idempotent_replay"] is True
    assert store.task()["phase"]=="execution"
    assert len(store.task_transition_attempts())==1
    assert len(store.task_transition_events())==1
    composite=[item for item in store.agent_actions() if item["action"]=="accept_current_evidence_and_advance"]
    assert len(composite)==1 and composite[0]["outcome"]=="accepted"
    assert len(fake.chat.completions.calls)==4


@pytest.mark.parametrize("phrase",[
    "Мне нравится погода, давай дальше гулять",
    "План мне не нравится, давай переходить к подготовке",
])
def test_composite_without_pending_rejects_irrelevant_or_negative_consent_after_manual_confirm(bundle,phrase):
    store,fake,client=bundle
    define_and_manually_confirm_plan(store,fake,client)
    before=store.task().copy()
    fake.chat.completions.answers=[tool_answer("accept_current_evidence_and_advance",{
        "next_current_step":"Собрать всё по плану","next_expected_action":"Рассказать о результате",
    }),"Нужно однозначно принять план и попросить продолжить."]

    response=agent_ask(client,phrase).json()

    assert response["action"]["outcome"]=="rejected"
    assert response["action"]["reason_code"].startswith("consent_")
    assert store.task()==before
    assert store.task_transition_attempts()==[] and store.task_transition_events()==[]


def test_composite_without_pending_still_runs_guards_when_approval_flag_is_true(bundle):
    store,fake,client=bundle
    with store.connection() as connection:
        connection.execute("UPDATE tasks SET plan_artifact='',planning_ready=1,plan_approved=1 WHERE id=1")
    before=store.task().copy()
    fake.chat.completions.answers=[tool_answer("accept_current_evidence_and_advance",{
        "next_current_step":"Начать подготовку","next_expected_action":"Сообщить результат",
    }),"Сначала нужен сохранённый план подготовки."]

    response=agent_ask(client,"Подтверждаю план, переходим к реализации").json()

    assert response["action"]["outcome"]=="rejected"
    assert response["action"]["reason_code"]=="unmet_prerequisites"
    assert {item["code"] for item in response["action"]["unmet_requirements"]}=={"plan_artifact_missing"}
    assert store.task()==before and store.task()["phase"]=="planning"
    assert len(store.task_transition_attempts())==1 and store.task_transition_attempts()[0]["outcome"]=="rejected"
    assert store.task_transition_events()==[]


def reach_validation(client):
    assert guarded_move(client,"advance",current_step="Выполнить план",expected_action="Сохранить результат").status_code==200
    assert guarded_move(client,"advance",current_step="Провести проверку",expected_action="Сообщить проблемы").status_code==200


def test_typed_validation_result_with_blockers_saves_private_text_without_pending_or_audit_leak(bundle):
    store,fake,client=bundle
    reach_validation(client)
    private_blocker="PRIVATE_TYPED_BLOCKER: не хватает паспорта"
    fake.chat.completions.answers=[tool_answer("record_validation_result",{
        "has_blockers":True,"blocking_issues":private_blocker,
    }),"Проблема сохранена; сначала возьмите паспорт."]

    response=agent_ask(client,"На проверке выяснилось, что паспорт остался дома").json()

    assert response["action"]["outcome"]=="accepted"
    assert response["action"]["safe_details"]=={"evidence_kind":"blocking_issues","has_blockers":True}
    assert store.task()["blocking_issues"]==private_blocker and store.task()["validation_passed"] is False
    assert response["pending_approval"] is None and store.pending_agent_approvals(pending_only=True)==[]
    assert private_blocker not in json.dumps(store.agent_actions(),ensure_ascii=False)


def test_typed_clear_replaces_pending_once_and_idempotent_retry_does_not_duplicate(bundle):
    store,fake,client=bundle
    reach_validation(client)
    save_evidence(client,blocking_issues="Паспорт отсутствует")
    request_id=str(uuid.uuid4())
    typed_clear=tool_answer("record_validation_result",{"has_blockers":False,"blocking_issues":""})
    fake.chat.completions.answers=[typed_clear,"Проблем больше нет; подтвердите финальную проверку."]

    first=agent_ask(client,"Паспорт забрали, блокирующих проблем больше нет",request_id).json()
    replay=agent_ask(client,"Паспорт забрали, блокирующих проблем больше нет",request_id).json()

    first_pending=first["pending_approval"]
    assert first_pending["kind"]=="validation_passed" and first_pending["status"]=="pending"
    assert replay["idempotent_replay"] is True
    assert store.task()["blocking_issues"]=="" and store.task()["validation_passed"] is False
    assert len(store.pending_agent_approvals(pending_only=True))==1
    revision_after_first=store.task()["revision"]
    fake.chat.completions.answers=[typed_clear,"Результат проверки обновлён."]
    replacement=agent_ask(client,"Повторно проверили: проблем по-прежнему нет").json()["pending_approval"]
    all_pending=store.pending_agent_approvals()
    assert replacement["id"]!=first_pending["id"] and replacement["status"]=="pending"
    assert next(item for item in all_pending if item["id"]==first_pending["id"])["status"]=="expired"
    assert len(store.pending_agent_approvals(pending_only=True))==1
    assert store.task()["revision"]==revision_after_first+1


@pytest.mark.parametrize("arguments,reason_code",[
    ({"has_blockers":True,"blocking_issues":""},"invalid_validation_result"),
    ({"has_blockers":False,"blocking_issues":"Проблема всё ещё есть"},"invalid_validation_result"),
    ({"has_blockers":"false","blocking_issues":""},"invalid_arguments"),
])
def test_typed_validation_result_rejects_invalid_boolean_text_combinations_without_mutation(
    bundle,arguments,reason_code
):
    store,fake,client=bundle
    reach_validation(client)
    before=store.task().copy()
    fake.chat.completions.answers=[tool_answer("record_validation_result",arguments),"Результат не сохранён."]

    response=agent_ask(client,"Сохрани результат проверки").json()

    assert response["action"]["outcome"]=="rejected"
    assert response["action"]["reason_code"]==reason_code
    assert store.task()==before and store.pending_agent_approvals(pending_only=True)==[]


def test_generic_agent_evidence_is_rejected_in_validation_without_mutation(bundle):
    store,fake,client=bundle
    reach_validation(client)
    before=store.task().copy()
    fake.chat.completions.answers=[tool_answer("save_current_evidence",{"content":"Блокирующих проблем нет"}),
                                  "Нужно явно указать, остались ли проблемы."]

    response=agent_ask(client,"Проверка завершена, проблем нет").json()

    assert response["action"]["outcome"]=="rejected"
    assert response["action"]["reason_code"]=="validation_requires_typed_result"
    assert store.task()==before and store.pending_agent_approvals(pending_only=True)==[]


def test_typed_validation_result_wrong_phase_paused_and_stale_are_safe(bundle):
    store,fake,client=bundle
    arguments={"has_blockers":False,"blocking_issues":""}
    fake.chat.completions.answers=[tool_answer("record_validation_result",arguments),"Сначала нужен этап проверки."]
    wrong=agent_ask(client,"Проблем нет").json()
    assert wrong["action"]["reason_code"]=="wrong_phase" and store.task()["phase"]=="planning"

    reach_validation(client)
    assert move(client,"pause").status_code==200
    paused_before=store.task().copy()
    fake.chat.completions.answers=[tool_answer("record_validation_result",arguments),"Сначала продолжите задачу."]
    paused=agent_ask(client,"Проблем нет").json()
    assert paused["action"]["reason_code"]=="task_paused" and store.task()==paused_before

    assert move(client,"resume").status_code==200
    stale_revision=store.task()["revision"]-1
    stale=store.execute_agent_action(
        str(uuid.uuid4()),"call-stale",store.active_session()["id"],"record_validation_result",arguments,
        stale_revision,"Проблем нет",
    )
    assert stale["outcome"]=="rejected" and stale["reason_code"]=="stale_revision"
    assert store.task()["blocking_issues"]==""


def test_manual_validation_clear_has_explicit_confirmation_fallback_without_pending(bundle):
    store,fake,client=bundle
    reach_validation(client)
    assert save_evidence(client,blocking_issues="Нужно забрать паспорт").status_code==200
    assert save_evidence(client,blocking_issues="").status_code==200
    assert store.pending_agent_approvals(pending_only=True)==[] and store.task()["validation_passed"] is False
    assert decide(client,"validation_passed").status_code==200
    assert store.task()["validation_passed"] is True
    fake.chat.completions.answers=[tool_answer("request_transition",{
        "event":"approve","next_current_step":"Подготовка завершена","next_expected_action":"Можно выезжать",
    }),"Переходим к завершению."]
    completed=agent_ask(client,"Прошу перейти на следующий шаг").json()
    assert completed["action"]["outcome"]=="accepted" and store.task()["phase"]=="done"


@pytest.mark.parametrize("phrase",[
    "Не прошу перейти на следующий шаг",
    "Я не просил перейти на следующий шаг",
    "Я не просила перейти на следующий шаг",
    "Мы не просим перейти на следующий шаг",
])
def test_validation_request_transition_rejects_negated_request_verb_without_mutation(bundle,phrase):
    store,fake,client=bundle
    reach_validation(client)
    assert decide(client,"validation_passed").status_code==200
    before=store.task().copy()
    attempts_before=len(store.task_transition_attempts())
    events_before=len(store.task_transition_events())
    fake.chat.completions.answers=[tool_answer("request_transition",{
        "event":"approve","next_current_step":"Подготовка завершена","next_expected_action":"Можно выезжать",
    }),"Без явной просьбы состояние не изменяется."]

    response=agent_ask(client,phrase).json()

    assert response["action"]["outcome"]=="rejected"
    assert response["action"]["reason_code"]=="transition_intent_negated"
    assert store.task()==before and store.task()["phase"]=="validation"
    assert len(store.task_transition_attempts())==attempts_before
    assert len(store.task_transition_events())==events_before


def test_current_validation_shape_typed_clear_pending_natural_confirm_reaches_done(bundle):
    store,fake,client=bundle
    reach_validation(client)
    positive_but_misclassified=(
        "Проверены документы, аптечка, еда и снаряжение. Все пункты закрыты; "
        "блокирующих замечаний нет."
    )
    assert save_evidence(client,blocking_issues=positive_but_misclassified).status_code==200
    assert store.task()["phase"]=="validation" and store.task()["blocking_issues"]==positive_but_misclassified
    fake.chat.completions.answers=[tool_answer("record_validation_result",{
        "has_blockers":False,"blocking_issues":"",
    }),"Проверка сохранена без проблем; подтвердите завершение."]

    cleared=agent_ask(client,"Все пункты закрыты, блокирующих замечаний нет").json()

    assert cleared["action"]["outcome"]=="accepted"
    assert store.task()["blocking_issues"]=="" and store.task()["validation_passed"] is False
    assert cleared["pending_approval"]["kind"]=="validation_passed"
    assert positive_but_misclassified not in json.dumps(store.agent_actions(),ensure_ascii=False)
    fake.chat.completions.answers=[tool_answer("accept_current_evidence_and_advance",{
        "next_current_step":"Подготовка завершена","next_expected_action":"Можно выезжать",
    }),"Проверка подтверждена, подготовка завершена."]
    done=agent_ask(client,"Проверка пройдена, всё верно, давай завершать").json()
    assert done["action"]["outcome"]=="accepted" and store.task()["phase"]=="done"


@pytest.mark.parametrize("phrase",[
    "План мне не нравится, давай дальше",
    "План нравится, давай дальше?",
    "Если план подходит, давай дальше",
    "Он сказал: «план нравится, давай дальше»",
    "Мне нравится погода, давай дальше гулять",
    "ок",
])
def test_natural_consent_gate_fails_closed(bundle,phrase):
    store,fake,client=bundle
    fake.chat.completions.answers=[tool_answer("define_task",{
        "goal":"Собрать чемодан","proposed_plan":"Паспорт и одежда",
        "current_step":"Согласовать список","expected_action":"Получить согласие",
    }),"Проверьте список."]
    agent_ask(client,"Составь список")
    before=store.task().copy()
    fake.chat.completions.answers=[tool_answer("accept_current_evidence_and_advance",{
        "next_current_step":"Собрать чемодан","next_expected_action":"Сообщить результат",
    }),"Нужно более явное подтверждение."]

    response=agent_ask(client,phrase).json()

    assert response["action"]["outcome"]=="rejected"
    assert response["action"]["reason_code"].startswith("consent_")
    assert store.task()==before and store.task()["plan_approved"] is False
    assert store.task_transition_attempts()==[] and store.task_transition_events()==[]
    assert store.pending_agent_approvals()[0]["status"]=="pending"


def test_natural_consent_without_current_pending_or_with_stale_pending_has_no_partial_mutation(bundle):
    store,fake,client=bundle
    consent_tool=tool_answer("accept_current_evidence_and_advance",{
        "next_current_step":"Собрать чемодан","next_expected_action":"Сообщить результат",
    })
    fake.chat.completions.answers=[consent_tool,"Сначала нужен согласованный список."]
    no_pending=agent_ask(client,"План мне нравится, давай дальше").json()
    assert no_pending["action"]["reason_code"]=="pending_approval_missing"
    assert store.task()["revision"]==0 and store.task_transition_attempts()==[]

    fake.chat.completions.answers=[tool_answer("define_task",{
        "goal":"Собрать чемодан","proposed_plan":"Паспорт и одежда",
        "current_step":"Согласовать список","expected_action":"Получить согласие",
    }),"Проверьте список."]
    agent_ask(client,"Составь список")
    client.put("/api/task",json={"goal":"Изменённая задача","current_step":"Новый шаг","expected_action":"Новое действие"})
    before=store.task().copy()
    fake.chat.completions.answers=[consent_tool,"Подтверждение устарело."]
    stale=agent_ask(client,"План мне нравится, давай дальше").json()
    assert stale["action"]["reason_code"]=="pending_approval_missing"
    assert store.task()==before and store.task()["plan_approved"] is False
    assert store.task_transition_attempts()==[]


def test_composite_consent_transaction_rolls_back_on_internal_fault(bundle,monkeypatch):
    store,fake,client=bundle
    fake.chat.completions.answers=[tool_answer("define_task",{
        "goal":"Собрать чемодан","proposed_plan":"Паспорт и одежда",
        "current_step":"Согласовать список","expected_action":"Получить согласие",
    }),"Проверьте список."]
    agent_ask(client,"Составь список")
    before=store.task().copy();pending_before=store.pending_agent_approvals()[0].copy()
    original=store._agent_action_row

    def fail_composite(*args,**kwargs):
        action=args[4]
        if action=="accept_current_evidence_and_advance":
            raise StaleTaskStateError("Имитированный сбой внутри composite transaction")
        return original(*args,**kwargs)

    monkeypatch.setattr(store,"_agent_action_row",fail_composite)
    fake.chat.completions.answers=[tool_answer("accept_current_evidence_and_advance",{
        "next_current_step":"Собрать чемодан","next_expected_action":"Сообщить результат",
    })]
    response=agent_ask(client,"План мне нравится, давай дальше")

    assert response.status_code==409
    assert store.task()==before
    assert store.pending_agent_approvals()[0]["status"]==pending_before["status"]=="pending"
    assert store.task_transition_attempts()==[] and store.task_transition_events()==[]
    assert all(item["action"]!="accept_current_evidence_and_advance" for item in store.agent_actions())


def test_post_action_provider_failure_uses_natural_fallback_without_repeating_action(bundle):
    store,fake,client=bundle
    request_id=str(uuid.uuid4())
    fake.chat.completions.answers=[
        tool_answer("define_task",{
            "goal":"Собрать чемодан","proposed_plan":"Паспорт и одежда",
            "current_step":"Согласовать список","expected_action":"Получить согласие",
        }),
        RuntimeError("post-action provider unavailable"),
    ]

    first=agent_ask(client,"Помоги со сборами",request_id)
    revision=store.task()["revision"]
    replay=agent_ask(client,"Не выполнять повторно",request_id)

    assert first.status_code==200 and first.json()["action"]["outcome"]=="accepted"
    assert "предложенный план" in first.json()["answer"].lower()
    assert first.json()["preview"]["post_action_error"]
    assert replay.status_code==200 and replay.json()["idempotent_replay"] is True
    assert len(fake.chat.completions.calls)==2 and len(store.agent_actions())==1
    assert store.task()["revision"]==revision


def test_decision_context_places_fresh_authoritative_snapshot_after_history(bundle):
    store,fake,client=bundle
    session=store.active_session()
    store.append_turn(session["id"],"Старое утверждение","Задача якобы уже done",{},store.task())
    fake.chat.completions.answers=[tool_answer("respond_without_action",{}),"На самом деле мы ещё планируем."]

    response=agent_ask(client,"Где мы сейчас?")

    assert response.status_code==200
    decision_messages=fake.chat.completions.calls[0]["messages"]
    history_index=next(index for index,item in enumerate(decision_messages) if "якобы уже done" in item["content"])
    snapshot_index=max(index for index,item in enumerate(decision_messages) if "<TASK_STATE_MACHINE" in item["content"])
    assert snapshot_index>history_index
    assert '"phase": "planning"' in decision_messages[snapshot_index]["content"]


def test_natural_completion_has_relevant_history_and_task_context_but_fresh_snapshot_is_last(bundle):
    store,fake,client=bundle
    client.put("/api/task",json={
        "goal":"Подготовить поездку в лес",
        "hard_constraints":"Не забыть лекарства",
        "task_data":"Едут четыре человека",
        "open_questions":"Кто берёт аптечку",
        "current_step":"Составить план",
        "expected_action":"Обсудить список",
    })
    store.add_long_term("knowledge","PRIVATE_UNRELATED_LONG_TERM")
    session=store.active_session()
    store.append_turn(session["id"],"У нас уже есть палатка","Учту палатку в подготовке",{},store.task())
    fake.chat.completions.answers=[tool_answer("respond_without_action",{}),"Сначала согласуем список для поездки."]

    response=agent_ask(client,"Что делать дальше?")

    assert response.status_code==200
    post_messages=fake.chat.completions.calls[1]["messages"]
    joined="\n".join(item["content"] for item in post_messages)
    assert "Подготовить поездку в лес" in joined
    assert "Не забыть лекарства" in joined
    assert "Едут четыре человека" in joined
    assert "Кто берёт аптечку" in joined
    assert "У нас уже есть палатка" in joined and "Учту палатку" in joined
    assert "PRIVATE_UNRELATED_LONG_TERM" not in joined
    assert "Безопасный результат серверного действия" in post_messages[-3]["content"]
    assert "<TASK_STATE_MACHINE" in post_messages[-2]["content"]
    assert post_messages[-1]=={"role":"user","content":"Что делать дальше?"}


def test_composite_consent_rejects_model_controlled_authority_fields(bundle):
    store,fake,client=bundle
    fake.chat.completions.answers=[tool_answer("define_task",{
        "goal":"Собрать чемодан","proposed_plan":"Паспорт и одежда",
        "current_step":"Согласовать список","expected_action":"Получить согласие",
    }),"Проверьте список."]
    agent_ask(client,"Составь список")
    before=store.task().copy()
    fake.chat.completions.answers=[tool_answer("accept_current_evidence_and_advance",{
        "next_current_step":"Собрать чемодан","next_expected_action":"Сообщить результат",
        "approved":True,"target":"done","revision":before["revision"],"session_id":999,
    }),"Нужно подтвердить список без изменения служебных полей."]

    response=agent_ask(client,"План мне нравится, давай дальше").json()

    assert response["action"]["outcome"]=="rejected"
    assert response["action"]["reason_code"]=="invalid_arguments"
    assert store.task()==before and store.task_transition_attempts()==[]
    assert store.pending_agent_approvals()[0]["status"]=="pending"


def test_composite_consent_cannot_use_pending_from_another_profile_session(bundle):
    store,fake,client=bundle
    fake.chat.completions.answers=[tool_answer("define_task",{
        "goal":"Собрать чемодан","proposed_plan":"Паспорт и одежда",
        "current_step":"Согласовать список","expected_action":"Получить согласие",
    }),"Проверьте список."]
    defined=agent_ask(client,"Составь список").json()
    original_pending_id=defined["pending_approval"]["id"]
    expert=next(profile for profile in store.profiles() if profile["name"]=="Эксперт")
    client.post(f"/api/profiles/{expert['id']}/activate")
    before=store.task().copy()
    fake.chat.completions.answers=[tool_answer("accept_current_evidence_and_advance",{
        "next_current_step":"Собрать чемодан","next_expected_action":"Сообщить результат",
    }),"В этой сессии нет списка, который можно подтвердить."]

    response=agent_ask(client,"План мне нравится, давай дальше").json()

    assert response["action"]["outcome"]=="rejected"
    assert response["action"]["reason_code"]=="pending_approval_missing"
    assert store.task()==before and store.task_transition_attempts()==[]
    with store.connection() as connection:
        pending=connection.execute(
            "SELECT status FROM pending_agent_approvals WHERE id=?",(original_pending_id,)
        ).fetchone()
    assert pending["status"]=="pending"


def test_full_natural_agent_dialogue_reaches_done_with_guarded_failures_and_private_audit(bundle):
    store,fake,client=bundle

    def converse(tool,arguments,prompt,natural_answer):
        fake.chat.completions.answers=[tool_answer(tool,arguments),natural_answer]
        response=agent_ask(client,prompt)
        assert response.status_code==200
        return response.json()

    private_plan="PRIVATE_NATURAL_PLAN: паспорт, билеты, лекарства, дождевик"
    private_result="PRIVATE_NATURAL_RESULT: вещи собраны и вес проверен"
    private_blocker="PRIVATE_NATURAL_BLOCKER: паспорт остался на столе"

    defined=converse("define_task",{
        "goal":"Собраться вчетвером в лес на два дня",
        "hard_constraints":"Не забыть документы и лекарства",
        "task_data":"Четыре человека, прогноз с дождём",
        "open_questions":"",
        "proposed_plan":private_plan,
        "current_step":"Согласовать план подготовки",
        "expected_action":"Получить оценку списка",
    },"Привет! Мы вчетвером хотим на два дня поехать в лес.",
      "Я подготовил список. Посмотрите, всё ли подходит.")
    assert defined["action"]["outcome"]=="accepted"
    assert store.task()["phase"]=="planning" and store.task()["plan_approved"] is False

    skipped=converse("request_transition",{
        "event":"advance","next_current_step":"Собирать вещи",
        "next_expected_action":"Сообщить, что собрано",
    },"Давай сразу собираться, план потом уточним.",
      "Пока рано собираться: сначала нужно согласовать список.")
    assert skipped["action"]["outcome"]=="rejected"
    assert skipped["action"]["reason_code"]=="unmet_prerequisites"
    assert store.task()["phase"]=="planning"

    planned=converse("accept_current_evidence_and_advance",{
        "next_current_step":"Собрать вещи по списку",
        "next_expected_action":"Рассказать, что уже собрано",
    },"План мне нравится, давай выполнять дальше.",
      "Список принят. Теперь собираем вещи по нему.")
    assert planned["action"]["outcome"]=="accepted"
    assert store.task()["phase"]=="execution" and store.task()["plan_approved"] is True

    checkpoint={key:store.task()[key] for key in (
        "phase","current_step","expected_action","plan_artifact","plan_approved",
    )}
    paused=converse("pause_task",{},"Давай ненадолго остановимся.","Хорошо, остановились на текущем месте.")
    assert paused["action"]["outcome"]=="accepted" and store.task()["run_status"]=="paused"
    resumed=converse("resume_task",{},"Продолжим сборы.","Продолжаем с сохранённого места.")
    assert resumed["action"]["outcome"]=="accepted" and store.task()["run_status"]=="active"
    assert all(store.task()[key]==value for key,value in checkpoint.items())

    saved_result=converse("save_current_evidence",{"content":private_result},
        "Чемоданы собраны, всё сверили со списком.",
        "Я сохранил результат. Проверьте, всё ли верно.")
    assert saved_result["pending_approval"]["kind"]=="implementation_ready"
    assert store.task()["execution_result"]==private_result

    validated=converse("accept_current_evidence_and_advance",{
        "next_current_step":"Проверить документы и чемоданы",
        "next_expected_action":"Сообщить о найденных проблемах",
    },"Результат сборов меня устраивает, давай дальше проверять.",
      "Результат принят. Теперь проведём последнюю проверку.")
    assert validated["action"]["outcome"]=="accepted"
    assert store.task()["phase"]=="validation" and store.task()["implementation_ready"] is True

    blocker=converse("record_validation_result",{"has_blockers":True,"blocking_issues":private_blocker},
        "На проверке выяснилось, что паспорт остался на столе.",
        "Я записал проблему. Сначала положите паспорт.")
    assert blocker["pending_approval"] is None and store.task()["blocking_issues"]==private_blocker
    blocked=converse("request_transition",{
        "event":"approve","next_current_step":"Поездка готова",
        "next_expected_action":"Можно выезжать",
    },"Давай уже заканчивать.","Пока закончить нельзя: паспорт ещё не положен.")
    assert blocked["action"]["outcome"]=="rejected"
    assert {item["code"] for item in blocked["action"]["unmet_requirements"]}=={
        "validation_not_passed","blocking_issues_present",
    }
    assert store.task()["phase"]=="validation"

    cleared=converse("record_validation_result",{"has_blockers":False,"blocking_issues":""},
        "Паспорт положили, других проблем нет.",
        "Проверка чистая. Подтвердите, что можно завершать.")
    assert cleared["pending_approval"]["kind"]=="validation_passed"
    done=converse("accept_current_evidence_and_advance",{
        "next_current_step":"Поездка готова",
        "next_expected_action":"Можно выезжать",
    },"Проверка пройдена, всё верно, давай завершать.","Всё проверено, можно выезжать.")
    assert done["action"]["outcome"]=="accepted"
    assert store.task()["phase"]=="done" and store.task()["validation_passed"] is True

    attempts=store.task_transition_attempts(limit=20)
    assert sum(item["outcome"]=="accepted" for item in attempts)==5
    assert sum(item["outcome"]=="rejected" for item in attempts)==2
    assert len(store.task_transition_events(limit=20))==5
    serialized_actions=json.dumps(store.agent_actions(limit=50),ensure_ascii=False)
    for private_value in (private_plan,private_result,private_blocker):
        assert private_value not in serialized_actions


def test_explicit_finish_is_server_attempt_even_if_model_would_save_a_blocker(bundle):
    store,fake,client=bundle
    assert guarded_move(client,"advance").status_code==200
    assert guarded_move(client,"advance").status_code==200
    assert save_evidence(client,blocking_issues="Паспорт забыт").status_code==200
    before=store.task().copy()
    # This is deliberately the wrong model action. The pre-tool server gate
    # must not execute it or let it hide the user's request to finish.
    fake.chat.completions.answers=[tool_answer("record_validation_result",{
        "has_blockers":False,"blocking_issues":"",
    })]

    response=agent_ask(client,"Хочу завершить подготовку и выезжать прямо сейчас.").json()

    assert response["action"]["action"]=="request_transition"
    assert response["action"]["outcome"]=="rejected"
    assert response["action"]["reason_code"]=="unmet_prerequisites"
    assert {item["code"] for item in response["action"]["unmet_requirements"]}=={
        "validation_not_passed","blocking_issues_present",
    }
    assert store.task()==before
    assert store.task_transition_attempts()[0]["requested_target"]=="done"
