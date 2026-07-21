"""The backend-neutral storage port (SLS-2).

``StorageBackend`` is the full method set the blueprints call via
``current_app.storage.<method>()``. It is defined as a ``typing.Protocol`` so an
adapter need only provide matching methods (structural typing) — the reference
``PostgresBackend`` and the future ``DynamoBackend`` both satisfy it.

Every method takes/returns backend-neutral values: primitives, ``dict`` payloads
already validated by Marshmallow, and the frozen DTOs from ``dto.py``. Methods
raise the backend-neutral errors from ``errors.py`` (never SQLAlchemy exceptions
or ``flask_smorest.abort``); the app maps those to HTTP status codes.

The method list is derived from the exhaustive access-pattern audit of every
blueprint (see ``STORAGE_ABSTRACTION_DEEPDIVE.md`` §1.2).
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from .dto import (
    AgentDTO,
    ChainRunDTO,
    ChainStepDTO,
    CounterDTO,
    DecisionDTO,
    EpicDTO,
    EventDTO,
    IdempotentOutcome,
    NoteDTO,
    ProjectDTO,
    ProjectNoteDTO,
    ReservationDTO,
    TaskDTO,
)


@runtime_checkable
class StorageBackend(Protocol):
    # --- projects -------------------------------------------------------
    def list_projects(self) -> list[ProjectDTO]: ...
    def get_project(self, slug: str) -> ProjectDTO: ...                       # NotFound
    def create_project(self, data: dict) -> ProjectDTO: ...                   # Conflict
    def update_project(self, slug: str, patch: dict) -> ProjectDTO: ...       # NotFound
    def delete_project(self, slug: str) -> None: ...                          # NotFound; cascade

    # --- agents ---------------------------------------------------------
    def list_agents(self, slug: str) -> list[AgentDTO]: ...
    def upsert_agent(self, slug: str, data: dict) -> AgentDTO: ...

    # --- epics ----------------------------------------------------------
    def list_epics(self, slug: str) -> list[EpicDTO]: ...
    def create_epic(self, slug: str, data: dict) -> EpicDTO: ...              # Conflict
    def get_epic(self, slug: str, key: str) -> EpicDTO: ...                   # NotFound
    def update_epic(self, slug: str, key: str, patch: dict) -> EpicDTO: ...   # NotFound
    def list_epic_notes(self, slug: str, key: str) -> list[NoteDTO]: ...
    def append_epic_note(self, slug: str, key: str, data: dict) -> NoteDTO: ...

    # --- tasks: CRUD ----------------------------------------------------
    def list_tasks(self, slug: str, flt: dict) -> list[TaskDTO]: ...
    def create_task(self, slug: str, data: dict) -> TaskDTO: ...             # Conflict/NotFound
    def get_task(self, slug: str, ident: str) -> TaskDTO: ...                # NotFound
    def update_task(self, slug: str, ident: str, patch: dict,
                    expected_version: str | None) -> TaskDTO: ...            # VersionConflict
    def delete_task(self, slug: str, ident: str) -> None: ...

    # --- tasks: the two atomic guarantees + lifecycle -------------------
    def claim_next(self, slug: str, agent: str, *, epic: str | None = None,
                   priority_max: str | None = None, component: str | None = None,
                   lease_ttl: int | None = None, idempotency_key: str | None = None,
                   serialize=None) -> IdempotentOutcome: ...
    def complete_task(self, slug: str, ident: str, data: dict,
                      expected_version: str | None) -> TaskDTO: ...
    def release_task(self, slug: str, ident: str, reset_to: str) -> TaskDTO: ...
    def set_status(self, slug: str, ident: str, status: str, note: str | None,
                   has_note: bool, expected_version: str | None) -> TaskDTO: ...
    def add_commit(self, slug: str, ident: str, data: dict) -> TaskDTO: ...
    def list_task_notes(self, slug: str, ident: str) -> list[NoteDTO]: ...
    def append_task_note(self, slug: str, ident: str, data: dict) -> NoteDTO: ...
    def add_relation(self, slug: str, ident: str, target: str, kind: str) -> str: ...

    # --- reservations / counters (atomic reservation) ------------------
    def reserve_number(self, slug: str, namespace: str, *, reserved_by: str | None = None,
                       task_key: str | None = None, note: str | None = None,
                       idempotency_key: str | None = None,
                       serialize=None) -> IdempotentOutcome: ...
    def list_reservations(self, slug: str, namespace: str | None) -> list[ReservationDTO]: ...
    def list_counters(self, slug: str) -> list[CounterDTO]: ...

    # --- events / notes-feed / decisions -------------------------------
    def create_event(self, slug: str, data: dict) -> EventDTO: ...
    def list_events(self, slug: str, flt: dict) -> list[EventDTO]: ...
    def list_project_notes(self, slug: str, flt: dict) -> list[ProjectNoteDTO]: ...
    def list_decisions(self, slug: str) -> list[DecisionDTO]: ...
    def create_decision(self, slug: str, data: dict) -> DecisionDTO: ...

    # --- chains ---------------------------------------------------------
    def create_chain_run(self, slug: str, ident: str, started_by: str | None) -> ChainRunDTO: ...
    def get_chain_run(self, slug: str, run_pubid: str) -> ChainRunDTO: ...
    def update_chain_run(self, slug: str, run_pubid: str, status: str | None) -> ChainRunDTO: ...
    def upsert_chain_step(self, slug: str, run_pubid: str, step_name: str,
                          data: dict) -> ChainStepDTO: ...

    # --- ports (SPEC.md round-trip) ------------------------------------
    def import_spec(self, slug: str, parsed) -> dict: ...
    def render_spec_text(self, slug: str) -> str: ...
