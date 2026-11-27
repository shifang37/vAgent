"""P1: a project is not a session; a second session joins an existing project.

These tests are the acceptance for the decoupling phase. The first session fills
a project through the real tool and service paths; a second session binds to that
project via ``ensure_session(project_id=...)`` and must see and continue the same
work, with tool contexts whose ``project_id`` and ``session_id`` differ on purpose.
"""

import asyncio
import json

import pytest
from conftest import ScriptedModel, VideoClock, tool_call, video_request
from film_support import delivered_clip, link_shot_versions
from langchain_core.messages import AIMessage
from shot_support import mock_tools, shot
from subtitle_support import plan, save_bound_storyboard, save_episode, scene_with_dialogue

from vagent.application import ApplicationService
from vagent.config import Config
from vagent.errors import AppError
from vagent.storage import RunRecordV1
from vagent.video.assembler import bundled_engine
from vagent.video.jobs import JobService
from vagent.video.providers.mock import MockVideoAdapter
from vagent.waiting import ToolExecutionContext

PROJECT = "coffee"
NEW_SESSION = "ep02"


def bind(store, *, session_id=NEW_SESSION, project_id=PROJECT, run_id=None):
    """Bind a fresh session to an existing project and give it one durable Run."""
    store.ensure_session(session_id, project_id=project_id)
    run_id = run_id or f"{session_id}-run"
    record = RunRecordV1(
        id=run_id,
        session_id=session_id,
        request_id=run_id,
        prompt="offline binding fixture",
        model="no-model",
        status="completed",
        messages=[],
        model_steps=0,
        tool_calls=0,
        input_tokens=0,
        output_tokens=0,
        answer="",
        created_at="2030-01-01T00:00:00+00:00",
        updated_at="2030-01-01T00:00:00+00:00",
    ).model_dump(by_alias=True)

    def add(draft):
        draft["runs"][run_id] = record
        draft["sessions"][session_id]["latestRunId"] = run_id

    store.transaction(add)
    return ToolExecutionContext(
        project_id=project_id,
        session_id=session_id,
        run_id=run_id,
        model_step=1,
        tool_call_id="binding-1",
    )


def recast(context, *, call_id, project_id=PROJECT, session_id=NEW_SESSION):
    """The same Run under the same session, addressed to another project."""
    return ToolExecutionContext(
        project_id=project_id,
        session_id=session_id,
        run_id=context.run_id,
        model_step=1,
        tool_call_id=call_id,
    )


async def delivered_project(store, clock=None):
    """An episode, a bound storyboard and two really delivered clips (no film)."""
    clock = clock or VideoClock()
    registry, _service, _adapter, _context = mock_tools(store, clock=clock)
    round_value = plan()
    episode = save_episode(registry, store, round_value)
    scene_index = scene_with_dialogue(round_value)
    scene_id = episode["sceneIds"][scene_index]
    saved = save_bound_storyboard(
        registry,
        store,
        scene_id,
        [shot("雨夜街道"), shot("推门入店")],
        key="binding-storyboard",
    )
    first = await delivered_clip(store, clock, run_id="binding-run-1", prompt="雨夜街道")
    second = await delivered_clip(store, clock, run_id="binding-run-2", prompt="推门入店")
    revision = link_shot_versions(
        store,
        storyboard_id=saved["storyboardId"],
        storyboard_version=1,
        entries=[
            (
                shot_id,
                job.id,
                job.result.media_refs[0].media_id,
                job.request.model_dump(mode="json", by_alias=True),
            )
            for shot_id, job in zip(saved["shotIds"], (first, second), strict=True)
        ],
    )
    return {
        "registry": registry,
        "episodeId": episode["episodeId"],
        "sceneId": scene_id,
        "storyboardId": saved["storyboardId"],
        "shotIds": saved["shotIds"],
        "jobs": [first, second],
        "revision": revision,
        "speaker": round_value["scenes"][scene_index]["dialogue"][0]["speaker"],
    }


async def test_new_session_sees_existing_video_state(store):
    """P-13: Jobs, storyboard and episode of the project follow the session binding."""
    fixture = await delivered_project(store)
    context = bind(store)
    project_before = store.snapshot()["projects"][PROJECT]

    config = Config(home=store.home, api_key=None, video_mode="mock")
    async with ApplicationService.open(config, existing_store=store) as service:
        # The aggregate session view reports the shared project, not the session id.
        view = service.session(NEW_SESSION)
        assert view["projectId"] == PROJECT
        assert view["project"]["id"] == PROJECT
        assert view["project"]["revision"] == project_before["revision"]
        assert {job["jobId"] for job in view["jobs"]} >= {job.id for job in fixture["jobs"]}
        assert view["storyboard"]["storyboardId"] == fixture["storyboardId"]
        assert view["episode"]["episodeId"] == fixture["episodeId"]
        # The session list names the same project for both sessions.
        assert {row["projectId"] for row in service.sessions()} == {PROJECT}
        # A preview of the shared project resolves the project's storyboard.
        preview = service.preview_shots(
            PROJECT,
            storyboard_id=fixture["storyboardId"],
            storyboard_version=1,
            shot_ids=fixture["shotIds"],
        )
        assert preview["storyboardId"] == fixture["storyboardId"]
        assert len(preview["shots"]) == len(fixture["shotIds"])

        # Shared tools accept the binding; a mismatched project is still refused.
        read = fixture["registry"].execute(
            "episode_read",
            {"episodeId": fixture["episodeId"]},
            store=store,
            project_id=PROJECT,
            operation_key=context.operation_key,
            context=context,
        )
        assert read["ok"] and read["data"]["episodeId"] == fixture["episodeId"]
        capabilities = fixture["registry"].execute(
            "video_capabilities",
            {},
            store=store,
            project_id=PROJECT,
            operation_key=recast(context, call_id="binding-2").operation_key,
            context=recast(context, call_id="binding-2"),
        )
        assert capabilities["ok"], capabilities
        mismatch = recast(context, call_id="binding-3", project_id=NEW_SESSION)
        refused = fixture["registry"].execute(
            "video_capabilities",
            {},
            store=store,
            project_id=NEW_SESSION,
            operation_key=mismatch.operation_key,
            context=mismatch,
        )
        assert not refused["ok"]
        assert refused["error"]["code"] == "TOOL_CONTEXT_INVALID"

        # A write from the new session lands in the shared project.
        writer = recast(context, call_id="binding-4")
        saved = fixture["registry"].execute(
            "artifact_save",
            {"kind": "brief", "title": "第 3 集大纲", "content": "雨夜追踪，第 3 集。"},
            store=store,
            project_id=PROJECT,
            operation_key=writer.operation_key,
            context=writer,
        )
        assert saved["ok"], saved
        artifact = store.snapshot()["artifacts"][saved["data"]["artifactId"]]
        assert artifact["projectId"] == PROJECT
        assert service.session(NEW_SESSION)["projectId"] == PROJECT

        # Video Jobs accept the binding too: the Job is owned by the project.
        video = JobService(store, [MockVideoAdapter(store)])
        registered = video.generate(
            video_request(MockVideoAdapter(store)), context=recast(context, call_id="binding-5")
        )
        assert registered["ok"], registered
        job = store.snapshot()["jobs"][registered["data"]["jobId"]]
        assert job["context"]["projectId"] == PROJECT
        assert job["context"]["sessionId"] == NEW_SESSION

        # Audio's own context check accepts the binding; it then asks for confirm.
        audio = await fixture["registry"].aexecute(
            "audio_generate",
            {
                "episodeId": fixture["episodeId"],
                "voices": [{"speaker": fixture["speaker"] or "画外", "voiceId": "voice-mock"}],
                "confirm": False,
            },
            store=store,
            project_id=PROJECT,
            operation_key=recast(context, call_id="binding-6").operation_key,
            context=recast(context, call_id="binding-6"),
        )
        assert not audio["ok"]
        assert audio["error"]["code"] == "CONFIRMATION_REQUIRED"


async def can_spawn_piped_process() -> bool:
    """FFmpeg assembly captures its output through pipes; some sandboxes forbid it."""
    try:
        engine = bundled_engine()
    except AppError:
        return False
    try:
        process = await asyncio.create_subprocess_exec(
            str(engine.path),
            "-version",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError:
        return False
    await process.communicate()
    return True


async def test_new_session_sees_existing_film(store):
    """P-13: the assembled film of the project is visible through the new session."""
    if not await can_spawn_piped_process():
        pytest.skip("this environment forbids the piped subprocess FFmpeg assembly needs")
    from subtitle_support import film_with_dialogue

    fixture = await film_with_dialogue(store)
    bind(store)
    config = Config(home=store.home, api_key=None, video_mode="mock")
    async with ApplicationService.open(config, existing_store=store) as service:
        view = service.session(NEW_SESSION)
        scene = next(item for item in view["episode"]["scenes"] if item["sceneId"] == fixture["sceneId"])
        assert scene["film"]["filmId"] == fixture["film"]["filmId"]
        assert scene["film"]["projectId"] == PROJECT
        assert scene["film"]["latest"]["mediaAvailable"] is True
        assert service.film(PROJECT, fixture["storyboardId"])["film"]["filmId"] == fixture["film"]["filmId"]


def project_read_model():
    """A model whose first move reads the project it was started in."""
    model = ScriptedModel(lambda messages, step: None)
    model.name = "binding-fixture"
    model.result = None

    def reply(messages, step):
        if step == 0:
            return tool_call("project_read", {}, call_id="read-project")
        model.result = json.loads(messages[-1].content)
        return AIMessage(content="已读取共享项目。")

    model.callback = reply
    return model


async def test_run_started_in_a_bound_session_works_in_its_project(store):
    """The Runner's context and tools use the session's project, never its own id."""
    store.ensure_session(PROJECT)

    def seed(draft):
        draft["projects"][PROJECT]["goal"] = "雨夜短剧 第三集"

    store.transaction(seed)
    store.ensure_session(NEW_SESSION, project_id=PROJECT)
    model = project_read_model()
    config = Config(home=store.home, api_key=None)
    async with ApplicationService.open(config, model=model, existing_store=store) as service:
        await service.start(NEW_SESSION, "继续第 3 集", "binding-run")
        await service.task

    assert model.result["ok"], model.result
    assert model.result["data"]["id"] == PROJECT
    assert model.result["data"]["goal"] == "雨夜短剧 第三集"
    state = store.snapshot()
    assert NEW_SESSION not in state["projects"]
    assert state["sessions"][NEW_SESSION]["projectId"] == PROJECT
    run = next(run for run in state["runs"].values() if run["sessionId"] == NEW_SESSION)
    assert run["status"] == "completed"
