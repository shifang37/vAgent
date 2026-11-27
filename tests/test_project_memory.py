"""Schema 10: project/session decoupling and the project-memory ledger (P0).

P0 only adds the data layer: the envelope, the optional session->project
binding and the migration. It must not change any observable behavior of a
migrated store, so every assertion here is about shape, equality of historical
objects, and the binding rules enforced by ``ensure_session``.
"""

import copy
import json

import pytest
from pydantic import ValidationError

from vagent.errors import AppError
from vagent.storage import (
    DatabaseV9,
    DatabaseV10,
    FileStore,
    MemoryEntry,
    ProjectMemory,
    migrated_state,
)


def test_fresh_store_is_schema_ten_with_an_empty_memory_ledger(store):
    state = store.snapshot()
    assert state["schemaVersion"] == 10
    assert state["memories"] == {}
    DatabaseV10.model_validate(state)


def test_v9_state_migrates_to_v10_with_an_exact_backup(tmp_path):
    home = tmp_path / "state"
    with FileStore.open(home) as store:
        store.ensure_session("coffee")
        original = copy.deepcopy(store.snapshot())

    # Reconstruct a genuine v9 envelope: no memory ledger and no session binding.
    legacy = copy.deepcopy(original)
    legacy["schemaVersion"] = 9
    legacy.pop("memories")
    for session in legacy["sessions"].values():
        session.pop("projectId", None)
    DatabaseV9.model_validate(legacy)
    raw = json.dumps(legacy, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
    (home / "state.json").write_bytes(raw)

    with FileStore.open(home) as store:
        migrated = store.snapshot()

    assert migrated["schemaVersion"] == 10
    assert migrated["memories"] == {}
    # The migration is envelope-only: historical objects are not re-encoded.
    assert migrated["projects"] == legacy["projects"]
    assert migrated["runs"] == legacy["runs"]
    assert migrated["sessions"] == legacy["sessions"]
    assert migrated["sessions"]["coffee"] == {"id": "coffee", "messages": []}
    DatabaseV10.model_validate(migrated)

    backups = list(home.glob("state-v9-*.json"))
    assert len(backups) == 1 and backups[0].read_bytes() == raw


def test_migrated_state_leaves_legacy_sessions_unbound():
    state = {
        "schemaVersion": 9,
        "projects": {
            "coffee": {
                "id": "coffee",
                "revision": 0,
                "goal": "",
                "audience": "",
                "style": "",
                "constraints": [],
                "plan": [],
            }
        },
        "sessions": {"coffee": {"id": "coffee", "messages": []}},
        "artifacts": {},
        "runs": {},
        "operations": {},
        "jobs": {},
        "waits": {},
        "media": {},
        "storyboards": {},
        "assets": {},
        "films": {},
        "episodes": {},
        "scenes": {},
        "characters": {},
        "styles": {},
        "audio": {},
        "subtitleTracks": {},
        "renders": {},
        "seasons": {},
        "batches": {},
        "timelines": {},
    }
    upgraded = migrated_state(state)
    assert upgraded["schemaVersion"] == 10
    assert upgraded["memories"] == {}
    assert "projectId" not in upgraded["sessions"]["coffee"]
    assert state["schemaVersion"] == 9 and "memories" not in state


def test_ensure_session_binds_a_new_session_to_its_own_project(store):
    store.ensure_session("coffee")
    state = store.snapshot()
    assert state["projects"]["coffee"]["id"] == "coffee"
    assert state["sessions"]["coffee"]["projectId"] == "coffee"
    # Re-running is idempotent and keeps the same binding.
    store.ensure_session("coffee")
    assert store.snapshot()["sessions"]["coffee"]["projectId"] == "coffee"


def test_new_session_can_join_an_existing_project(store):
    store.ensure_session("ep01")
    store.ensure_session("ep02", project_id="ep01")
    state = store.snapshot()
    assert state["sessions"]["ep02"]["projectId"] == "ep01"
    assert "ep02" not in state["projects"]
    # Idempotent re-bind to the same project is not a conflict.
    store.ensure_session("ep02", project_id="ep01")
    assert store.snapshot()["sessions"]["ep02"]["projectId"] == "ep01"


def test_session_cannot_change_its_project(store):
    store.ensure_session("ep01")
    store.ensure_session("ep02", project_id="ep01")
    store.ensure_session("ep03")
    with pytest.raises(AppError) as error:
        store.ensure_session("ep02", project_id="ep03")
    assert error.value.code == "SESSION_PROJECT_CONFLICT"
    assert store.snapshot()["sessions"]["ep02"]["projectId"] == "ep01"


def test_unknown_project_binding_is_rejected(store):
    with pytest.raises(AppError) as error:
        store.ensure_session("ep02", project_id="missing")
    assert error.value.code == "PROJECT_NOT_FOUND"
    assert "ep02" not in store.snapshot()["sessions"]


def test_database_v10_adds_exactly_the_memory_ledger():
    added = set(DatabaseV10.model_fields) - set(DatabaseV9.model_fields)
    assert added == {"memories"}
    assert DatabaseV10.model_fields["schema_version"].annotation == __import__("typing").Literal[10]


def test_project_memory_shape_is_bounded():
    entry = MemoryEntry(key="ep-01", kind="progress", text="第 1 集已保存 v1")
    document = ProjectMemory(id="coffee", revision=3, entries=[entry], updated_at="2026-11-27T00:00:00+00:00")
    assert document.model_dump(by_alias=True)["entries"][0]["key"] == "ep-01"

    with pytest.raises(ValidationError):
        MemoryEntry(key="Bad Key", kind="fact", text="x")
    with pytest.raises(ValidationError):
        MemoryEntry(key="ok", kind="note", text="x")
    with pytest.raises(ValidationError):
        MemoryEntry(key="ok", kind="fact", text="x" * 401)
    with pytest.raises(ValidationError):
        ProjectMemory(
            id="coffee",
            revision=0,
            entries=[MemoryEntry(key=f"k{i}", kind="fact", text="x") for i in range(65)],
            updated_at="2026-11-27T00:00:00+00:00",
        )


def test_future_schema_version_is_rejected(tmp_path):
    home = tmp_path / "future"
    home.mkdir()
    (home / "state.json").write_text('{"schemaVersion": 11}', encoding="utf-8")
    with pytest.raises(AppError) as error:
        FileStore.open(home)
    assert error.value.code == "INVALID_STORE"
    assert (home / "state.json").read_text(encoding="utf-8") == '{"schemaVersion": 11}'
