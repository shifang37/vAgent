"""D0's frozen v3 fixtures proving the D1 v3->v4 envelope upgrade."""

import hashlib
import json
from pathlib import Path

import pytest

from vagent.errors import AppError
from vagent.storage import Database, DatabaseV3, FileStore
from vagent.video.contracts import canonical_fingerprint

FIXTURES = Path(__file__).parent / "fixtures" / "m2"
MANIFEST = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))["snapshots"]


@pytest.mark.parametrize("entry", MANIFEST, ids=lambda entry: entry["file"])
def test_frozen_m2_v3_fixtures_migrate_to_v5_with_exact_backup(tmp_path, entry):
    raw = (FIXTURES / entry["file"]).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == entry["rawSha256"]
    original = json.loads(raw)
    assert original["schemaVersion"] == entry["schemaVersion"] == 3
    DatabaseV3.model_validate(original)
    (tmp_path / "state.json").write_bytes(raw)
    checkpoint_files = {
        name: f"untouched-{name}".encode()
        for name in ("checkpoints.sqlite", "checkpoints.sqlite-wal", "checkpoints.sqlite-shm")
    }
    for name, content in checkpoint_files.items():
        (tmp_path / name).write_bytes(content)
    with FileStore.open(tmp_path) as store:
        state = store.snapshot()
        assert state["schemaVersion"] == 5
        assert state["storyboards"] == {} and state["assets"] == {} and state["films"] == {}
        # The E0-frozen v4 envelope is still reproduced exactly by the migration.
        v4_projection = {key: value for key, value in state.items() if key != "films"}
        v4_projection["schemaVersion"] = 4
        assert canonical_fingerprint(v4_projection) == entry["expectedV4CanonicalSha256"]
        Database.model_validate(state)
        for collection, digest in entry["preservedCollectionsSha256"].items():
            assert canonical_fingerprint(state[collection]) == digest
            assert state[collection] == original[collection]
        assert {key: job["requestFingerprint"] for key, job in state["jobs"].items()} == entry[
            "jobRequestFingerprints"
        ]
        assert sorted(state["media"]) == entry["mediaIds"]
        assert {key: op["fingerprint"] for key, op in state["operations"].items()} == entry[
            "operationFingerprints"
        ]
    backups = list(tmp_path.glob("state-v3-*.json"))
    assert len(backups) == 1 and backups[0].read_bytes() == raw
    for name, content in checkpoint_files.items():
        assert (tmp_path / name).read_bytes() == content
    with FileStore.open(tmp_path) as reopened:
        assert reopened.snapshot() == state
    assert len(list(tmp_path.glob("state-v*.json"))) == 1


@pytest.mark.parametrize("phase", ["backup", "replace"])
def test_v3_migration_failure_preserves_original_and_releases_lock(tmp_path, monkeypatch, phase):
    import vagent.storage

    raw = (FIXTURES / MANIFEST[0]["file"]).read_bytes()
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
    backups = list(tmp_path.glob("state-v3-*.json"))
    assert len(backups) == (phase == "replace")
    if backups:
        assert backups[0].read_bytes() == raw
    monkeypatch.undo()
    with FileStore.open(tmp_path) as store:
        assert store.snapshot()["schemaVersion"] == 5


def test_v5_store_reopens_without_creating_backups(tmp_path):
    with FileStore.open(tmp_path) as store:
        assert store.snapshot()["schemaVersion"] == 5
    with FileStore.open(tmp_path) as reopened:
        assert reopened.snapshot()["schemaVersion"] == 5
    assert not list(tmp_path.glob("state-v*.json"))


@pytest.mark.parametrize("corrupt", ["empty", "unknown-version"])
def test_invalid_envelopes_are_rejected_without_backup(tmp_path, corrupt):
    if corrupt == "empty":
        raw = json.dumps({"schemaVersion": 4, "projects": {}}).encode()
    else:
        raw = json.dumps({"schemaVersion": 6, "projects": {}}).encode()
    (tmp_path / "state.json").write_bytes(raw)
    with pytest.raises(AppError) as error:
        FileStore.open(tmp_path)
    assert error.value.code == "INVALID_STORE"
    assert (tmp_path / "state.json").read_bytes() == raw
    assert not list(tmp_path.glob("state-v*.json"))
    assert not (tmp_path / "instance.lock").exists()
