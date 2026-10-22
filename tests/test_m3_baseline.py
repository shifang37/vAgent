"""E0's immutable synthetic v4 corpus, captured before M3 changes any contracts."""

import hashlib
import json
from pathlib import Path

import pytest

from vagent.errors import AppError
from vagent.storage import DatabaseV4, DatabaseV5, DatabaseV7, FileStore
from vagent.video.contracts import canonical_fingerprint, parse_job
from vagent.waiting import parse_wait

FIXTURES = Path(__file__).parent / "fixtures" / "m3"
MANIFEST = json.loads((FIXTURES / "manifest.json").read_bytes())
SNAPSHOTS = MANIFEST["snapshots"]


def load(entry):
    return json.loads((FIXTURES / entry["file"]).read_bytes())


@pytest.mark.parametrize("entry", SNAPSHOTS, ids=lambda entry: entry["file"])
def test_v4_baseline_integrity_and_frozen_identities(entry):
    raw = (FIXTURES / entry["file"]).read_bytes()
    state = json.loads(raw)
    assert hashlib.sha256(raw).hexdigest() == entry["rawSha256"]
    assert state["schemaVersion"] == entry["schemaVersion"] == 4
    DatabaseV4.model_validate(state)
    assert canonical_fingerprint(state) == entry["canonicalSha256"]
    # The frozen future-envelope hash is now produced by the real migration below.
    assert (
        canonical_fingerprint({**state, "schemaVersion": 5, "films": {}})
        == entry["expectedV5CanonicalSha256"]
    )
    assert {key: canonical_fingerprint(state[key]) for key in entry["collectionsSha256"]} == entry[
        "collectionsSha256"
    ]
    assert {key: len(state[key]) for key in entry["collectionCounts"]} == entry["collectionCounts"]
    assert {key: job["contractVersion"] for key, job in state["jobs"].items()} == entry["jobContracts"]
    assert {key: job["requestFingerprint"] for key, job in state["jobs"].items()} == entry[
        "jobRequestFingerprints"
    ]
    assert {key: job.get("intentFingerprint") for key, job in state["jobs"].items()} == entry[
        "jobIntentFingerprints"
    ]
    assert {key: op["fingerprint"] for key, op in state["operations"].items()} == entry[
        "operationFingerprints"
    ]
    for collection, manifest_key in (("media", "mediaSha256"), ("assets", "assetsSha256")):
        assert {key: item["sha256"] for key, item in state[collection].items()} == entry[manifest_key]
    for board_id, expected in entry["storyboardVersions"].items():
        board = state["storyboards"][board_id]
        assert board["currentVersion"] == expected["currentVersion"]
        assert board["revision"] == expected["revision"]
        assert canonical_fingerprint(board["versions"]) == expected["versionsSha256"]
        assert canonical_fingerprint(board["shotVersions"]) == expected["shotVersionsSha256"]
    for run_id, expected in entry["runSignatures"].items():
        run = state["runs"][run_id]
        assert run.get("contextSignature") == expected["contextSignature"]
        assert run.get("executionVersion") == expected["executionVersion"]
        assert run.get("contextVersion", 1) == expected["contextVersion"]
        assert canonical_fingerprint(run.get("toolFeatures", {})) == expected["toolFeaturesSha256"]
    for run_id, profile in entry["computedRunnerProfiles"].items():
        run = state["runs"][run_id]
        assert profile["kind"] == "computed-synthetic-runner"
        assert run["contextSignature"] == profile["contextSignature"]
        assert len(bytes.fromhex(profile["contextSignature"])) == 32
        assert run["model"] == profile["model"] == "m3-baseline-fixture"
        assert run["executionVersion"] == profile["executionVersion"] == 3
        assert run["contextVersion"] == profile["contextVersion"] == 2
        assert canonical_fingerprint(run["toolFeatures"]) == profile["toolFeaturesSha256"]
        assert profile["skills"] and all(skill["version"] for skill in profile["skills"])


@pytest.mark.parametrize("entry", SNAPSHOTS, ids=lambda entry: entry["file"])
def test_v4_baseline_migrates_to_the_frozen_v5_envelope(tmp_path, entry):
    raw = (FIXTURES / entry["file"]).read_bytes()
    original = json.loads(raw)
    (tmp_path / "state.json").write_bytes(raw)
    sentinels = {
        name: f"domain-only-sentinel:{name}".encode()
        for name in ("checkpoints.sqlite", "checkpoints.sqlite-wal", "checkpoints.sqlite-shm")
    }
    for name, content in sentinels.items():
        (tmp_path / name).write_bytes(content)
    with FileStore.open(tmp_path) as store:
        migrated = store.snapshot()
    # M5 E0 froze this v5 envelope first; the v4 fixture still reaches it exactly.
    v5_envelope = {**original, "schemaVersion": 5, "films": {}}
    assert canonical_fingerprint(v5_envelope) == entry["expectedV5CanonicalSha256"]
    DatabaseV5.model_validate(v5_envelope)
    # The current migration continues mechanically into the v6 episode collections
    # and then the v7 audio/subtitle collections.
    expected = {
        **v5_envelope,
        "schemaVersion": 7,
        "episodes": {},
        "scenes": {},
        "characters": {},
        "styles": {},
        "audio": {},
        "subtitleTracks": {},
    }
    assert migrated == expected
    DatabaseV7.model_validate(migrated)
    backups = list(tmp_path.glob("state-v4-*.json"))
    assert len(backups) == 1 and backups[0].read_bytes() == raw
    for name, content in sentinels.items():
        assert (tmp_path / name).read_bytes() == content
    with FileStore.open(tmp_path) as reopened:
        assert reopened.snapshot() == expected
    assert len(list(tmp_path.glob("state-v*.json"))) == 1


@pytest.mark.parametrize("mode", ["mock", "live"])
def test_legacy_v4_baselines_preserve_every_historical_domain_value(mode):
    original = json.loads((FIXTURES.parent / "m2" / f"legacy-v3-{mode}.json").read_bytes())
    state = json.loads((FIXTURES / f"legacy-v4-{mode}.json").read_bytes())
    assert state == {**original, "schemaVersion": 4, "storyboards": {}, "assets": {}}


def test_baseline_covers_historical_contracts_and_current_linked_history():
    assert MANIFEST["kind"] == "synthetic-v4-domain-baseline-only"
    assert MANIFEST["capturedFrom"] == "d06dc5f"
    assert MANIFEST["checkpointsIncluded"] is False
    assert MANIFEST["binaryAssetsIncluded"] is False
    assert MANIFEST["realProviderRequests"] == 0
    states = [load(entry) for entry in SNAPSHOTS]
    assert {job["contractVersion"] for state in states for job in state["jobs"].values()} == {1, 2, 3, 4, 5}
    assert {run["status"] for state in states for run in state["runs"].values()} >= {
        "waiting_external",
        "completed",
    }
    assert any(state["media"] for state in states)
    for state in states:
        for media in state["media"].values():
            job = parse_job(state["jobs"][media["jobId"]])
            assert job.status == "succeeded"
            assert job.context.project_id == media["projectId"]
            assert job.result.media_refs[0].media_id == media["id"]
    current = json.loads((FIXTURES / "current-v4-shots.json").read_bytes())
    assert len(current["assets"]) == len(current["storyboards"]) == 1
    board = next(iter(current["storyboards"].values()))
    assert [version["version"] for version in board["versions"]] == [1, 2]
    assert {shot["mode"] for version in board["versions"] for shot in version["shots"]} == {"t2v", "i2v"}
    counts = {}
    for version in board["shotVersions"]:
        counts[version["shotId"]] = counts.get(version["shotId"], 0) + 1
        job = parse_job(current["jobs"][version["jobId"]])
        assert job.shot_ref.shot_version_id == version["id"]
        assert job.shot_ref.storyboard_id == board["id"]
        assert job.request.model_dump(mode="json", by_alias=True) == version["request"]
    assert sorted(counts.values()) == [2, 3]  # Two saved versions plus one explicit target redo.
    binding = parse_wait(next(iter(current["waits"].values())))
    assert binding.resource.kind == "jobset" and len(binding.job_ids) == 2
    assert all(current["jobs"][job_id]["contractVersion"] == 5 for job_id in binding.job_ids)
    assert current["runs"][binding.context.run_id]["activeWaitId"] == binding.id


@pytest.mark.parametrize("missing", ["operation", "reference-asset"])
def test_baseline_cross_references_are_required_to_open(tmp_path, missing):
    state = json.loads((FIXTURES / "current-v4-shots.json").read_bytes())
    if missing == "operation":
        job = next(iter(state["jobs"].values()))
        del state["operations"][job["operationKey"]]
    else:
        state["assets"].clear()
    raw = json.dumps(state, ensure_ascii=False).encode()
    (tmp_path / "state.json").write_bytes(raw)
    with pytest.raises(AppError) as error:
        FileStore.open(tmp_path)
    assert error.value.code == "INVALID_STORE"
    assert (tmp_path / "state.json").read_bytes() == raw
    assert not (tmp_path / "instance.lock").exists()
    assert not list(tmp_path.glob("state-v*.json"))
