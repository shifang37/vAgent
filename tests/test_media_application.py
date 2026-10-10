import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from conftest import ScriptedModel, tool_call
from job_support import eventually
from langchain_core.messages import AIMessage, ToolMessage, messages_from_dict
from wan_support import live_arguments, live_config

from vagent.application import ApplicationService
from vagent.storage import Artifact, ArtifactVersion, now
from vagent.video.media import DownloadRetry


def wait_model(remembered, *, expected_error=None):
    def respond(messages, step):
        if step == 0:
            return tool_call(
                "video_generate", live_arguments(**remembered.get("arguments", {})), call_id="generate"
            )
        if step == 1:
            remembered["jobId"] = json.loads(messages[-1].content)["data"]["jobId"]
            return tool_call("await_job", {"jobId": remembered["jobId"]}, call_id="original-media-wait")
        assert step == 2 and isinstance(messages[-1], ToolMessage)
        assert messages[-1].tool_call_id == "original-media-wait"
        result = json.loads(messages[-1].content)
        if expected_error:
            assert result["error"]["code"] == expected_error
        else:
            assert result["ok"] and result["data"]["mediaAvailable"]
            assert not result["data"]["simulated"] and len(result["data"]["mediaRefs"]) == 1
        return AIMessage(content="结果已确认。")

    return ScriptedModel(respond)


async def waiting(service, remembered, runtime):
    run = await service.start("coffee", "生成一个单镜头视频并等待完成", "media-request")
    await service.task
    await eventually(
        lambda: runtime.downloads and service.run_record(run["id"])["status"] == "waiting_external"
    )
    assert service.video_jobs.get(remembered["jobId"]).status == "downloading"
    return run


async def test_delivery_resumes_original_tool_with_frozen_source_and_survives_restart(
    tmp_path, media_runtime
):
    media_runtime.gate = asyncio.Event()
    remembered = {"arguments": {"sourceRefs": [{"artifactId": "source", "version": 1}]}}
    model = wait_model(remembered)
    config = live_config(tmp_path)
    async with ApplicationService.open(config, model=model) as service:
        service.store.ensure_session("coffee")
        source = Artifact(
            id="source",
            projectId="coffee",
            kind="storyboard",
            versions=[
                ArtifactVersion(version=1, title="原镜头", content="固定来源版本。", createdAt=now()),
            ],
        )
        service.store.transaction(
            lambda draft: draft["artifacts"].__setitem__(
                "source", source.model_dump(mode="json", by_alias=True)
            )
        )
        run = await waiting(service, remembered, media_runtime)
        before = service.run_record(run["id"])
        service.store.transaction(
            lambda draft: draft["artifacts"]["source"]["versions"].append(
                ArtifactVersion(
                    version=2, title="后来修改", content="新版本不能替换原视频来源。", createdAt=now()
                ).model_dump(mode="json", by_alias=True)
            )
        )
        media_runtime.gate.set()
        result = await service.wait_for_run(run["id"])
        assert result["status"] == "completed" and model.calls == 3 and result["toolCalls"] == 2
        assert result["policy"] == before["policy"] and result["activeSeconds"] >= before["activeSeconds"]
        job = service.video_jobs.get(remembered["jobId"])
        asset = service.media.asset(job.download.media_id)
        assert asset.source_refs[0].version == 1 and job.result.source_refs == asset.source_refs
        assert len(media_runtime.submissions) == 1 and len(media_runtime.downloads) == 1
        original_operations = service.store.snapshot()["operations"]
        delivered = [
            message
            for message in messages_from_dict(result["messages"])
            if isinstance(message, ToolMessage) and message.tool_call_id == "original-media-wait"
        ]
        assert len(delivered) == 1
        assert "Signature" not in str(result["messages"])
    async with ApplicationService.open(
        replace(config, video_mode="off", video_api_key=None), model=model
    ) as service:
        assert service.job(job.id)["mediaAvailable"]
        assert service.store.snapshot()["operations"] == original_operations and model.calls == 3
        assert service.video_jobs.get(job.id).download.media_id == asset.id
        assert len(media_runtime.submissions) == 1


async def test_download_failure_then_manual_repair_does_not_rewrite_delivered_wait(tmp_path, media_runtime):
    media_runtime.gate, media_runtime.status = asyncio.Event(), 404
    remembered = {}
    model = wait_model(remembered, expected_error="JOB_DOWNLOAD_FAILED")
    async with ApplicationService.open(live_config(tmp_path), model=model) as service:
        run = await waiting(service, remembered, media_runtime)
        media_runtime.gate.set()
        finished = await service.wait_for_run(run["id"])
        assert finished["status"] == "completed" and model.calls == 3
        original = service.store.snapshot()
        job = service.video_jobs.get(remembered["jobId"])
        assert job.status == "download_failed"
        media_runtime.status = 200
        service.retry_download(
            job.id, DownloadRetry(client_request_id="repair-original", expected_revision=job.revision)
        )
        await eventually(lambda: service.video_jobs.get(job.id).status == "succeeded")
        current = service.store.snapshot()
        assert current["runs"] == original["runs"] and current["waits"] == original["waits"]
        for key, operation in original["operations"].items():
            assert current["operations"][key] == operation
        assert model.calls == 3 and len(media_runtime.submissions) == 1 and len(media_runtime.downloads) == 2


@pytest.mark.parametrize("ending", ["stop", "timeout"])
async def test_download_can_finish_after_stopped_or_timed_out_wait(tmp_path, media_runtime, ending):
    media_runtime.gate = asyncio.Event()
    remembered = {}
    model = wait_model(remembered, expected_error="JOB_WAIT_TIMEOUT" if ending == "timeout" else None)
    async with ApplicationService.open(live_config(tmp_path), model=model) as service:
        run = await waiting(service, remembered, media_runtime)
        if ending == "stop":
            service.stop(run["id"])
            await eventually(lambda: service.run_record(run["id"])["status"] != "waiting_external")
        else:
            binding = next(iter(service.store.snapshot()["waits"].values()))
            service.wait_coordinator.waits.clock = lambda: (
                datetime.fromisoformat(binding["deadlineAt"]) + timedelta(seconds=1)
            )
            await service.wait_coordinator.run_once()
            await service.wait_for_run(run["id"])
        prior = service.store.snapshot()
        calls = model.calls
        media_runtime.gate.set()
        await eventually(lambda: service.video_jobs.get(remembered["jobId"]).status == "succeeded")
        assert service.store.snapshot()["runs"] == prior["runs"]
        assert service.store.snapshot()["waits"] == prior["waits"]
        assert model.calls == calls == (2 if ending == "stop" else 3)
        assert len(media_runtime.submissions) == 1


async def test_restart_during_download_consumes_attempt_and_resumes_original_wait(
    tmp_path, media_runtime, video_clock
):
    media_runtime.gate = asyncio.Event()
    remembered = {}
    model = wait_model(remembered)
    config = live_config(tmp_path)
    async with ApplicationService.open(config, model=model) as service:
        run = await waiting(service, remembered, media_runtime)
        initial = service.video_jobs.get(remembered["jobId"])
        record = service.run_record(run["id"])
        assert initial.download.phase == "writing" and initial.download.attempts == 1
    async with ApplicationService.open(config, model=model) as service:
        recovered = service.video_jobs.get(initial.id)
        assert recovered.download.attempts == 1 and recovered.download.phase == "pending"
        assert datetime.fromisoformat(recovered.download.next_attempt_at) >= datetime.fromisoformat(
            initial.download.deadline_at
        )
        assert service.run_record(run["id"])["activeSeconds"] == record["activeSeconds"]
        assert len(media_runtime.downloads) == 1
        media_runtime.gate.set()
        video_clock.value = datetime.fromisoformat(recovered.download.next_attempt_at)
        result = await service.wait_for_run(run["id"])
        assert result["status"] == "completed" and model.calls == 3
        final = service.video_jobs.get(initial.id)
        assert final.download.attempts == 2 and final.download.media_id == initial.download.media_id
        assert len(media_runtime.submissions) == 1 and len(media_runtime.downloads) == 2
