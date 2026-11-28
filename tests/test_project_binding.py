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


async def test_service_create_session_start_and_projects_entry_points(store):
    """P3 service entry points: create_session(project_id), start(project_id) and projects()."""
    config = Config(home=store.home, api_key=None)
    model = project_read_model()
    async with ApplicationService.open(config, model=model, existing_store=store) as service:
        first = service.create_session()
        project_id = first["projectId"]
        assert first["id"] == project_id

        def seed(draft):
            draft["projects"][project_id]["goal"] = "雨夜侦探短剧"
            draft["memories"][project_id] = {
                "id": project_id,
                "revision": 1,
                "entries": [{"key": "tone", "kind": "decision", "text": "雨夜冷调"}],
                "updatedAt": "2026-11-27T00:00:00+00:00",
            }

        store.transaction(seed)

        # create_session(project_id=...) joins the existing project without creating a new one.
        second = service.create_session(project_id=project_id)
        assert second["id"] != project_id and second["projectId"] == project_id
        assert second["id"] not in store.snapshot()["projects"]

        # start(project_id=...) binds a fresh session on first run and reuses that project.
        await service.start("ep03", "读取项目继续", "p3-start-1", project_id=project_id)
        await service.task
        assert model.result["ok"] and model.result["data"]["id"] == project_id
        assert store.snapshot()["sessions"]["ep03"]["projectId"] == project_id

        # Omitting project_id on a follow-up start keeps the recorded binding (D6).
        await service.start("ep03", "继续下一镜", "p3-start-2")
        await service.task
        assert store.snapshot()["sessions"]["ep03"]["projectId"] == project_id
        assert "ep03" not in store.snapshot()["projects"]

        # Missing project and conflicting project are refused before creating runs or projects.
        with pytest.raises(AppError) as missing:
            service.create_session(project_id="missing-project")
        assert missing.value.code == "PROJECT_NOT_FOUND"

        other = service.create_session()
        with pytest.raises(AppError) as conflict:
            await service.start("ep03", "换项目", "p3-conflict", project_id=other["projectId"])
        assert conflict.value.code == "SESSION_PROJECT_CONFLICT"

        # projects() summarizes sessions and ledger counts; session() exposes memory ledger.
        listing = {row["id"]: row for row in service.projects()}
        assert listing[project_id]["title"] == "雨夜侦探短剧"
        assert listing[project_id]["sessionCount"] == 3
        assert listing[project_id]["memoryRevision"] == 1
        assert listing[project_id]["memoryEntries"] == 1
        view = service.session(second["id"])
        assert view["projectId"] == project_id
        assert view["memory"]["revision"] == 1
        assert view["memory"]["entries"][0]["text"] == "雨夜冷调"


def test_cli_parser_and_project_binding_precedence_d6(store, monkeypatch):
    """P-11: CLI --project parses on run/chat, keeps -s default intact and enforces D6 precedence."""
    from vagent import cli
    from vagent.context import PROJECT_MEMORY_HEADER

    parsed_default = cli.parser().parse_args(["run", "写方案"])
    assert parsed_default.session == "default" and parsed_default.project is None
    parsed_run = cli.parser().parse_args(["run", "写第 3 集", "-s", "ep03", "--project", PROJECT])
    assert parsed_run.session == "ep03" and parsed_run.project == PROJECT
    parsed_chat = cli.parser().parse_args(["chat", "-s", "ep03", "--project", PROJECT])
    assert parsed_chat.session == "ep03" and parsed_chat.project == PROJECT

    store.ensure_session(PROJECT)
    store.ensure_session("other")

    def seed(draft):
        draft["projects"][PROJECT]["goal"] = "第 3 集雨夜续拍"
        draft["memories"][PROJECT] = {
            "id": PROJECT,
            "revision": 2,
            "entries": [{"key": "rule-color", "kind": "decision", "text": "霓虹偏冷青"}],
            "updatedAt": "2026-11-27T00:00:00+00:00",
        }

    store.transaction(seed)

    seen_systems = []

    def make_model():
        model = ScriptedModel(
            lambda messages, step: (
                seen_systems.append(messages[0].content) or AIMessage(content="收到")
            )
        )
        model.name = "cli-p11-fixture"
        return model

    monkeypatch.setattr(cli, "load_config", lambda: Config(home=store.home, api_key=None))
    orig_open = ApplicationService.open

    def open_with_model(config, **kwargs):
        kwargs["model"] = make_model()
        return orig_open(config, **kwargs)

    monkeypatch.setattr(cli.ApplicationService, "open", staticmethod(open_with_model))

    # Release the fixture lock before run_agent opens the same home.
    store.close()
    try:
        # 1) Default `-s default` without `--project` creates and binds project `default`.
        assert asyncio.run(cli.run_agent(cli.parser().parse_args(["run", "默认会话"]))) == 0
        # 2) Explicit `--project` binds new session `ep03` to `coffee` and sees its memory.
        assert (
            asyncio.run(
                cli.run_agent(cli.parser().parse_args(["run", "继续第 3 集", "-s", "ep03", "--project", PROJECT]))
            )
            == 0
        )
        # 3) Omitting `--project` on the already-bound session keeps its binding (D6).
        assert (
            asyncio.run(cli.run_agent(cli.parser().parse_args(["run", "续跑第 3 集", "-s", "ep03"]))) == 0
        )
        # 4) Passing a different `--project` fails with SESSION_PROJECT_CONFLICT.
        with pytest.raises(AppError) as conflict:
            asyncio.run(
                cli.run_agent(cli.parser().parse_args(["run", "换项目", "-s", "ep03", "--project", "other"]))
            )
        assert conflict.value.code == "SESSION_PROJECT_CONFLICT"
        # 5) Passing an unknown `--project` fails with PROJECT_NOT_FOUND before creating the session.
        with pytest.raises(AppError) as missing:
            asyncio.run(
                cli.run_agent(cli.parser().parse_args(["run", "未知项目", "-s", "ep99", "--project", "nope"]))
            )
        assert missing.value.code == "PROJECT_NOT_FOUND"
    finally:
        from vagent.storage import FileStore

        with FileStore.open(store.home) as reopened:
            state = reopened.snapshot()

    assert state["sessions"]["default"]["projectId"] == "default"
    assert state["sessions"]["ep03"]["projectId"] == PROJECT
    assert "ep03" not in state["projects"]
    assert "ep99" not in state["sessions"]
    assert PROJECT_MEMORY_HEADER not in seen_systems[0]
    assert PROJECT_MEMORY_HEADER in seen_systems[1] and "霓虹偏冷青" in seen_systems[1]
    assert PROJECT_MEMORY_HEADER in seen_systems[2] and "霓虹偏冷青" in seen_systems[2]


async def test_cli_chat_forwards_project_flag(store, monkeypatch):
    """P-11: `vagent chat -s <id> --project <project>` binds and passes project_id on each turn."""
    from vagent import cli
    from vagent.context import PROJECT_MEMORY_HEADER

    store.ensure_session(PROJECT)

    def seed(draft):
        draft["memories"][PROJECT] = {
            "id": PROJECT,
            "revision": 1,
            "entries": [{"key": "chat-rule", "kind": "fact", "text": "男主左手有疤"}],
            "updatedAt": "2026-11-27T00:00:00+00:00",
        }

    store.transaction(seed)

    seen_systems = []
    prompts = iter(["我们设定了什么？", "/exit"])

    async def fake_read_prompt(_label):
        return next(prompts)

    def make_model():
        model = ScriptedModel(
            lambda messages, step: (
                seen_systems.append(messages[0].content) or AIMessage(content="男主左手有疤。")
            )
        )
        model.name = "cli-chat-p11-fixture"
        return model

    monkeypatch.setattr(cli, "load_config", lambda: Config(home=store.home, api_key=None))
    monkeypatch.setattr(cli, "read_prompt", fake_read_prompt)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    orig_open = ApplicationService.open

    def open_with_model(config, **kwargs):
        kwargs["model"] = make_model()
        return orig_open(config, **kwargs)

    monkeypatch.setattr(cli.ApplicationService, "open", staticmethod(open_with_model))

    store.close()
    try:
        code = await cli.run_agent(cli.parser().parse_args(["chat", "-s", "chat-ep02", "--project", PROJECT]))
        assert code == 0
    finally:
        from vagent.storage import FileStore

        with FileStore.open(store.home) as reopened:
            state = reopened.snapshot()

    assert state["sessions"]["chat-ep02"]["projectId"] == PROJECT
    assert "chat-ep02" not in state["projects"]
    assert len(seen_systems) == 1
    assert PROJECT_MEMORY_HEADER in seen_systems[0] and "男主左手有疤" in seen_systems[0]
