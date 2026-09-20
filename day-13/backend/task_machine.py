"""Pure finite-state machine for the Day 13 task lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class TaskPhase(StrEnum):
    PLANNING = "planning"
    EXECUTION = "execution"
    VALIDATION = "validation"
    DONE = "done"


class RunStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"


class TaskEvent(StrEnum):
    ADVANCE = "advance"
    APPROVE = "approve"
    REQUEST_CHANGES = "request_changes"
    PAUSE = "pause"
    RESUME = "resume"


class InvalidTransition(ValueError):
    pass


@dataclass(frozen=True)
class MachineState:
    phase: TaskPhase
    run_status: RunStatus


PHASE_TRANSITIONS = {
    (TaskPhase.PLANNING, TaskEvent.ADVANCE): TaskPhase.EXECUTION,
    (TaskPhase.EXECUTION, TaskEvent.ADVANCE): TaskPhase.VALIDATION,
    (TaskPhase.VALIDATION, TaskEvent.APPROVE): TaskPhase.DONE,
    (TaskPhase.VALIDATION, TaskEvent.REQUEST_CHANGES): TaskPhase.EXECUTION,
}


def allowed_events(state: MachineState) -> list[TaskEvent]:
    if state.phase is TaskPhase.DONE:
        return []
    if state.run_status is RunStatus.PAUSED:
        return [TaskEvent.RESUME]
    events = [event for (phase, event), _ in PHASE_TRANSITIONS.items() if phase is state.phase]
    return [*events, TaskEvent.PAUSE]


def transition(state: MachineState, event: TaskEvent) -> MachineState:
    if event not in allowed_events(state):
        raise InvalidTransition(
            f"Событие «{event.value}» недоступно в состоянии {state.phase.value}/{state.run_status.value}."
        )
    if event is TaskEvent.PAUSE:
        return MachineState(state.phase, RunStatus.PAUSED)
    if event is TaskEvent.RESUME:
        return MachineState(state.phase, RunStatus.ACTIVE)
    return MachineState(PHASE_TRANSITIONS[(state.phase, event)], RunStatus.ACTIVE)
