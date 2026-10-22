"""Capture E0's synthetic v5 domain baseline; never use an application data directory.

Run once on the post-M4 implementation. Committed snapshots are golden evidence,
not outputs to regenerate when a later migration or compatibility check fails.
Every HTTP call in this script fails on purpose: Jobs are registered only, and the
film is assembled locally by the frozen M4 normalizing path.

The v5 envelope is what M5 must preserve: the historical fixtures are copied into
it unchanged, and the current fixture adds the M4 film surface (delivered clips,
FilmMedia union, ffmpeg-concat-normalize-v4 record) plus real 6/6 tool signatures.
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

from conftest import VideoClock, video_run  # noqa: E402
from film_support import assembly_args, delivered_pair  # noqa: E402
from shot_support import mock_tools  # noqa: E402
from wan_support import live_service  # noqa: E402

import vagent.video.film_service as film_service_module  # noqa: E402
from vagent.context import ContextBuilder  # noqa: E402
from vagent.runner import AgentRunner  # noqa: E402
from vagent.skills import SkillCatalog, register_skill_tool  # noqa: E402
from vagent.storage import (  # noqa: E402
    DatabaseV5,
    FileStore,
)
from vagent.tools import create_project_tools  # noqa: E402
from vagent.video.assembler import (  # noqa: E402
    AssemblyError,
)
from vagent.video.contracts import canonical_fingerprint  # noqa: E402
from vagent.video.film_service import FilmService  # noqa: E402
from vagent.video.jobs import JobService  # noqa: E402
from vagent.video.providers.mock import MockVideoAdapter  # noqa: E402
from vagent.video.tools import register_video_tools  # noqa: E402

DESTINATION = ROOT / "tests" / "fixtures" / "m5"
M3_FIXTURES = ROOT / "tests" / "fixtures" / "m3"
M2_FIXTURES = ROOT / "tests" / "fixtures" / "m2"
CAPTURE_COMMIT = "00d087f"
TIMESTAMP = "2030-01-01T00:00:00+00:00"
V6_ADDED = ("episodes", "scenes", "characters", "styles")


def write_json(path: Path, value: dict) -> bytes:
    raw = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()
    path.write_bytes(raw)
    return raw


def fail_http(request):
    raise AssertionError(f"E0 fixture capture must not send HTTP: {request.method}")


def v5_envelope(state: dict) -> dict:
    """The pre-M5 envelope: every collection M4 had, and nothing from M5."""
    wrapped = {key: value for key, value in state.items() if key not in V6_ADDED}
    wrapped.setdefault("films", {})
    wrapped["schemaVersion"] = 5
    return wrapped


def attach_signature(store, run_id, registry, profiles):
    skills = SkillCatalog.discover(ROOT / "skills")
    registry = register_skill_tool(registry, skills)
    runner = AgentRunner(
        store=store,
        model=SimpleNamespace(name="m5-baseline-fixture"),
        tools=registry,
        skills=skills.list(),
        execution_version=3,
        context=ContextBuilder(format_version=2),
    )
    signature = runner.context_signature()
    profiles[run_id] = {
        "kind": "computed-synthetic-runner",
        "model": runner.model.name,
        "executionVersion": 3,
        "contextVersion": 2,
        "contextSignature": signature,
        "toolFeaturesSha256": canonical_fingerprint(runner.tools.features),
        "videoToolsVersion": runner.tools.features["video"]["toolsVersion"],
        "videoRulesVersion": runner.tools.features["video"]["rulesVersion"],
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


async def current_snapshot(home: Path) -> tuple[dict, dict]:
    """A fresh store carrying the M4 film surface and real 6/6 tool signatures."""
    clock = VideoClock()
    profiles: dict[str, dict] = {}
    with FileStore.open(home) as store:
        registry, _service, _adapter, _context = mock_tools(store, clock=clock)
        pair = await delivered_pair(store, clock)
        video_run(store, "mock-run6")
        attach_signature(store, "mock-run6", registry, profiles)
        async with live_service(store, clock, fail_http) as (service, _):
            # A fresh registry per Run keeps the skill tool registration unique.
            for run_id in ("film-run-1", "film-run-2"):
                live_tools = register_video_tools(create_project_tools(), service, mode="live")
                attach_signature(store, run_id, live_tools, profiles)

            # Force the frozen M4 fallback so the baseline carries the normalizing
            # assembly record instead of the copy path.
            service_films = FilmService(JobService(store, [MockVideoAdapter(store)]))
            original = film_service_module.plan_assembly

            def incompatible(signatures):
                raise AssemblyError("FILM_INPUT_INCOMPATIBLE", "forced by the E0 baseline")

            film_service_module.plan_assembly = incompatible
            try:
                args = assembly_args(pair)
                assembled = await service_films.assemble(
                    project_id="coffee",
                    storyboard_id=args["storyboardId"],
                    expected_revision=args["expectedRevision"],
                    operation_key="film-normalize",
                    args=args,
                )
            finally:
                film_service_module.plan_assembly = original
            assert assembled["ok"], assembled
        return store.snapshot(), profiles


def describe(filename: str, raw: bytes, state: dict, profiles=None, source=None) -> dict:
    DatabaseV5.model_validate(state)
    snapshot = {
        "file": filename,
        "schemaVersion": 5,
        "source": source or "synthetic-public-builders-registration-only",
        "rawSha256": hashlib.sha256(raw).hexdigest(),
        "canonicalSha256": canonical_fingerprint(state),
        "expectedV6CanonicalSha256": canonical_fingerprint(
            {**state, "schemaVersion": 6, **{name: {} for name in V6_ADDED}}
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
        "films": {
            key: {
                "storyboardId": film["storyboardId"],
                "currentVersion": film["currentVersion"],
                "revision": film["revision"],
                "versionsSha256": canonical_fingerprint(film["versions"]),
                "assemblyVersions": [item["assembly"]["assemblyVersion"] for item in film["versions"]],
                "normalizationProfiles": [
                    (item["assembly"].get("normalization") or {}).get("profile") for item in film["versions"]
                ],
                "outputMediaIds": [item["outputMediaId"] for item in film["versions"]],
            }
            for key, film in state["films"].items()
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
        "computedRunnerProfiles": profiles or {},
    }
    return snapshot


def main() -> None:
    DESTINATION.mkdir(parents=True, exist_ok=True)
    snapshots = []
    for mode in ("mock", "live"):
        source = f"legacy-v4-{mode}.json"
        state = v5_envelope(json.loads((M3_FIXTURES / source).read_bytes()))
        filename = f"legacy-v5-{mode}.json"
        raw = write_json(DESTINATION / filename, state)
        snapshots.append(describe(filename, raw, state, source=f"../m3/{source}"))

    # The M3 synthetic shot corpus keeps its 4/4 history untouched in the v5 envelope.
    source = "current-v4-shots.json"
    state = v5_envelope(json.loads((M3_FIXTURES / source).read_bytes()))
    filename = "current-v5-shots.json"
    raw = write_json(DESTINATION / filename, state)
    snapshots.append(describe(filename, raw, state, source=f"../m3/{source}"))

    counter = iter(range(1, 5000))

    def fixture_uuid():
        return uuid5(NAMESPACE_URL, f"vagent-m5-e0-baseline-{next(counter)}")

    modules = (
        "film_support",
        "shot_support",
        "media_support",
        "wan_support",
        "vagent.video.storyboards",
        "vagent.video.jobs",
        "vagent.video.tools",
        "vagent.video.films",
        "vagent.video.film_service",
        "vagent.video.media",
        "vagent.video.media_worker",
        "vagent.video.worker",
        "vagent.video.assets",
    )
    with TemporaryDirectory(prefix="vagent-m5-e0-") as temporary, ExitStack() as stack:
        for module in modules:
            try:
                stack.enter_context(patch(f"{module}.uuid4", fixture_uuid))
            except AttributeError:
                continue
        stack.enter_context(patch("vagent.video.storyboards.utc_now", lambda: TIMESTAMP))
        state, profiles = asyncio.run(current_snapshot(Path(temporary)))
    state = v5_envelope(state)
    # The assembled film is a real local product; its bytes are not shipped here.
    filename = "current-v5-films.json"
    raw = write_json(DESTINATION / filename, state)
    snapshots.append(describe(filename, raw, state, profiles))

    write_json(
        DESTINATION / "manifest.json",
        {
            "fixtureVersion": 1,
            "capturedFrom": CAPTURE_COMMIT,
            "capturedOn": "2026-10-22",
            "kind": "synthetic-v5-domain-baseline-only",
            "checkpointsIncluded": False,
            "binaryAssetsIncluded": False,
            "realProviderRequests": 0,
            "notes": (
                "M5 E0 baseline taken after M4. Historical v4 domain JSON is copied unchanged into v5 "
                "envelopes; the current fixture is built by the frozen live pipeline with a transport that "
                "rejects every request, plus one locally normalized film assembled by bundled FFmpeg. "
                "The delivered clips are copies of the committed M1-C sample; no application data, "
                "credentials, SQLite checkpoints or resumable Run evidence is included. computedRunnerProfiles "
                "pin real 6/6 toolFeatures signatures. expectedV6CanonicalSha256 is only the mechanical "
                "schemaVersion=6 plus four empty episode collections expectation, not a migration result. "
                "The film bytes and their SHA-256 depend on this capture only; do not regenerate this "
                "corpus to make a later change pass."
            ),
            "snapshots": snapshots,
        },
    )


if __name__ == "__main__":
    main()
