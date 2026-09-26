"""Independent manual-chat calendar choice contracts, with synthetic sources only."""
from copy import deepcopy

import pytest

from backend.services import Service
from backend.store import Store


CALENDARS = [{"id": "cal-private", "name": "Личный"}, {"id": "cal-work", "name": "Рабочий"}]
START = "2099-10-01T10:00:00+06:00"
END = "2099-10-01T18:00:00+06:00"


def choose(prompt, calendars=CALENDARS):
    from backend.calendar_choice import resolve_calendar_selection
    return resolve_calendar_selection(prompt, deepcopy(calendars))


@pytest.mark.parametrize("prompt", ["Личный", "  ЛИЧНЫЙ  ", '"Личный"', "«Личный»",
    "Используй календарь «Личный»", "cal-private"])
def test_exact_affirmative_current_text_selects_the_named_calendar(prompt):
    assert choose(prompt) == "cal-private"


@pytest.mark.parametrize("prompt", ["", "Проверь мой календарь", "1", "первый",
    "Не используй календарь Личный", "Личный?", "Можно использовать Личный?",
    "Личный или Рабочий", "Используй Личный или Рабочий", "Лич", "unknown-calendar-id"])
def test_ambiguous_negative_question_numeric_and_unknown_text_do_not_select_any_calendar(prompt):
    assert choose(prompt) is None


def test_duplicate_normalized_names_require_an_exact_id():
    calendars = [*CALENDARS, {"id": "cal-private-duplicate", "name": "  ЛИЧНЫЙ  "}]
    assert choose("Личный", calendars) is None
    assert choose("Используй календарь Личный", calendars) is None
    assert choose("cal-private", calendars) == "cal-private"


def test_short_name_embedded_in_a_longer_exact_name_does_not_create_an_ambiguous_match():
    calendars = [*CALENDARS, {"id": "cal-longer", "name": "Мой Личный"}]
    assert choose("Мой Личный", calendars) == "cal-longer"
    assert choose("Используй календарь «Мой Личный»", calendars) == "cal-longer"


@pytest.mark.parametrize("prompt", ["Не пропустить", "«Не пропустить»", "Используй календарь «Не пропустить»"])
def test_negative_word_inside_the_literal_calendar_label_is_not_a_user_refusal(prompt):
    calendars = [*CALENDARS, {"id": "cal-reminders", "name": "Не пропустить"}]
    assert choose(prompt, calendars) == "cal-reminders"


@pytest.mark.parametrize("prompt", ["Личный.", "Личный!", "Используй календарь «Личный».", "Используй календарь Личный!"])
def test_terminal_sentence_punctuation_does_not_turn_a_positive_choice_into_an_unknown_name(prompt):
    assert choose(prompt) == "cal-private"


@pytest.fixture
def calendar_service(tmp_path):
    store = Store(tmp_path / "manual-calendar-choice.sqlite")
    class Provider:
        def __init__(self): self.items, self.busy_calls = deepcopy(CALENDARS), []
        def calendars(self): return deepcopy(self.items)
        def busy(self, calendar_id, start, end, timezone):
            self.busy_calls.append((calendar_id, start, end, timezone))
            return []
        def create(self, *args): pytest.fail("Calendar choice must never create an event")
    provider = Provider()
    return store, Service(store, calendar=provider), provider


def run(store, prompt, request_id="test-request", calendar_id=None):
    return store.new_run(request_id, request_id + "-fingerprint", prompt, calendar_id)[0]


def slots(service, calendars_id, calendar_id, run_id):
    return service.find_free_slots(calendars_id, calendar_id, START, END, 3, "10:00", "18:00", run_id)


def test_model_argument_cannot_choose_the_first_calendar_without_user_text(calendar_service):
    store, service, provider = calendar_service
    current = run(store, "Проверь мой календарь")
    found = service.list_calendars(current["id"])
    assert found["requires_selection"] and found["selected_calendar_id"] is None
    assert store.get("run", current["id"])["calendar_id"] is None
    with pytest.raises(ValueError): slots(service, found["calendars_id"], "cal-private", current["id"])
    assert provider.busy_calls == []


def test_current_manual_choice_is_persisted_and_wrong_model_calendar_is_refused(calendar_service):
    store, service, provider = calendar_service
    current = run(store, "Используй календарь «Рабочий»")
    found = service.list_calendars(current["id"])
    assert found["selected_calendar_id"] == "cal-work" and not found["requires_selection"]
    assert store.get("run", current["id"])["calendar_id"] == "cal-work"
    with pytest.raises(ValueError): slots(service, found["calendars_id"], "cal-private", current["id"])
    assert provider.busy_calls == []
    available = slots(service, found["calendars_id"], "cal-work", current["id"])
    assert available["calendar_id"] == "cal-work"
    assert len(provider.busy_calls) == 1
    assert store.all("plan") == [] and store.all("operation") == []


def test_previous_run_choice_and_history_never_select_calendar_for_new_unspecified_text(calendar_service):
    store, service, provider = calendar_service
    previous = run(store, "Личный", request_id="previous")
    service.list_calendars(previous["id"])
    store.put("message", {"id": "previous-answer", "run_id": previous["id"], "role": "assistant", "content": "Вы выбрали Личный"})
    current = run(store, "Найди свободное время", request_id="current")
    found = service.list_calendars(current["id"])
    assert found["requires_selection"] and found["selected_calendar_id"] is None
    assert store.get("run", current["id"])["calendar_id"] is None
    with pytest.raises(ValueError): slots(service, found["calendars_id"], "cal-private", current["id"])
    assert provider.busy_calls == []


def test_name_is_resolved_against_current_discovery_not_an_old_name_id_mapping(calendar_service):
    store, service, provider = calendar_service
    previous = run(store, "Личный", request_id="old-catalog")
    service.list_calendars(previous["id"])
    provider.items = [{"id": "cal-new-private", "name": "Личный"}, CALENDARS[1]]
    current = run(store, "Личный", request_id="new-catalog")
    found = service.list_calendars(current["id"])
    assert found["selected_calendar_id"] == "cal-new-private"
    with pytest.raises(ValueError): slots(service, found["calendars_id"], "cal-private", current["id"])
    assert provider.busy_calls == []


def test_chat_selection_is_revoked_when_a_second_discovery_no_longer_contains_the_label(calendar_service):
    store, service, provider = calendar_service
    current = run(store, "Личный")
    assert service.list_calendars(current["id"])["selected_calendar_id"] == "cal-private"
    provider.items = [{"id": "new-a", "name": "Новый A"}, {"id": "new-b", "name": "Новый B"}]
    fresh = service.list_calendars(current["id"])
    assert fresh["selected_calendar_id"] is None and fresh["requires_selection"]
    assert store.get("run", current["id"])["calendar_id"] is None
    with pytest.raises(ValueError): slots(service, fresh["calendars_id"], "new-a", current["id"])
    assert provider.busy_calls == []


def test_calendar_evidence_from_another_run_is_not_reused_after_manual_choice(calendar_service):
    store, service, provider = calendar_service
    previous = run(store, "Личный", request_id="previous-evidence")
    found = service.list_calendars(previous["id"])
    current = run(store, "Личный", request_id="current-evidence")
    with pytest.raises(ValueError): slots(service, found["calendars_id"], "cal-private", current["id"])
    assert provider.busy_calls == []


def test_legacy_explicit_api_calendar_id_remains_valid_without_enabling_model_override(calendar_service):
    store, service, provider = calendar_service
    current = run(store, "Старое поручение", calendar_id="cal-work")
    found = service.list_calendars(current["id"])
    assert found["selected_calendar_id"] == "cal-work" and not found["requires_selection"]
    with pytest.raises(ValueError): slots(service, found["calendars_id"], "cal-private", current["id"])
    assert provider.busy_calls == []
