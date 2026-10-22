"""M5 E0's immutable synthetic v5 corpus, captured after M4 and before M5 code."""

import hashlib
import json
from pathlib import Path

import pytest

from vagent.errors import AppError
from vagent.storage import Database, DatabaseV5, FileStore
from vagent.video.contracts import canonical_fingerprint
from vagent.video.episodes import (
    EPISODE_COST_CAP,
    EPISODE_JOB_CAP,
    EPISODE_MAX_SECONDS,
    EPISODE_MAX_SHOTS,
    EPISODE_MIN_SECONDS,
    EPISODE_MIN_SHOTS,
    SCENE_MAX_SHOTS,
    TEXT_SPLIT_CONTRACT,
)
from vagent.video.films import parse_media
from vagent.video.storyboards import Storyboard

FIXTURES = Path(__file__).parent / "fixtures" / "m5"
MANIFEST = json.loads((FIXTURES / "manifest.json").read_bytes())
SNAPSHOTS = MANIFEST["snapshots"]
V6_ADDED = {"episodes": {}, "scenes": {}, "characters": {}, "styles": {}}


def load(entry):
    return json.loads((FIXTURES / entry["file"]).read_bytes())


def v6_projection(state):
    return {**state, "schemaVersion": 6, **V6_ADDED}


@pytest.mark.parametrize("entry", SNAPSHOTS, ids=lambda entry: entry["file"])
def test_v5_baseline_integrity_and_frozen_identities(entry):
    raw = (FIXTURES / entry["file"]).read_bytes()
    state = json.loads(raw)
    assert hashlib.sha256(raw).hexdigest() == entry["rawSha256"]
    assert state["schemaVersion"] == entry["schemaVersion"] == 5
    DatabaseV5.model_validate(state)
    assert canonical_fingerprint(state) == entry["canonicalSha256"]
    assert canonical_fingerprint(v6_projection(state)) == entry["expectedV6CanonicalSha256"]
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
    for film_id, expected in entry["films"].items():
        film = state["films"][film_id]
        assert film["storyboardId"] == expected["storyboardId"]
        assert film["currentVersion"] == expected["currentVersion"]
        assert film["revision"] == expected["revision"]
        assert canonical_fingerprint(film["versions"]) == expected["versionsSha256"]
        assert [item["assembly"]["assemblyVersion"] for item in film["versions"]] == expected[
            "assemblyVersions"
        ]
        assert [
            (item["assembly"].get("normalization") or {}).get("profile") for item in film["versions"]
        ] == expected["normalizationProfiles"]
        assert [item["outputMediaId"] for item in film["versions"]] == expected["outputMediaIds"]
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
        assert run["model"] == profile["model"] == "m5-baseline-fixture"
        assert run["executionVersion"] == profile["executionVersion"] == 3
        assert run["contextVersion"] == profile["contextVersion"] == 2
        assert canonical_fingerprint(run["toolFeatures"]) == profile["toolFeaturesSha256"]
        # M5 E0 does not move the tool surface: 6/6 is the frozen M4 identity.
        assert profile["videoToolsVersion"] == profile["videoRulesVersion"] == 6
        assert run["toolFeatures"]["video"]["toolsVersion"] == 6
        assert run["toolFeatures"]["video"]["rulesVersion"] == 6
        assert profile["skills"] and all(skill["version"] for skill in profile["skills"])


@pytest.mark.parametrize("entry", SNAPSHOTS, ids=lambda entry: entry["file"])
def test_v5_baseline_migrates_to_the_v6_envelope(tmp_path, entry):
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
    expected = v6_projection(original)
    assert migrated == expected
    # E0 froze this hash before the migration existed; the real migration reproduces it.
    assert canonical_fingerprint(migrated) == entry["expectedV6CanonicalSha256"]
    for name, added in V6_ADDED.items():
        assert migrated[name] == added
    # Every historical domain value is untouched, only the envelope grew.
    for collection, value in original.items():
        if collection != "schemaVersion":
            assert migrated[collection] == value
    Database.model_validate(migrated)
    backups = list(tmp_path.glob("state-v5-*.json"))
    assert len(backups) == 1 and backups[0].read_bytes() == raw
    for name, content in sentinels.items():
        assert (tmp_path / name).read_bytes() == content
    with FileStore.open(tmp_path) as reopened:
        assert reopened.snapshot() == expected
    assert len(list(tmp_path.glob("state-v*.json"))) == 1


def test_baseline_covers_both_tool_surfaces_and_the_m4_film_record():
    assert MANIFEST["kind"] == "synthetic-v5-domain-baseline-only"
    assert MANIFEST["capturedFrom"] == "00d087f"
    assert MANIFEST["checkpointsIncluded"] is False
    assert MANIFEST["binaryAssetsIncluded"] is False
    assert MANIFEST["realProviderRequests"] == 0
    states = {entry["file"]: load(entry) for entry in SNAPSHOTS}
    surfaces = {
        entry["file"]: {
            run["toolFeatures"]["video"]["toolsVersion"]
            for run in load(entry)["runs"].values()
            if run.get("toolFeatures")
        }
        for entry in SNAPSHOTS
    }
    # Historical Runs keep their recorded 4/4 identity; current Runs carry 6/6.
    assert surfaces["current-v5-shots.json"] == {4}
    assert surfaces["current-v5-films.json"] == {6}
    films = states["current-v5-films.json"]["films"]
    assert len(films) == 1
    film = next(iter(films.values()))
    latest = film["versions"][-1]
    assert latest["assembly"]["assemblyVersion"] == "ffmpeg-concat-normalize-v4"
    assert latest["assembly"]["normalization"]["version"] == "ffmpeg-concat-normalize-v4"
    assert latest["assembly"]["normalization"]["width"] == 1280
    assert latest["assembly"]["normalization"]["height"] == 720
    # The media index stays a union: two delivered clips and one film record.
    media = states["current-v5-films.json"]["media"]
    kinds = sorted(parse_media(raw).__class__.__name__ for raw in media.values())
    assert kinds == ["FilmMedia", "MediaAsset", "MediaAsset"]
    assert latest["sizeBytes"] > 0 and len(latest["sha256"]) == 64
    assert [segment["shotId"] for segment in latest["assembly"]["timeline"]] == [
        item["shotId"] for item in latest["inputs"]
    ]


def test_film_inputs_reference_delivered_media_and_known_shot_versions():
    state = load(next(entry for entry in SNAPSHOTS if entry["file"] == "current-v5-films.json"))
    film_id, film = next(iter(state["films"].items()))
    assert film["id"] == film_id
    assert film["storyboardId"] in state["storyboards"]
    board = Storyboard.model_validate(state["storyboards"][film["storyboardId"]])
    known = {item.id for item in board.shot_versions}
    for version in film["versions"]:
        for item in version["inputs"]:
            assert item["shotVersionId"] in known
            job = state["jobs"][item["jobId"]]
            assert job["status"] == "succeeded"
            assert job["result"]["mediaRefs"][0]["mediaId"] == item["mediaId"]
            media = state["media"][item["mediaId"]]
            assert media["sha256"] == item["sha256"] and media["sizeBytes"] == item["sizeBytes"]
            assert media["jobId"] == item["jobId"]


def test_frozen_episode_limits_match_the_published_contract():
    assert TEXT_SPLIT_CONTRACT == "text-split-zh-v1"
    assert (EPISODE_MIN_SECONDS, EPISODE_MAX_SECONDS) == (60, 90)
    assert (EPISODE_MIN_SHOTS, EPISODE_MAX_SHOTS) == (10, 20)
    assert SCENE_MAX_SHOTS == 6
    assert EPISODE_JOB_CAP == 20
    assert EPISODE_COST_CAP == "60.00"


@pytest.mark.parametrize("missing", ["film-media", "film-storyboard"])
def test_baseline_cross_references_are_required_to_open(tmp_path, missing):
    state = load(next(entry for entry in SNAPSHOTS if entry["file"] == "current-v5-films.json"))
    if missing == "film-media":
        film = next(iter(state["films"].values()))
        del state["media"][film["versions"][-1]["outputMediaId"]]
    else:
        state["storyboards"].clear()
    raw = json.dumps(state, ensure_ascii=False).encode()
    (tmp_path / "state.json").write_bytes(raw)
    with pytest.raises(AppError) as error:
        FileStore.open(tmp_path)
    assert error.value.code == "INVALID_STORE"
    assert (tmp_path / "state.json").read_bytes() == raw
    assert not (tmp_path / "instance.lock").exists()
    assert not list(tmp_path.glob("state-v*.json"))
