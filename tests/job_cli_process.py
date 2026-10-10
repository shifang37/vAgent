"""Real CLI/asyncio SIGINT cleanup fixture; no model or provider network access."""

import asyncio
import signal
import sys
from contextlib import asynccontextmanager, suppress

from conftest import tool_call

import vagent.application as application
from vagent import cli
from vagent.video.providers.mock import MockScenario, MockVideoAdapter

mode, job_id = sys.argv[1:3]
queries_before_run = int(sys.argv[3]) if len(sys.argv) > 3 else 0
original_open = application.ApplicationService.open


class Model:
    name = "cli-process-fixture"

    async def generate(self, messages, tools):
        return tool_call("await_job", {"jobId": job_id}, call_id="wait")


@asynccontextmanager
async def opened(*args, **kwargs):
    async with original_open(*args, **kwargs) as service:
        if queries_before_run:
            async with asyncio.timeout(8):
                while True:
                    job = service.video_jobs.get(job_id)
                    if job.query_attempts >= queries_before_run and job.query_started_at is None:
                        break
                    await asyncio.sleep(0.01)

        async def interrupt():
            async with asyncio.timeout(8):
                while True:
                    state = service.store.snapshot()
                    ready = (
                        any(run["status"] == "waiting_external" for run in state["runs"].values())
                        if mode == "run"
                        else state["jobs"][job_id]["status"] == "queued"
                    )
                    if ready:
                        # Exercise asyncio.Runner's actual SIGINT handler without
                        # depending on a visible console on either platform.
                        signal.raise_signal(signal.SIGINT)
                        return
                    await asyncio.sleep(0.01)

        interrupter = asyncio.create_task(interrupt())
        try:
            yield service
        finally:
            interrupter.cancel()
            with suppress(asyncio.CancelledError):
                await interrupter


application.ApplicationService.open = staticmethod(opened)
# Keep this cancellation fixture pending even if the Worker polls before the
# Agent reaches await_job; the default scenario completes after two queries.
application.MockVideoAdapter = lambda store: MockVideoAdapter(store, scenario=MockScenario(states=["queued"]))
if mode == "run":
    application.DeepSeekModel = lambda *_args, **_kwargs: Model()
if mode == "chat":
    if sys.platform != "win32":
        raise SystemExit("This empty-keyboard fixture is Windows-only")
    import msvcrt

    sys.stdin.isatty = lambda: True
    msvcrt.kbhit = lambda: False
    sys.argv = ["vagent", "chat", "--session", "coffee"]
elif mode == "run":
    sys.argv = ["vagent", "run", "等待已有任务", "--session", "coffee"]
else:
    sys.argv = ["vagent", "jobs", "work"]
cli.main()
