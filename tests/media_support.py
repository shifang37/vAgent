"""Offline media fixtures: a locally generated, decodable 5-second AVC/AAC MP4."""

from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

from wan_support import VIDEO_URL, live_arguments, live_run, live_service, provider_response

from vagent.video.contracts import DownloadPolicy
from vagent.video.worker import JobWorker

SAMPLE = Path(__file__).parent / "fixtures" / "m1c" / "sample-720p.mp4"


@asynccontextmanager
async def download_service(store, clock, *, policy=None, url=VIDEO_URL):
    import httpx

    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=provider_response("SUCCEEDED", video_url=url))

    context = live_run(store)
    async with live_service(store, clock, handler) as (service, _):
        service.download_policy = policy or DownloadPolicy()
        registered = service.generate(live_arguments(), context=context)
        job_id = registered["data"]["jobId"]
        await JobWorker(service).run_once()
        yield service, service.get(job_id), calls


def install_media_runtime(monkeypatch, clock):
    import httpx

    import vagent.application as application
    from vagent.video.jobs import JobService
    from vagent.video.media_http import MediaHttpClient
    from vagent.video.media_worker import MediaWorker
    from vagent.video.providers.wan import WanAdapter

    runtime = SimpleNamespace(
        submissions=[], downloads=[], gate=None, data=SAMPLE.read_bytes(), status=200, headers={}, clients=[]
    )

    def upstream(request):
        runtime.submissions.append(request)
        return httpx.Response(200, json=provider_response("SUCCEEDED", video_url=VIDEO_URL))

    async def download(request):
        runtime.downloads.append(request)
        if runtime.gate is not None:
            await runtime.gate.wait()
        return httpx.Response(runtime.status, content=runtime.data, headers=runtime.headers)

    def media_client():
        client = MediaHttpClient(transport=httpx.MockTransport(download))
        runtime.clients.append(client)
        return client

    monkeypatch.setattr(
        application,
        "WanAdapter",
        lambda **kwargs: WanAdapter(
            **kwargs,
            transport=httpx.MockTransport(upstream),
            clock=clock,
        ),
    )
    monkeypatch.setattr(application, "MediaHttpClient", media_client)
    monkeypatch.setattr(
        application, "JobService", lambda *args, **kwargs: JobService(*args, **kwargs, clock=clock)
    )
    # Stay above Windows' 15.6 ms monotonic clock resolution; shorter timers can
    # spin and starve the asynchronous file threads under a busy test event loop.
    monkeypatch.setattr(application, "JobWorker", lambda jobs: JobWorker(jobs, idle_interval_seconds=0.05))
    monkeypatch.setattr(
        application,
        "MediaWorker",
        lambda media, client: MediaWorker(media, client, idle_interval_seconds=0.05),
    )
    return runtime
