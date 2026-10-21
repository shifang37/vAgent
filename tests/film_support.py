"""E1 fixtures: two really delivered local clips and a storyboard citing them.

The clips come from the frozen M1-C live pipeline with a controlled transport, so the
Job, media index and file all satisfy the real validators. Their ShotVersions are
linked explicitly, because M3 film assembly only needs the delivered reference and
must also work for pre-M2 single-shot registrations.
"""

import hashlib
from uuid import uuid4

import httpx
from conftest import VideoClock
from media_support import SAMPLE
from shot_support import mock_tools, save_storyboard, shot
from wan_support import VIDEO_URL, live_arguments, live_run, live_service, provider_response

from vagent.video.media_http import MediaHttpClient
from vagent.video.media_worker import MediaWorker
from vagent.video.storyboards import ShotVersion, Storyboard
from vagent.video.worker import JobWorker


def provider_handler(request):
    return httpx.Response(200, json=provider_response("SUCCEEDED", video_url=VIDEO_URL))


def clip_response(request):
    return httpx.Response(200, content=SAMPLE.read_bytes(), headers={"Content-Type": "video/mp4"})


async def delivered_clip(store, clock, *, run_id: str, prompt: str):
    """Submit, query and download one clip through the frozen live pipeline."""
    # Live submissions are spaced by one second on the frozen clock.
    clock.advance(5)
    context = live_run(store, run_id)
    async with live_service(store, clock, provider_handler) as (service, _provider):
        service.generate(live_arguments(prompt=prompt), context=context)
        worker = JobWorker(service)
        for _ in range(8):
            job = await worker.run_once()
            if job is not None and job.status in {"downloading", "succeeded"}:
                break
        async with MediaHttpClient(transport=httpx.MockTransport(clip_response)) as client:
            delivered = await MediaWorker(service.media, client).run_once()
    if delivered is None or delivered.status != "succeeded":
        raise AssertionError("fixture clip was not delivered")
    return delivered


def link_shot_versions(store, *, storyboard_id: str, storyboard_version: int, entries):
    """Append delivered references for one saved storyboard version, as M2 would."""
    timestamp = "2030-01-01T00:00:00+00:00"

    def link(draft):
        record = Storyboard.model_validate(draft["storyboards"][storyboard_id])
        created = [
            ShotVersion(
                id=str(uuid4()),
                shot_id=shot_id,
                storyboard_version=storyboard_version,
                job_id=job_id,
                request=request,
                media_id=media_id,
                created_at=timestamp,
            )
            for shot_id, job_id, media_id, request in entries
        ]
        updated = record.model_copy(
            update={"shot_versions": (*record.shot_versions, *created), "revision": record.revision + 1}
        )
        draft["storyboards"][storyboard_id] = updated.model_dump(mode="json", by_alias=True)

    store.transaction(link)
    return store.snapshot()["storyboards"][storyboard_id]["revision"]


async def delivered_pair(store, clock=None):
    """A two-shot storyboard whose shots point at two distinct delivered clips."""
    clock = clock or VideoClock()
    first = await delivered_clip(store, clock, run_id="film-run-1", prompt="雨夜街道")
    second = await delivered_clip(store, clock, run_id="film-run-2", prompt="推门入店")
    registry, _service, _adapter, _context = mock_tools(store, clock=clock)
    saved = save_storyboard(registry, store, [shot("雨夜街道"), shot("推门入店")], key="film-storyboard")
    link_shot_versions(
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
    state = store.snapshot()
    board = state["storyboards"][saved["storyboardId"]]
    return {
        "storyboardId": saved["storyboardId"],
        "revision": board["revision"],
        "shotIds": saved["shotIds"],
        "jobs": [first, second],
        "mediaIds": [job.result.media_refs[0].media_id for job in (first, second)],
        "sha256": hashlib.sha256(SAMPLE.read_bytes()).hexdigest(),
        "files": [
            store.home / state["media"][job.result.media_refs[0].media_id]["relativePath"]
            for job in (first, second)
        ],
    }


def assembly_args(fixture, *, revision=None, storyboard_id=None):
    return {
        "storyboardId": fixture["storyboardId"] if storyboard_id is None else storyboard_id,
        "expectedRevision": fixture["revision"] if revision is None else revision,
    }
