import copy
import json

import pytest
from conftest import video_run
from langchain_core.messages import HumanMessage, messages_to_dict

from vagent.errors import AppError
from vagent.storage import Database, FileStore
from vagent.tools import create_project_tools
from vagent.waiting import ExternalResourceRef, WaitBinding


def legacy_state(home):
    with FileStore.open(home) as store:
        context = video_run(store)
        tools = create_project_tools()
        args = {"kind": "brief", "title": "旧标题", "content": "旧正文"}
        first = tools.execute(
            "artifact_save", args, store=store, project_id="coffee", operation_key="original-save"
        )
        tools.execute(
            "artifact_save",
            {**args, "artifactId": first["data"]["artifactId"], "expectedVersion": 1, "content": "新正文"},
            store=store,
            project_id="coffee",
            operation_key="original-update",
        )
        state = store.snapshot()
        state.pop("jobs")
        state.pop("waits")
        state["schemaVersion"] = 1
        run = state["runs"][context.run_id]
        run.update(
            status="failed",
            executionVersion=1,
            contextSignature="original-signature",
            contextVersion=2,
            activeSeconds=12.5,
            modelSteps=1,
            toolCalls=2,
            toolCallKeys=["original-save", "original-update"],
            inputTokens=37,
            outputTokens=5,
            messages=messages_to_dict([HumanMessage(content="原始需求")]),
            modelCalls=[
                {
                    "step": 1,
                    "status": "responded",
                    "startedAt": "2030-01-01T00:00:00Z",
                    "inputTokens": 37,
                    "outputTokens": 5,
                    "cacheHitTokens": 20,
                    "cacheMissTokens": 17,
                    "cacheUsageSource": "reported",
                }
            ],
        )
        state["sessions"]["coffee"]["messages"] = copy.deepcopy(run["messages"])
    raw = json.dumps(state, ensure_ascii=False, indent=4).replace("\n", "\r\n").encode("utf-8")
    (home / "state.json").write_bytes(raw)
    return state, raw, args, first


def test_migration_keeps_exact_backup_and_all_legacy_domain_data(tmp_path):
    home = tmp_path / "state"
    original, raw, args, first = legacy_state(home)
    checkpoint = home / "checkpoints.sqlite"
    checkpoint.write_bytes(b"untouched-checkpoint-fixture")
    with FileStore.open(home) as store:
        migrated = store.snapshot()
        assert migrated == {**original, "schemaVersion": 2, "jobs": {}, "waits": {}}
        assert next(home.glob("state-v1-*.json")).read_bytes() == raw
        assert checkpoint.read_bytes() == b"untouched-checkpoint-fixture"
        parsed = Database.model_validate(migrated)
        assert parsed.runs["video-run"].video_mode == "off"
        assert parsed.runs["video-run"].external_wait_seconds == 0
        assert "videoMode" not in migrated["runs"]["video-run"]
        assert store.operation_result("original-save", "artifact_save", args) == first
    with FileStore.open(home) as reopened:
        assert reopened.snapshot() == migrated
        assert len(list(home.glob("state-v1-*.json"))) == 1


@pytest.mark.parametrize("phase", ["backup", "replace"])
def test_migration_failure_preserves_original_and_releases_lock(tmp_path, monkeypatch, phase):
    import vagent.storage

    home = tmp_path / "state"
    _, raw, _, _ = legacy_state(home)
    original_replace = vagent.storage.os.replace

    def fail(source, destination):
        if (destination.name == "state.json") == (phase == "replace"):
            raise OSError("injected migration write failure")
        return original_replace(source, destination)

    monkeypatch.setattr(vagent.storage.os, "replace", fail)
    with pytest.raises(OSError):
        FileStore.open(home)
    assert (home / "state.json").read_bytes() == raw
    assert not (home / "instance.lock").exists()
    assert not list(home.glob("*.tmp"))
    backups = list(home.glob("state-v1-*.json"))
    assert len(backups) == (1 if phase == "replace" else 0)
    if backups:
        assert backups[0].read_bytes() == raw
    monkeypatch.undo()
    with FileStore.open(home) as store:
        assert store.snapshot()["schemaVersion"] == 2


@pytest.mark.parametrize("version", [0, 3, True, "1", None])
def test_unknown_or_coerced_versions_are_not_migrated(tmp_path, version):
    state = tmp_path / "state.json"
    raw = json.dumps({"schemaVersion": version, "projects": {}}).encode()
    state.write_bytes(raw)
    with pytest.raises(AppError) as error:
        FileStore.open(tmp_path)
    assert error.value.code == "INVALID_STORE"
    assert state.read_bytes() == raw
    assert not list(tmp_path.glob("state-v1-*.json"))
    assert not (tmp_path / "instance.lock").exists()


def test_invalid_legacy_messages_are_rejected_before_backup(tmp_path):
    original, _, _, _ = legacy_state(tmp_path)
    original["runs"]["video-run"]["messages"] = [{"type": "tool", "data": {"content": "no call ID"}}]
    raw = json.dumps(original).encode()
    (tmp_path / "state.json").write_bytes(raw)
    with pytest.raises(AppError) as error:
        FileStore.open(tmp_path)
    assert error.value.code == "INVALID_STORE"
    assert (tmp_path / "state.json").read_bytes() == raw
    assert not list(tmp_path.glob("state-v1-*.json"))


def test_migration_requires_existing_instance_lock(tmp_path):
    original, raw, _, _ = legacy_state(tmp_path)
    lock = tmp_path / "instance.lock"
    lock.write_text("fixture active writer", encoding="utf-8")
    with pytest.raises(AppError) as error:
        FileStore.open(tmp_path)
    assert error.value.code == "STORE_LOCKED"
    assert (tmp_path / "state.json").read_bytes() == raw
    assert not list(tmp_path.glob("state-v1-*.json"))
    assert lock.read_text(encoding="utf-8") == "fixture active writer"
    lock.unlink()
    with FileStore.open(tmp_path) as store:
        assert store.snapshot()["operations"] == original["operations"]


def test_v2_waiting_run_and_binding_are_preserved_without_starting_a_runner(store):
    context = video_run(store)
    binding = WaitBinding(
        id="saved-wait",
        context=context,
        resource=ExternalResourceRef(kind="job", id="job-reference"),
        started_at="2030-01-01T00:00:00Z",
        deadline_at="2030-01-01T00:10:00Z",
    )

    def wait(draft):
        draft["runs"][context.run_id].update(
            status="waiting_external",
            executionVersion=2,
            videoMode="mock",
            activeWaitId=binding.id,
            externalWaitSeconds=7.5,
        )
        draft["waits"][binding.id] = binding.model_dump(mode="json", by_alias=True)

    store.transaction(wait)
    original = store.snapshot()
    home = store.home
    store.close()
    with FileStore.open(home) as reopened:
        assert reopened.snapshot() == original
        assert not list(home.glob("state-v1-*.json"))
