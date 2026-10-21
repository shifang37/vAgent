"""Capture E0's synthetic v4 domain baseline; never use an application data directory.

Run once on the pre-M3 implementation. Committed snapshots are golden evidence,
not outputs to regenerate when a later migration or compatibility check fails.
All live Jobs are registration-only: the transport rejects every HTTP request.
"""

import asyncio
import hashlib
import json
import sys
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from uuid import NAMESPACE_URL, uuid5

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from conftest import VideoClock  # noqa: E402
from shot_support import (  # noqa: E402
    generation_args,
    insert_image,
    m2_run,
    mock_tools,
    next_context,
    redo_args,
    revise_storyboard,
    save_storyboard,
    shot,
)
from wan_support import live_arguments, live_run, live_service  # noqa: E402

from vagent.context import ContextBuilder  # noqa: E402
from vagent.runner import AgentRunner  # noqa: E402
from vagent.skills import SkillCatalog, register_skill_tool  # noqa: E402
from vagent.storage import Database, FileStore, migrated_state  # noqa: E402
from vagent.tools import create_project_tools  # noqa: E402
from vagent.video.contracts import canonical_fingerprint  # noqa: E402
from vagent.video.tools import register_video_tools  # noqa: E402

DESTINATION = ROOT / "tests" / "fixtures" / "m3"
TIMESTAMP = "2030-01-01T00:00:00+00:00"
CAPTURE_COMMIT = "d06dc5f"


def write_json(path, value):
    raw = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()
    path.write_bytes(raw)
    return raw


def fail_http(request):
    raise AssertionError(f"E0 fixture capture must not send HTTP: {request.method}")


def attach_signature(store, run_id, registry, signatures):
    skills = SkillCatalog.discover(ROOT / "skills")
    registry = register_skill_tool(registry, skills)
    runner = AgentRunner(
        store=store,
        model=SimpleNamespace(name="m3-baseline-fixture"),
        tools=registry,
        skills=skills.list(),
        execution_version=3,
        context=ContextBuilder(format_version=2),
    )
    signature = runner.context_signature()
    signatures[run_id] = {
        "kind": "computed-synthetic-runner",
        "model": runner.model.name,
        "executionVersion": 3,
        "contextVersion": 2,
        "contextSignature": signature,
        "toolFeaturesSha256": canonical_fingerprint(runner.tools.features),
        "skills": skills.list(),
        "toolSpecsSha256": canonical_fingerprint(runner.context.prepare_tools(runner.tools.specs())),
        "systemPromptSha256": hashlib.sha256(runner.system_prompt.encode()).hexdigest(),
        "maxInputBytes": runner.context.max_input_bytes,
    }
    store.transaction(
        lambda draft: draft["runs"][run_id].update(
            model=runner.model.name,
            executionVersion=3,
            contextVersion=2,
            contextSignature=signature,
            toolFeatures=runner.tools.features,
            videoMode=runner.video_mode,
        )
    )
    return registry


async def current_snapshot(home):
    clock = VideoClock()
    signatures = {}
    with FileStore.open(home) as store:
        registry, _, _, _ = mock_tools(store, clock=clock)
        attach_signature(store, "video-run", registry, signatures)
        image_id = insert_image(store)
        saved = save_storyboard(
            registry,
            store,
            [
                shot("镜头一：雨夜咖啡店窗外"),
                shot("镜头二：推近窗边的咖啡杯", mode="i2v", referenceAssetIds=[image_id]),
            ],
        )
        workspace_context = m2_run(store, "workspace-shots", mode="live")
        async with live_service(store, clock, fail_http) as (service, _):
            tools = register_video_tools(create_project_tools(), service, mode="live")
            attach_signature(store, "workspace-shots", tools, signatures)
            result = service.shot_generate(
                generation_args(saved),
                context=next_context(workspace_context, "batch"),
            )
            assert result["ok"], result
        revised = revise_storyboard(
            registry,
            store,
            saved,
            [
                shot("镜头一：雨夜咖啡店窗外，镜头缓慢推近", id=saved["shotIds"][0]),
                shot(
                    "镜头二：推近窗边的咖啡杯",
                    id=saved["shotIds"][1],
                    mode="i2v",
                    referenceAssetIds=[image_id],
                ),
            ],
        )
        shared_context = m2_run(store, "shared-shots", mode="live")
        async with live_service(store, clock, fail_http, video_workspace_id=None) as (service, _):
            tools = register_video_tools(create_project_tools(), service, mode="live")
            attach_signature(store, "shared-shots", tools, signatures)
            generated = service.shot_generate(generation_args(revised), context=shared_context)
            assert generated["ok"], generated
            redo_context = m2_run(store, "shared-redo", mode="live")
            tools = register_video_tools(create_project_tools(), service, mode="live")
            attach_signature(store, "shared-redo", tools, signatures)
            board = store.snapshot()["storyboards"][saved["storyboardId"]]
            redone = service.redo_shot(
                redo_args(revised, revised["shotIds"][0], revision=board["revision"]),
                context=redo_context,
            )
            assert redone["ok"], redone
            single_context = live_run(store, "shared-single")
            tools = register_video_tools(create_project_tools(), service, mode="live")
            attach_signature(store, "shared-single", tools, signatures)
            single = service.generate(live_arguments(), context=single_context)
            assert single["ok"], single
            wait_context = next_context(shared_context, "await-set", modelStep=2)
            tools = register_video_tools(create_project_tools(), service, mode="live")
            deferred = tools.execute(
                "await_job",
                {"jobIds": [item["jobId"] for item in generated["data"]["shots"]]},
                store=store,
                project_id="coffee",
                operation_key=wait_context.operation_key,
                context=wait_context,
            )
            assert deferred.kind == "deferred", deferred
            store.transaction(
                lambda draft: draft["runs"][shared_context.run_id].update(
                    status="waiting_external", activeWaitId=deferred.wait_id,
                    externalWaitStartedAt=TIMESTAMP,
                )
            )
        return store.snapshot(), signatures


def describe(filename, raw, state, signatures=None, source=None):
    Database.model_validate(state)
    return {
        "file": filename,
        "schemaVersion": 4,
        "source": source or "synthetic-public-builders-registration-only",
        "rawSha256": hashlib.sha256(raw).hexdigest(),
        "canonicalSha256": canonical_fingerprint(state),
        "expectedV5CanonicalSha256": canonical_fingerprint(
            {**state, "schemaVersion": 5, "films": {}}
        ),
        "collectionsSha256": {
            key: canonical_fingerprint(value) for key, value in state.items() if key != "schemaVersion"
        },
        "collectionCounts": {key: len(value) for key, value in state.items() if key != "schemaVersion"},
        "jobContracts": {key: job["contractVersion"] for key, job in state["jobs"].items()},
        "jobRequestFingerprints": {key: job["requestFingerprint"] for key, job in state["jobs"].items()},
        "jobIntentFingerprints": {key: job.get("intentFingerprint") for key, job in state["jobs"].items()},
        "operationFingerprints": {key: op["fingerprint"] for key, op in state["operations"].items()},
        "mediaSha256": {key: media["sha256"] for key, media in state["media"].items()},
        "assetsSha256": {key: asset["sha256"] for key, asset in state["assets"].items()},
        "storyboardVersions": {
            key: {
                "currentVersion": board["currentVersion"],
                "revision": board["revision"],
                "versionsSha256": canonical_fingerprint(board["versions"]),
                "shotVersionsSha256": canonical_fingerprint(board["shotVersions"]),
            }
            for key, board in state["storyboards"].items()
        },
        "runSignatures": {
            key: {
                "contextSignature": run.get("contextSignature"),
                "executionVersion": run.get("executionVersion"),
                "contextVersion": run.get("contextVersion", 1),
                "toolFeaturesSha256": canonical_fingerprint(run.get("toolFeatures", {})),
            }
            for key, run in state["runs"].items()
        },
        "computedRunnerProfiles": signatures or {},
    }


def main():
    DESTINATION.mkdir(parents=True, exist_ok=True)
    snapshots = []
    for mode in ("mock", "live"):
        source = f"legacy-v3-{mode}.json"
        state = migrated_state(json.loads((ROOT / "tests" / "fixtures" / "m2" / source).read_bytes()))
        filename = f"legacy-v4-{mode}.json"
        raw = write_json(DESTINATION / filename, state)
        snapshots.append(describe(filename, raw, state, source=f"../m2/{source}"))
    counter = iter(range(1, 1000))

    def fixture_uuid():
        return uuid5(NAMESPACE_URL, f"vagent-m3-e0-baseline-{next(counter)}")

    with TemporaryDirectory(prefix="vagent-m3-e0-") as temporary, ExitStack() as stack:
        for module in ("shot_support", "vagent.video.storyboards", "vagent.video.jobs", "vagent.video.tools"):
            stack.enter_context(patch(f"{module}.uuid4", fixture_uuid))
        stack.enter_context(patch("vagent.video.storyboards.utc_now", lambda: TIMESTAMP))
        state, signatures = asyncio.run(current_snapshot(Path(temporary)))
    filename = "current-v4-shots.json"
    raw = write_json(DESTINATION / filename, state)
    snapshots.append(describe(filename, raw, state, signatures))
    write_json(
        DESTINATION / "manifest.json",
        {
            "fixtureVersion": 1,
            "capturedFrom": CAPTURE_COMMIT,
            "capturedOn": "2026-10-21",
            "kind": "synthetic-v4-domain-baseline-only",
            "checkpointsIncluded": False,
            "binaryAssetsIncluded": False,
            "realProviderRequests": 0,
            "notes": (
                "E0 baseline before M3. Historical v3 domain JSON is copied unchanged into v4 envelopes. "
                "Current fixtures use fixed UUIDs/time and repository test builders; every HTTP call fails. "
                "Image records describe synthetic PNG headers; media records reuse the committed M2 synthetic "
                "delivery. No application data, credentials, pixels, SQLite checkpoints or resumable Run "
                "evidence are included. Legacy signatures/checkpoints remain labelled domain sentinels; "
                "computedRunnerProfiles pin real signatures computed from synthetic current registries, "
                "built-in skill metadata and model identity. expectedV5CanonicalSha256 is only the "
                "mechanical schemaVersion=5, films={} envelope expectation, not a migration result. "
                "Do not regenerate to make later changes pass."
            ),
            "snapshots": snapshots,
        },
    )


if __name__ == "__main__":
    main()
