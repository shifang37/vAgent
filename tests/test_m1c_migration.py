"""C0's immutable byte/hash evidence, separate from runtime/checkpoint recovery."""

import hashlib
import json
from pathlib import Path

import pytest

from vagent.errors import AppError
from vagent.storage import Database, DatabaseV1, DatabaseV2, FileStore
from vagent.video.contracts import canonical_fingerprint

FIXTURES = Path(__file__).parent / "fixtures" / "m1c"
MANIFEST = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))["snapshots"]


@pytest.mark.parametrize("entry", MANIFEST, ids=lambda entry: entry["file"])
def test_frozen_c0_migration_envelope_and_original_bytes(tmp_path, entry):
    raw = (FIXTURES / entry["file"]).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == entry["rawSha256"]
    original = json.loads(raw)
    {1: DatabaseV1, 2: DatabaseV2}[entry["schemaVersion"]].model_validate(original)
    (tmp_path / "state.json").write_bytes(raw)
    checkpoint_files = {
        name: f"untouched-{name}".encode()
        for name in ("checkpoints.sqlite", "checkpoints.sqlite-wal", "checkpoints.sqlite-shm")
    }
    for name, content in checkpoint_files.items():
        (tmp_path / name).write_bytes(content)
    with FileStore.open(tmp_path) as store:
        state = store.snapshot()
        assert state["schemaVersion"] == 3 and state["media"] == {}
        assert canonical_fingerprint(state) == entry["expectedV3CanonicalSha256"]
        Database.model_validate(state)
        for collection, digest in entry["preservedCollectionsSha256"].items():
            assert canonical_fingerprint(state[collection]) == digest
            assert state[collection] == original[collection]
        assert {key: job["requestFingerprint"] for key, job in state["jobs"].items()} == entry[
            "jobRequestFingerprints"
        ]
        assert {key: op["fingerprint"] for key, op in state["operations"].items()} == entry[
            "operationFingerprints"
        ]
    backups = list(tmp_path.glob(f"state-v{entry['schemaVersion']}-*.json"))
    assert len(backups) == 1 and backups[0].read_bytes() == raw
    for name, content in checkpoint_files.items():
        assert (tmp_path / name).read_bytes() == content
    with FileStore.open(tmp_path) as reopened:
        assert reopened.snapshot() == state
    assert len(list(tmp_path.glob("state-v*.json"))) == 1


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("phase", ["backup", "replace"])
def test_both_source_versions_preserved_on_migration_write_failure(tmp_path, monkeypatch, version, phase):
    import vagent.storage

    raw = (FIXTURES / MANIFEST[version - 1]["file"]).read_bytes()
    (tmp_path / "state.json").write_bytes(raw)
    replace = vagent.storage.os.replace

    def fail(source, destination):
        if (destination.name == "state.json") == (phase == "replace"):
            raise OSError("injected migration failure")
        return replace(source, destination)

    monkeypatch.setattr(vagent.storage.os, "replace", fail)
    with pytest.raises(OSError):
        FileStore.open(tmp_path)
    assert (tmp_path / "state.json").read_bytes() == raw
    assert not (tmp_path / "instance.lock").exists()
    assert not list(tmp_path.glob("*.tmp"))
    backups = list(tmp_path.glob("state-v*.json"))
    assert len(backups) == (phase == "replace")
    if backups:
        assert backups[0].read_bytes() == raw
    monkeypatch.undo()
    with FileStore.open(tmp_path) as store:
        assert store.snapshot()["schemaVersion"] == 3


@pytest.mark.parametrize("corrupt", ["job-version", "fingerprint", "operation", "message"])
def test_invalid_v2_source_rejected_before_backup(tmp_path, corrupt):
    state = json.loads((FIXTURES / "legacy-v2-mock.json").read_bytes())
    job = next(iter(state["jobs"].values()))
    if corrupt == "job-version":
        job["contractVersion"] = True
    elif corrupt == "fingerprint":
        job["requestFingerprint"] = "0" * 64
    elif corrupt == "operation":
        state["operations"].pop(job["operationKey"])
    else:
        next(iter(state["runs"].values()))["messages"] = [{"type": "tool", "data": {"content": "missing ID"}}]
    raw = json.dumps(state).encode()
    (tmp_path / "state.json").write_bytes(raw)
    with pytest.raises(AppError, match="原文件") as error:
        FileStore.open(tmp_path)
    assert error.value.code == "INVALID_STORE"
    assert (tmp_path / "state.json").read_bytes() == raw
    assert not list(tmp_path.glob("state-v*.json"))
