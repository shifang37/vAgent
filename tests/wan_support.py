"""Synthetic Wan inputs; no credentials or public HTTP are used by tests."""

from contextlib import asynccontextmanager

import httpx
from conftest import video_run

from vagent.config import Config
from vagent.video.contracts import (
    WAN_ADAPTER_VERSION,
    WAN_ENDPOINT_PROFILE,
    WAN_PROMPT_PREFIX,
    WAN_REGION,
    VideoIntentV2,
    VideoRequestV2,
    WanParameters,
    wan_capabilities,
)
from vagent.video.jobs import JobService
from vagent.video.providers.mock import MockVideoAdapter
from vagent.video.providers.wan import WanAdapter


def live_arguments(**changes):
    capability = wan_capabilities()
    return {
        "provider": capability.provider,
        "model": capability.model,
        "capabilitiesVersion": capability.capabilities_version,
        "prompt": "雨夜咖啡店，镜头缓慢推近。",
        "spec": {"durationSeconds": 5, "resolution": "720p", "aspectRatio": "16:9"},
        **changes,
    }


def frozen_request(*, workspace_id="test-workspace", **changes):
    intent = VideoIntentV2.model_validate(live_arguments(**changes))
    return VideoRequestV2(
        **intent.model_dump(mode="json", by_alias=True),
        workspace_id=workspace_id,
        region=WAN_REGION,
        adapter_version=WAN_ADAPTER_VERSION,
        endpoint_profile=WAN_ENDPOINT_PROFILE,
        provider_prompt=WAN_PROMPT_PREFIX + intent.prompt,
        parameters=WanParameters(),
    )


def provider_response(status="PENDING", *, task_id="wan-original", request_id="trace-original", **output):
    return {"request_id": request_id, "output": {"task_id": task_id, "task_status": status, **output}}


VIDEO_URL = "https://dashscope-result-sh.oss-accelerate.aliyuncs.com/test.mp4?Signature=private-signature"


def live_run(store, run_id="live-run", *, project_id="coffee", read_only=False):
    context = video_run(store, run_id, project_id=project_id, read_only=read_only)
    store.transaction(lambda draft: draft["runs"][run_id].update(videoMode="live", executionVersion=2))
    return context


def live_config(home, **changes):
    return Config(
        **{
            "home": home,
            "api_key": None,
            "video_mode": "live",
            "video_api_key": "video-test-secret",
            "video_workspace_id": "test-workspace",
            **changes,
        }
    )


@asynccontextmanager
async def live_service(store, clock, handler, **config_changes):
    config = live_config(store.home, **config_changes)
    async with WanAdapter(
        api_key=config.video_api_key,
        workspace_id=config.video_workspace_id,
        transport=httpx.MockTransport(handler),
        clock=clock,
    ) as provider:
        service = JobService(
            store, [MockVideoAdapter(store, clock=clock), provider], clock=clock, config=config
        )
        yield service, provider
