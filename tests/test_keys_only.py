"""K1 keys-only track: the shared Beijing host beside the frozen workspace host.

The offline chain drives the real application code paths against a controlled Wan
upstream and local test media: no credentials, and no provider submit happens in
any case here (the paid sample is a separate, later approval). V2/V3 byte
identity is pinned against the pre-K commit cc3628e.
"""

import hashlib
import json

import httpx
import pytest
from media_support import SAMPLE
from shot_support import (
    generation_args,
    insert_image,
    jobs_of,
    m2_run,
    mock_tools,
    redo_args,
    save_storyboard,
    shot,
    shot_versions,
    storyboard_record,
)
from wan_support import (
    VIDEO_URL,
    frozen_request,
    live_arguments,
    live_run,
    live_service,
    provider_response,
)

from vagent.errors import AppError
from vagent.storage import FileStore
from vagent.video.contracts import (
    WAN_PROMPT_PREFIX,
    JobV4,
    JobV5,
    ShotSpecV3,
    VideoIntentV2,
    VideoRequestV3,
    VideoRequestV4,
    WanParameters,
    WanParametersV3,
    parse_job,
    wan_shot_capabilities,
)
from vagent.video.media_http import MediaHttpClient
from vagent.video.media_worker import MediaWorker
from vagent.video.providers.wan import WanAdapter
from vagent.video.worker import JobWorker
from vagent.waiting import ToolExecutionContext

PROMPT = "雨夜咖啡店，镜头缓慢推近。"
SHARED_HOST = "dashscope.aliyuncs.com"
DEDICATED_HOST = "test-workspace.cn-beijing.maas.aliyuncs.com"

# Captured on cc3628e with tmp/keys-only-capture-pins.py before the K1 change
# (evidence: output/keys-only-prek-pins.json). V2/V3 must stay byte-identical.
PRE_K_PINS = {
    "v2RequestFingerprint": "288e2c859260a0f7fa54eb28f856d3baac4d8f0a936215c8d021504b885e5ae1",
    "v2IntentFingerprint": "30d42674a6a65d51093b774fa2fc3198cbe5695cc904a9315f2e38d278e3bb6a",
    "v2SerializedSha256": "f23fede712ef8f95320b5c1e698899ab3db3f4e6723460c50b6a749ea4ef1edf",
    "v3RequestFingerprint": "a82826bd2bae35619aabcc17ee91a1b9d7d3670ff1a2f96f245da1b7c01b42c2",
    "v3IntentFingerprint": "b002e141f7fc83f2a07636858e14411547396f2e17a30659ca7f0efda707be9d",
    "v3SerializedSha256": "7767ef57245bc9630ab249e7aa0eb0b1d3a62b1ffd78ab50c9ad7d25fe3e9737",
}


def serialized_sha(value: dict) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def shared_request() -> VideoRequestV4:
    intent = VideoIntentV2.model_validate(live_arguments())
    return VideoRequestV4(
        **intent.model_dump(mode="json", by_alias=True),
        region="cn-beijing",
        adapter_version="wan-http-v1",
        endpoint_profile="beijing-shared-v1",
        provider_prompt=WAN_PROMPT_PREFIX + intent.prompt,
        parameters=WanParameters(),
    )


def workspace_request_v3() -> VideoRequestV3:
    capability = next(item for item in wan_shot_capabilities() if "t2v" in item.shot_modes)
    return VideoRequestV3(
        provider="wan",
        model=capability.model,
        capabilitiesVersion=capability.capabilities_version,
        prompt=PROMPT,
        spec=ShotSpecV3.model_validate({"mode": "t2v", "durationSeconds": 5, "aspectRatio": "16:9"}),
        workspaceId="test-workspace",
        providerPrompt=WAN_PROMPT_PREFIX + PROMPT,
        parameters=WanParametersV3(duration=5, ratio="16:9"),
        media=[],
    )


def shot_context(run_id="shot-run", call_id="batch"):
    return ToolExecutionContext(
        project_id="coffee", session_id="coffee", run_id=run_id, model_step=1, tool_call_id=call_id
    )


class SharedUpstream:
    """A controlled Wan upstream recording every request host; one task per submit."""

    def __init__(self):
        self.calls = []
        self.submits = 0

    def handler(self, request):
        self.calls.append(request)
        if request.method == "POST":
            self.submits += 1
            return httpx.Response(200, json=provider_response("PENDING", task_id=f"shared-{self.submits}"))
        task_id = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(
            200,
            json={
                **provider_response("SUCCEEDED", task_id=task_id, video_url=VIDEO_URL),
                "usage": {"duration": 5},
            },
        )

    @property
    def submitted(self):
        return [call for call in self.calls if call.method == "POST"]

    def hosts(self):
        return {call.url.host for call in self.calls}


def test_workspace_v2_v3_requests_stay_byte_identical():
    v2 = frozen_request()
    assert v2.fingerprint() == PRE_K_PINS["v2RequestFingerprint"]
    assert v2.intent_fingerprint() == PRE_K_PINS["v2IntentFingerprint"]
    assert serialized_sha(v2.model_dump(mode="json", by_alias=True)) == PRE_K_PINS["v2SerializedSha256"]
    v3 = workspace_request_v3()
    assert v3.fingerprint() == PRE_K_PINS["v3RequestFingerprint"]
    assert v3.intent_fingerprint() == PRE_K_PINS["v3IntentFingerprint"]
    assert serialized_sha(v3.model_dump(mode="json", by_alias=True)) == PRE_K_PINS["v3SerializedSha256"]


async def test_keys_only_adapter_needs_just_the_video_key():
    async with WanAdapter(
        api_key="fixture-key", workspace_id=None, transport=httpx.MockTransport(lambda _: httpx.Response(500))
    ) as adapter:
        adapter.check_configuration(shared_request())  # No workspace is configured at all.
        assert adapter._host("beijing-shared-v1") == SHARED_HOST
        with pytest.raises(AppError) as caught:
            adapter.check_configuration(frozen_request())
        assert caught.value.code == "VIDEO_WORKSPACE_REQUIRED"
    async with WanAdapter(api_key=None, workspace_id=None) as adapter:
        with pytest.raises(AppError) as caught:
            adapter.check_configuration(shared_request())
        assert caught.value.code == "VIDEO_KEY_MISSING"


async def test_keys_only_single_shot_delivers_from_the_shared_host(store, video_clock):
    upstream = SharedUpstream()
    async with live_service(store, video_clock, upstream.handler, video_workspace_id=None) as (service, _):
        assert service.check_live_configuration() is None
        assert service.configuration_status()["configured"] is True
        registered = service.generate(live_arguments(), context=live_run(store, "keys-only"))
        assert registered["ok"], registered
        job = service.get(registered["data"]["jobId"])
        assert isinstance(job, JobV4) and job.contract_version == 4
        assert job.request.endpoint_profile == "beijing-shared-v1"
        raw = store.snapshot()["jobs"][job.id]
        assert "workspaceId" not in json.dumps(raw, ensure_ascii=False)
        assert raw["request"]["endpointProfile"] == "beijing-shared-v1"
        worker = JobWorker(service)
        queued = await worker.run_once()
        assert queued.provider_task_id == "shared-1" and queued.status == "queued"
        submitted = upstream.submitted[0]
        assert submitted.url.host == SHARED_HOST
        assert submitted.url.path == "/api/v1/services/aigc/video-generation/video-synthesis"
        assert json.loads(submitted.content) == {
            "model": "wan2.7-t2v-2026-06-12",
            "input": {"prompt": "生成单镜头视频。\n" + PROMPT},
            "parameters": {
                "resolution": "720P",
                "ratio": "16:9",
                "duration": 5,
                "prompt_extend": False,
                "watermark": True,
                "seed": 0,
            },
        }
        video_clock.due(queued)
        downloading = await worker.run_once()
        assert downloading.status == "downloading"
        assert upstream.calls[-1].method == "GET" and upstream.calls[-1].url.host == SHARED_HOST
        assert upstream.hosts() == {SHARED_HOST}
        data = SAMPLE.read_bytes()

        def download(_request):
            return httpx.Response(200, content=data, headers={"Content-Type": "video/mp4"})

        async with MediaHttpClient(transport=httpx.MockTransport(download)) as client:
            completed = await MediaWorker(service.media, client).run_once()
        assert completed.status == "succeeded" and completed.media_availability.status == "available"
        assert completed.download.phase == "committed"
        assert "workspaceId" not in json.dumps(completed.model_dump(mode="json", by_alias=True))
    home = store.home
    store.close()
    with FileStore.open(home) as reopened:
        restored = parse_job(reopened.snapshot()["jobs"][job.id])
        assert isinstance(restored, JobV4) and restored.status == "succeeded"
        assert len(reopened.snapshot()["media"]) == 1


async def test_keys_only_shot_batch_and_redo_stay_on_the_shared_host(store, video_clock):
    upstream = SharedUpstream()
    registry, _, _, _ = mock_tools(store, clock=video_clock)
    asset_id = insert_image(store)
    saved = save_storyboard(
        registry,
        store,
        [
            shot("镜头一：雨夜咖啡店窗外"),
            {**shot("镜头二：推近窗边的咖啡杯"), "mode": "i2v", "referenceAssetIds": [asset_id]},
        ],
    )
    m2_run(store, "shot-run", mode="live")
    async with live_service(store, video_clock, upstream.handler, video_workspace_id=None) as (service, _):
        registered = service.shot_generate(generation_args(saved), context=shot_context())
        assert registered["ok"], registered
        jobs = [parse_job(raw) for raw in jobs_of(store, "shot-run")]
        assert len(jobs) == 2 and all(isinstance(job, JobV5) and job.contract_version == 5 for job in jobs)
        assert all(job.request.endpoint_profile == "beijing-shared-v1" for job in jobs)
        assert all(
            "workspaceId" not in json.dumps(job.model_dump(mode="json", by_alias=True)) for job in jobs
        )
        worker = JobWorker(service)
        for _ in range(14):
            video_clock.advance(5)
            if await worker.run_once() is None:
                break
        assert upstream.submits == 2
        assert {call.url.host for call in upstream.submitted} == {SHARED_HOST}
        bodies = [json.loads(call.content) for call in upstream.submitted]
        image_body = next(body for body in bodies if body["model"] == "wan2.7-i2v-2026-04-25")
        assert image_body["input"]["media"][0]["type"] == "first_frame"
        assert image_body["input"]["media"][0]["url"].startswith("data:image/png;base64,")
        assert "ratio" not in image_body["parameters"]
        # Explicit user redo: one new ShotVersion plus one new V5 Job for the target only.
        before = storyboard_record(store, saved["storyboardId"])
        target = saved["shotIds"][0]
        other = saved["shotIds"][1]
        m2_run(store, "redo-run", mode="live")
        redone = service.redo_shot(
            redo_args(saved, target, revision=before["revision"], client_request_id="redo-1"),
            context=shot_context("redo-run", "redo"),
        )
        assert redone["ok"], redone
        redo_jobs = jobs_of(store, "redo-run")
        assert len(redo_jobs) == 1
        redo_job = parse_job(redo_jobs[0])
        assert isinstance(redo_job, JobV5) and redo_job.shot_ref.shot_id == target
        assert len(shot_versions(store, saved["storyboardId"], target)) == 2
        assert len(shot_versions(store, saved["storyboardId"], other)) == 1
        assert upstream.submits == 2  # Redo registers only; nothing is submitted by it.


async def test_mixed_directory_restart_keeps_both_profiles_working(store, video_clock):
    upstream = SharedUpstream()
    # 1) A dedicated-era Job registered while a workspace was configured.
    async with live_service(store, video_clock, upstream.handler) as (service, _):
        registered = service.generate(live_arguments(), context=live_run(store, "dedicated-run"))
        assert registered["ok"], registered
        dedicated_id = registered["data"]["jobId"]
        assert "workspaceId" in json.dumps(store.snapshot()["jobs"][dedicated_id])
    home = store.home
    store.close()
    # 2) Restart keys-only: the shared Job works; the dedicated Job blocks, it does
    #    not silently move to another host.
    with FileStore.open(home) as reopened:
        async with live_service(reopened, video_clock, upstream.handler, video_workspace_id=None) as (
            service,
            _,
        ):
            registered = service.generate(live_arguments(), context=live_run(reopened, "shared-run"))
            assert registered["ok"], registered
            shared_id = registered["data"]["jobId"]
            worker = JobWorker(service)
            for _ in range(10):
                video_clock.advance(20)  # Step past the 15 s live poll interval.
                if await worker.run_once() is None:
                    break
            blocked = service.get(dedicated_id)
            assert blocked.status == "pending_submit" and blocked.runtime_block is not None
            assert blocked.runtime_block.code == "VIDEO_PROVIDER_UNAVAILABLE"
            assert blocked.runtime_block.stage == "submit"
            assert service.get(shared_id).provider_task_id == "shared-1"
            assert upstream.hosts() == {SHARED_HOST}
    # 3) Restart with the workspace restored: the dedicated Job recovers on its own
    #    host while the delivered shared Job is untouched.
    with FileStore.open(home) as reopened:
        async with live_service(reopened, video_clock, upstream.handler) as (service, _):
            mark = len(upstream.calls)
            worker = JobWorker(service)
            for _ in range(10):
                video_clock.advance(20)  # Step past the 15 s live poll interval.
                if await worker.run_once() is None:
                    break
            recovered = service.get(dedicated_id)
            assert recovered.provider_task_id == "shared-2" and recovered.runtime_block is None
            new_hosts = {call.url.host for call in upstream.calls[mark:]}
            assert new_hosts == {DEDICATED_HOST}
            assert service.get(shared_id).status == "downloading"
