import asyncio
import json

from conftest import tool_call, video_request, video_run
from langchain_core.messages import AIMessage


async def eventually(read, *, timeout=5):
    async with asyncio.timeout(timeout):
        while True:
            result = read()
            if result:
                return result
            await asyncio.sleep(0.01)


def register_job(service, *, run_id="owner", session_id="coffee"):
    provider = service.video_jobs.capabilities()[0]
    request = {
        "provider": provider.provider,
        "model": provider.model,
        "capabilitiesVersion": provider.capabilities_version,
        "prompt": "雨夜咖啡店",
        "spec": provider.specs[0].model_dump(mode="json", by_alias=True),
    }
    context = video_run(service.store, run_id, project_id=session_id)
    return service.video_jobs.generate(request, context=context)["data"]["jobId"]


async def finish_job(service, clock, job_id):
    async with asyncio.timeout(5):
        while True:
            job = service.video_jobs.get(job_id)
            if job.status in {"succeeded", "failed", "unknown"} or job.query_state == "paused":
                return job
            if job.next_poll_at and not job.query_started_at:
                clock.due(job)
            await asyncio.sleep(0.01)


class JobModel:
    name = "job-entry-fixture"

    def __init__(self, *, wait=True):
        self.calls = 0
        self.wait = wait
        self.job_id = None
        self.request = None

    async def generate(self, messages, tools):
        self.calls += 1
        if self.calls == 1:
            assert self.request
            return tool_call("video_generate", self.request, call_id="generate")
        result = json.loads(messages[-1].content)
        assert result["ok"], result
        self.job_id = result["data"]["jobId"]
        if self.calls == 2 and self.wait:
            return tool_call("await_job", {"jobId": self.job_id}, call_id="original-wait")
        if self.wait:
            assert self.calls == 3 and result["data"]["status"] == "succeeded"
        return AIMessage(content=f"模拟任务 {self.job_id}，无真实媒体。")

    def configure(self, runtime):
        self.request = video_request(runtime.adapters[-1])
        return self
