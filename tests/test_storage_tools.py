import json
from uuid import uuid4

import pytest
from langchain_core.messages import HumanMessage, messages_from_dict

from vagent.errors import AppError
from vagent.storage import FileStore
from vagent.tools import create_project_tools


def execute(store, name, args=None, *, project="coffee", key=None):
    store.ensure_session(project)
    return create_project_tools().execute(
        name, args or {}, store=store, project_id=project, operation_key=key or str(uuid4())
    )


def test_versions_preserve_history_and_scope(store):
    args = {"kind": "brief", "title": "warm", "content": "original content"}
    first = execute(store, "artifact_save", args)["data"]
    artifact_id = first["artifactId"]
    second = execute(
        store,
        "artifact_save",
        {**args, "artifactId": artifact_id, "expectedVersion": 1, "content": "rainy night"},
    )
    assert second["data"]["version"] == 2
    old = execute(store, "artifact_read", {"artifactId": artifact_id, "version": 1, "offset": 2, "limit": 5})[
        "data"
    ]
    assert old["content"] == "igina" and old["nextOffset"] == 7
    assert execute(store, "artifact_read", {"artifactId": artifact_id})["data"]["content"] == "rainy night"
    assert (
        execute(store, "artifact_read", {"artifactId": artifact_id}, project="other")["error"]["code"]
        == "NOT_FOUND"
    )
    assert (
        execute(store, "artifact_save", {**args, "artifactId": artifact_id, "expectedVersion": 1})["error"][
            "code"
        ]
        == "VERSION_CONFLICT"
    )
    assert (
        execute(store, "artifact_save", {**args, "artifactId": artifact_id})["error"]["code"]
        == "INVALID_ARGUMENTS"
    )
    assert len(store.snapshot()["artifacts"][artifact_id]["versions"]) == 2


def test_project_revision_prevents_overwrite_and_null_values(store):
    first = execute(store, "project_update", {"expectedRevision": 0, "audience": "workers", "style": "warm"})
    assert first["data"]["revision"] == 1
    assert (
        execute(store, "project_update", {"expectedRevision": 0, "style": "rain"})["error"]["code"]
        == "REVISION_CONFLICT"
    )
    assert (
        execute(store, "project_update", {"expectedRevision": 1, "style": None})["error"]["code"]
        == "INVALID_ARGUMENTS"
    )
    latest = execute(store, "project_update", {"expectedRevision": 1, "style": "rain"})["data"]
    assert latest["audience"] == "workers" and latest["revision"] == 2


def test_operation_journal_survives_restart(store):
    args = {"kind": "script", "title": "test", "content": "same"}
    first = execute(store, "artifact_save", args, key="stable-op")
    assert execute(store, "artifact_save", args, key="stable-op") == first
    home = store.home
    store.close()
    with FileStore.open(home) as reopened:
        assert execute(reopened, "artifact_save", args, key="stable-op") == first
        assert (
            execute(reopened, "artifact_save", {**args, "content": "different"}, key="stable-op")["error"][
                "code"
            ]
            == "OPERATION_CONFLICT"
        )
        assert len(reopened.snapshot()["artifacts"]) == 1


def test_failed_operation_rolls_back_partial_mutation(store):
    store.ensure_session("coffee")

    def broken(draft):
        draft["projects"]["coffee"]["goal"] = "partial"
        raise AppError("EXPECTED", "failed")

    assert not store.operation("broken", "test", {}, broken)["ok"]
    assert store.snapshot()["projects"]["coffee"]["goal"] == ""
    assert store.snapshot()["operations"]["broken"]["result"]["error"]["code"] == "EXPECTED"


def test_exclusive_writer_and_reopen(store):
    with pytest.raises(AppError, match="锁定"):
        FileStore.open(store.home)
    home = store.home
    store.close()
    with FileStore.open(home) as reopened:
        assert reopened.snapshot()["schemaVersion"] == 2


def test_corrupt_state_is_preserved(tmp_path):
    state = tmp_path / "state.json"
    raw = '{"schemaVersion":99,"projects":{}}'
    state.write_text(raw, encoding="utf-8")
    with pytest.raises(AppError, match="保留原文件"):
        FileStore.open(tmp_path)
    assert state.read_text(encoding="utf-8") == raw
    assert not (tmp_path / "instance.lock").exists()


def test_atomic_replace_failure_preserves_disk_and_memory(store, monkeypatch):
    import vagent.storage

    before = store.snapshot()
    raw = (store.home / "state.json").read_bytes()

    def fail(*_):
        raise OSError("disk error")

    monkeypatch.setattr(vagent.storage.os, "replace", fail)
    with pytest.raises(OSError):
        store.ensure_session("new")
    assert store.snapshot() == before
    assert (store.home / "state.json").read_bytes() == raw
    assert not list(store.home.glob("*.tmp"))


def test_legacy_schema_one_messages_and_interrupted_runs(tmp_path):
    # LangChain JS toDict() shape from the original prototype; Python must read it unchanged.
    messages = [
        {"type": "human", "data": {"content": "旧需求", "additional_kwargs": {}, "response_metadata": {}}},
        {
            "type": "ai",
            "data": {
                "content": "",
                "additional_kwargs": {},
                "response_metadata": {},
                "tool_calls": [{"name": "project_read", "args": {}, "id": "read", "type": "tool_call"}],
                "invalid_tool_calls": [],
            },
        },
        {
            "type": "tool",
            "data": {
                "content": '{"ok":true}',
                "tool_call_id": "read",
                "name": "project_read",
                "additional_kwargs": {},
                "response_metadata": {},
            },
        },
        {
            "type": "ai",
            "data": {
                "content": "已读取",
                "additional_kwargs": {},
                "response_metadata": {},
                "tool_calls": [],
                "invalid_tool_calls": [],
            },
        },
    ]
    legacy = {
        "schemaVersion": 1,
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
        "sessions": {"coffee": {"id": "coffee", "messages": messages}},
        "artifacts": {},
        "operations": {},
        "runs": {
            "old": {
                "id": "old",
                "sessionId": "coffee",
                "requestId": "old",
                "prompt": "旧需求",
                "model": "test",
                "status": "running",
                "messages": messages,
                "modelSteps": 2,
                "toolCalls": 1,
                "inputTokens": 0,
                "outputTokens": 0,
                "answer": "",
                "createdAt": "2026-09-29",
                "updatedAt": "2026-09-29",
            }
        },
    }
    (tmp_path / "state.json").write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
    with FileStore.open(tmp_path) as reopened:
        snapshot = reopened.snapshot()
        assert snapshot["runs"]["old"]["status"] == "interrupted"
        assert snapshot["runs"]["old"]["contextBytes"] == 0
        restored = messages_from_dict(snapshot["sessions"]["coffee"]["messages"])
        assert isinstance(restored[0], HumanMessage) and restored[0].content == "旧需求"
        assert len(restored) == 4
