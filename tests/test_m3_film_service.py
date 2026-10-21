"""E1: film assembly through the service — atomic commit, idempotency and versions."""

import hashlib
import os
import sys

import pytest
from film_support import assembly_args, delivered_pair, link_shot_versions
from shot_support import mock_tools, revise_storyboard, save_storyboard, shot

from vagent.errors import AppError
from vagent.video import clients
from vagent.video.assembler import bundled_engine
from vagent.video.film_service import FilmService
from vagent.video.films import cleanup_stale_parts
from vagent.video.jobs import JobService
from vagent.video.providers.mock import MockVideoAdapter

WINDOWS_FFMPEG_SHA256 = "2ce797a0f88d7f067180338fb227f7b1928ea727bd9a4d7a1d022f7c52af71a3"
TWO_CLIP_SHA256 = "41be1ed90fa7abfcdb3b3d46775d96695ca3e441f92adb15363be400ec21fd8a"


def film_service(store):
    return FilmService(JobService(store, [MockVideoAdapter(store)]))


def engine_available():
    try:
        return bundled_engine()
    except AppError:
        return None


requires_ffmpeg = pytest.mark.skipif(engine_available() is None, reason="bundled FFmpeg is unavailable")


async def assemble(service, fixture, *, revision=None, key="film-op-1"):
    args = assembly_args(fixture, revision=revision)
    return await service.assemble(
        project_id="coffee",
        storyboard_id=args["storyboardId"],
        expected_revision=args["expectedRevision"],
        operation_key=key,
        args=args,
    )


@requires_ffmpeg
async def test_film_assembly_commits_one_version_media_and_operation(store):
    fixture = await delivered_pair(store)
    service = film_service(store)
    jobs_before = len(store.snapshot()["jobs"])
    result = await assemble(service, fixture)
    assert result["ok"], result
    data = result["data"]
    assert data["currentVersion"] == 1 and data["versionCount"] == 1
    latest = data["latest"]
    assert latest["mediaAvailable"] is True
    assert [item["shotId"] for item in latest["inputs"]] == fixture["shotIds"]
    assert [item["mediaId"] for item in latest["inputs"]] == fixture["mediaIds"]
    assert [round(item["startSeconds"], 6) for item in latest["timeline"]] == [0.0, 5.021333]
    assert [round(item["durationSeconds"], 6) for item in latest["timeline"]] == [5.021333, 5.021333]
    assert latest["engine"]["packageVersion"] == "0.6.0"
    assert latest["mediaUrl"] == f"/api/media/{latest['outputMediaId']}/content"
    state = store.snapshot()
    assert len(state["films"]) == 1 and len(state["media"]) == 3  # two clips plus the film
    assert len(state["jobs"]) == jobs_before  # a film never registers a generation Job
    film_media = state["media"][latest["outputMediaId"]]
    assert film_media["kind"] == "film" and film_media["contractVersion"] == 2
    assert film_media["filmVersion"] == 1 and film_media["validationVersion"] == "mp4-avc-v1"
    path = store.home / film_media["relativePath"]
    assert path.is_file() and not path.with_name(path.name + ".part").exists()
    assert film_media["sizeBytes"] == os.path.getsize(path)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == film_media["sha256"]
    assert film_media["metadata"]["durationSeconds"] == pytest.approx(10.0212890625)
    if sys.platform == "win32":
        engine = bundled_engine()
        if engine.binary_sha256 == WINDOWS_FFMPEG_SHA256:
            assert film_media["sha256"] == TWO_CLIP_SHA256
    # The committed Operation replays the same view instead of assembling again.
    replay = await assemble(service, fixture)
    assert replay == result
    assert len(store.snapshot()["films"]) == 1


@requires_ffmpeg
async def test_same_inputs_replay_the_existing_version_without_a_new_file(store):
    fixture = await delivered_pair(store)
    service = film_service(store)
    first = (await assemble(service, fixture, key="film-op-a"))["data"]
    files = {path.name for path in (store.home / "media" / "coffee").iterdir()}
    second = (
        await service.assemble(
            project_id="coffee",
            storyboard_id=fixture["storyboardId"],
            expected_revision=fixture["revision"],
            operation_key="film-op-b",
            args=assembly_args(fixture),
        )
    )["data"]
    assert second["currentVersion"] == first["currentVersion"] == 1
    assert second["latest"]["outputMediaId"] == first["latest"]["outputMediaId"]
    assert {path.name for path in (store.home / "media" / "coffee").iterdir()} == files
    assert len(store.snapshot()["films"]) == 1


@requires_ffmpeg
async def test_a_changed_storyboard_revision_appends_a_new_version(store):
    fixture = await delivered_pair(store)
    service = film_service(store)
    first = (await assemble(service, fixture))["data"]
    registry, _jobs, _adapter, _context = mock_tools(store)
    revised = revise_storyboard(
        registry,
        store,
        {"storyboardId": fixture["storyboardId"], "version": 1, "shotIds": fixture["shotIds"]},
        [
            {"id": shot_id, **shot(description)}
            for shot_id, description in zip(fixture["shotIds"], ["雨夜街道", "推门入店"], strict=True)
        ],
        key="film-storyboard-revise",
    )
    jobs = fixture["jobs"]
    revision = link_shot_versions(
        store,
        storyboard_id=fixture["storyboardId"],
        storyboard_version=2,
        entries=[
            (
                shot_id,
                job.id,
                job.result.media_refs[0].media_id,
                job.request.model_dump(mode="json", by_alias=True),
            )
            for shot_id, job in zip(fixture["shotIds"], jobs, strict=True)
        ],
    )
    second = (await assemble(service, fixture, revision=revision, key="film-op-2"))["data"]
    assert revised["version"] == 2
    assert second["currentVersion"] == 2 and second["versionCount"] == 2
    assert second["latest"]["outputMediaId"] != first["latest"]["outputMediaId"]
    assert second["versions"][0] == first["latest"]  # The earlier version and file stay intact
    assert (store.home / "media" / "coffee" / f"{first['latest']['outputMediaId']}.mp4").is_file()
    state = store.snapshot()
    assert state["films"][second["filmId"]]["revision"] == 2


@requires_ffmpeg
async def test_stale_revision_is_rejected_without_touching_the_film(store):
    fixture = await delivered_pair(store)
    service = film_service(store)
    with pytest.raises(AppError) as error:
        await assemble(service, fixture, revision=fixture["revision"] + 5)
    assert error.value.code == "VERSION_CONFLICT"
    assert not store.snapshot()["films"]


@requires_ffmpeg
async def test_missing_or_unready_shots_refuse_the_whole_assembly(store):
    await delivered_pair(store)
    registry, _jobs, _adapter, _context = mock_tools(store)
    saved = save_storyboard(
        registry, store, [shot("雨夜街道"), shot("推门入店"), shot("收尾")], key="three-shots"
    )
    service = film_service(store)
    with pytest.raises(AppError) as error:
        await service.assemble(
            project_id="coffee",
            storyboard_id=saved["storyboardId"],
            expected_revision=0,
            operation_key="film-op-missing",
            args={"storyboardId": saved["storyboardId"], "expectedRevision": 0},
        )
    assert error.value.code == "FILM_INPUT_MISSING"
    assert not store.snapshot()["films"]
    assert not list((store.home / "media" / "coffee").glob("*.part"))


@requires_ffmpeg
async def test_corrupted_input_file_refuses_assembly_and_keeps_the_index(store):
    fixture = await delivered_pair(store)
    service = film_service(store)
    path = fixture["files"][0]
    original = path.read_bytes()
    path.write_bytes(original[:-16])
    with pytest.raises(AppError) as error:
        await assemble(service, fixture)
    assert error.value.code == "FILM_MEDIA_INVALID"
    assert not store.snapshot()["films"]
    assert not list((store.home / "media" / "coffee").glob("*.part"))


@requires_ffmpeg
async def test_removed_film_file_reports_invalid_instead_of_silently_rebuilding(store):
    fixture = await delivered_pair(store)
    service = film_service(store)
    first = (await assemble(service, fixture))["data"]
    media_id = first["latest"]["outputMediaId"]
    (store.home / "media" / "coffee" / f"{media_id}.mp4").unlink()
    view = service.view("coffee", fixture["storyboardId"])["film"]
    assert view["latest"]["mediaAvailable"] is False and view["latest"]["mediaUrl"] is None
    with pytest.raises(AppError) as error:
        await service.assemble(
            project_id="coffee",
            storyboard_id=fixture["storyboardId"],
            expected_revision=fixture["revision"],
            operation_key="film-op-repair",
            args=assembly_args(fixture),
        )
    assert error.value.code == "FILM_MEDIA_INVALID"
    assert not list((store.home / "media" / "coffee").glob("*.part"))


def test_startup_cleanup_removes_only_unowned_parts(tmp_path):
    media = tmp_path / "media" / "coffee"
    media.mkdir(parents=True)
    stale = media / "11111111-1111-4111-8111-111111111111.mp4.part"
    owned = media / "22222222-2222-4222-8222-222222222222.mp4.part"
    committed = media / "33333333-3333-4333-8333-333333333333.mp4"
    for path in (stale, owned, committed):
        path.write_bytes(b"x")
    state = {"jobs": {"job-1": {"download": {"mediaId": owned.name.removesuffix(".mp4.part")}}}}
    assert cleanup_stale_parts(tmp_path, state) == 1
    assert not stale.exists() and owned.exists() and committed.exists()


async def test_film_view_is_empty_before_the_first_assembly(store):
    fixture = await delivered_pair(store)
    assert film_service(store).view("coffee", fixture["storyboardId"]) == {
        "storyboardId": fixture["storyboardId"],
        "film": None,
    }


@requires_ffmpeg
async def test_client_action_uses_a_local_client_run_and_replays(store):
    fixture = await delivered_pair(store)
    host = _Host(store)

    first = await clients.assemble_film(
        host,
        session_id="coffee",
        client_request_id="assemble-1",
        storyboard_id=fixture["storyboardId"],
        expected_revision=fixture["revision"],
    )
    runs = [run for run in store.snapshot()["runs"].values() if run["model"] == "local-client"]
    assert len(runs) == 1 and runs[0]["status"] == "completed"
    assert first["currentVersion"] == 1 and first["runId"] == runs[0]["id"]
    assert first["latest"]["mediaAvailable"] is True
    replay = await clients.assemble_film(
        host,
        session_id="coffee",
        client_request_id="assemble-1",
        storyboard_id=fixture["storyboardId"],
        expected_revision=fixture["revision"],
    )
    assert replay == first
    assert len(store.snapshot()["films"]) == 1


class _Host:
    """The slice of ApplicationService a local client action touches."""

    def __init__(self, store):
        self.store = store
        self.films = film_service(store)
        self.video_jobs = None

    def generation_mode(self):
        return "mock"

    def capabilities(self, mode):
        return []

    def check_live_configuration(self):
        raise AssertionError("a local film action must not require video configuration")
