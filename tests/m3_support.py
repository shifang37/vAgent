"""Shared M3 E2 fixtures: a controlled Kling upstream and Storyboard helpers."""

import json
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlsplit

import httpx
from conftest import VideoClock
from shot_support import generation_args, save_storyboard, shot  # noqa: F401

from vagent.config import Config
from vagent.video.jobs import JobService
from vagent.video.providers.kling import KlingAdapter
from vagent.video.providers.mock import MockVideoAdapter
from vagent.waiting import ToolExecutionContext

KLING_HOST = "api-singapore.klingai.com"


def kling_config(home, **changes):
    return Config(
        **{
            "home": home,
            "api_key": None,
            "video_mode": "live",
            "video_provider": "kling",
            "kling_api_key": "kling-test-secret",
            **changes,
        }
    )


@asynccontextmanager
async def kling_service(store, clock=None, handler=None, **config_changes):
    clock = clock or VideoClock()
    config = kling_config(store.home, **config_changes)
    async with KlingAdapter(
        api_key=config.kling_api_key, transport=httpx.MockTransport(handler), clock=clock
    ) as provider:
        service = JobService(
            store, [MockVideoAdapter(store, clock=clock), provider], clock=clock, config=config
        )
        yield service, provider


@asynccontextmanager
async def providers_service(store, clock=None, *, kling_handler=None, fal_handler=None, **config_changes):
    """One service publishing Wan/mock plus both E2 providers (no Wan adapter)."""
    from vagent.video.providers.fal import FalAdapter

    clock = clock or VideoClock()
    kwargs = {
        "home": store.home,
        "api_key": None,
        "video_mode": "live",
        "video_provider": "kling",
        "kling_api_key": "kling-test-secret",
        "fal_api_key": "fal-test-secret",
        **config_changes,
    }
    config = Config(**kwargs)
    async with (
        KlingAdapter(
            api_key=config.kling_api_key,
            transport=httpx.MockTransport(kling_handler),
            clock=clock,
        ) as kling,
        FalAdapter(
            api_key=config.fal_api_key,
            transport=httpx.MockTransport(fal_handler),
            clock=clock,
        ) as fal,
    ):
        service = JobService(
            store,
            [MockVideoAdapter(store, clock=clock), kling, fal],
            clock=clock,
            config=config,
        )
        yield service, kling, fal


class KlingUpstream:
    """A stateful controlled Kling API: each task answers from its own record."""

    def __init__(
        self,
        *,
        url_template="https://cdn.example-provider.test/{task_id}.mp4?sig=private",
        status="succeeded",
    ):
        self.requests = []
        self.tasks = {}
        self.url_template = url_template
        self.status = status

    def handler(self, request):
        self.requests.append(request)
        if request.method == "POST":
            body = json.loads(request.content)
            task_id = f"kling-task-{len(self.tasks) + 1}"
            self.tasks[task_id] = body["prompt"]
            return httpx.Response(200, json={"code": 0, "data": {"id": task_id}})
        task_id = parse_qs(urlsplit(str(request.url)).query)["task_ids"][0]
        if self.status == "succeeded":
            entry = {
                "id": task_id,
                "status": "succeeded",
                "outputs": [{"type": "video", "url": self.url_template.format(task_id=task_id)}],
            }
        else:
            entry = {"id": task_id, "status": self.status}
        return httpx.Response(200, json={"code": 0, "data": [entry]})

    @property
    def submitted(self):
        return [call for call in self.requests if call.method == "POST"]

    @property
    def hosts(self):
        return {call.url.host for call in self.requests}


class FalUpstream:
    """A stateful controlled fal queue: COMPLETED on the first status read."""

    def __init__(
        self,
        *,
        url_template="https://v3.fal.media/files/{task_id}.mp4?token=private",
        status="COMPLETED",
    ):
        self.requests = []
        self.tasks = {}
        self.url_template = url_template
        self.status = status

    def handler(self, request):
        self.requests.append(request)
        path = request.url.path
        if request.method == "POST":
            request_id = f"fal-request-{len(self.tasks) + 1}"
            self.tasks[request_id] = json.loads(request.content)
            return httpx.Response(200, json={"request_id": request_id, "status": "IN_QUEUE"})
        if path.endswith("/status"):
            request_id = path.split("/")[-2]
            return httpx.Response(200, json={"request_id": request_id, "status": self.status})
        request_id = path.rsplit("/", 1)[-1]
        return httpx.Response(200, json={"video": {"url": self.url_template.format(task_id=request_id)}})

    @property
    def submitted(self):
        return [call for call in self.requests if call.method == "POST"]

    @property
    def hosts(self):
        return {call.url.host for call in self.requests}


def shot_context(run_id="shot-run", call_id="batch", project_id="coffee"):
    return ToolExecutionContext(
        project_id=project_id,
        session_id=project_id,
        run_id=run_id,
        model_step=1,
        tool_call_id=call_id,
    )
