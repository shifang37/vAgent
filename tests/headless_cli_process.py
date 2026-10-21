"""Child process for headless CLI tests: the real CLI with controlled adapters.

The plan phase uses a scripted model; the authorized phase talks to a controlled
Kling transport and downloads the local fixture. Task status is read from a JSON
control file so two invocations can observe a queued task and then finish it.
"""

import hashlib
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
from langchain_core.messages import AIMessage
from media_support import SAMPLE

import vagent.application as application
from vagent import cli, headless
from vagent.video.jobs import JobService
from vagent.video.media_http import MediaHttpClient
from vagent.video.media_worker import MediaWorker
from vagent.video.providers.fal import FalAdapter
from vagent.video.providers.kling import KlingAdapter
from vagent.video.worker import JobWorker

CONTROL = Path(os.environ["HEADLESS_CONTROL"])
SUBMISSIONS = CONTROL.with_name("submissions.jsonl")
headless.WAIT_TIMEOUT_SECONDS = float(os.environ.get("HEADLESS_WAIT_TIMEOUT", "600"))


class FastClock:
    """Every timestamp call advances 20 virtual seconds past provider poll delays."""

    def __init__(self):
        self.value = datetime(2030, 1, 1, tzinfo=UTC)

    def __call__(self):
        self.value += timedelta(seconds=20)
        return self.value


clock = FastClock()


def control():
    return json.loads(CONTROL.read_text(encoding="utf-8"))


def task_id(prompt: str) -> str:
    return "kling-" + hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]


def kling_handler(request):
    if request.method == "POST":
        body = json.loads(request.content)
        with SUBMISSIONS.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"prompt": body["prompt"]}) + "\n")
        return httpx.Response(200, json={"code": 0, "data": {"id": task_id(body["prompt"])}})
    ids = parse_qs(urlsplit(str(request.url)).query)["task_ids"]
    status = control().get("status", "succeeded")
    entries = []
    for item in ids:
        if status == "succeeded":
            entries.append(
                {
                    "id": item,
                    "status": "succeeded",
                    "outputs": [
                        {
                            "type": "video",
                            "url": f"https://cdn.example-provider.test/{item}.mp4?sig=private",
                        }
                    ],
                }
            )
        else:
            entries.append({"id": item, "status": "processing"})
    return httpx.Response(200, json={"code": 0, "data": entries})


def download(_request):
    data = SAMPLE.read_bytes()
    return httpx.Response(
        200, content=data, headers={"Content-Type": "video/mp4", "Content-Length": str(len(data))}
    )


class StoryboardModel:
    """One storyboard_save turn, then a final answer; no other model step."""

    name = "headless-cli-fixture"

    def __init__(self):
        self.calls = 0

    async def generate(self, messages, tools):
        self.calls += 1
        if self.calls == 1:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "save-1",
                        "name": "storyboard_save",
                        "args": {
                            "title": "headless CLI 分镜",
                            "shots": [
                                {
                                    "description": f"镜头{index}：雨夜咖啡店",
                                    "framing": "wide",
                                    "durationSeconds": 5,
                                    "mode": "t2v",
                                }
                                for index in (1, 2)
                            ],
                        },
                    }
                ],
            )
        return AIMessage(content="已保存两个镜头的分镜草案，尚未登记任何视频任务。")


application.DeepSeekModel = lambda *_args, **_kwargs: StoryboardModel()
application.KlingAdapter = lambda api_key: KlingAdapter(
    api_key=api_key, transport=httpx.MockTransport(kling_handler), clock=clock
)
application.FalAdapter = lambda api_key: FalAdapter(
    api_key=api_key, transport=httpx.MockTransport(lambda _: httpx.Response(500, json={})), clock=clock
)
application.MediaHttpClient = lambda: MediaHttpClient(transport=httpx.MockTransport(download))
application.JobService = lambda store, adapters, **kwargs: JobService(store, adapters, clock=clock, **kwargs)
application.JobWorker = lambda service: JobWorker(service, idle_interval_seconds=0.005)
application.MediaWorker = lambda media, client: MediaWorker(media, client, idle_interval_seconds=0.005)

sys.argv = ["vagent", "headless", *json.loads(os.environ["HEADLESS_ARGS"])]
cli.main()
