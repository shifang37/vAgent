import json

import httpx
from media_support import SAMPLE, download_service
from test_live_cli import command

from vagent.storage import FileStore
from vagent.video.jobs import JobService
from vagent.video.media_http import MediaHttpClient
from vagent.video.media_worker import MediaWorker


async def test_cli_retry_records_intent_without_generation_and_reports_local_media(tmp_path, video_clock):
    home = tmp_path / "state"
    with FileStore.open(home) as store:
        async with download_service(store, video_clock) as (jobs, job, _):
            async with MediaHttpClient(
                transport=httpx.MockTransport(lambda _: httpx.Response(404))
            ) as client:
                failed = await MediaWorker(jobs.media, client).run_once()
                assert failed.status == "download_failed"
    first = command(tmp_path, home, "retry-download", job.id)
    assert first.returncode == 0, first.stderr
    accepted = json.loads(first.stdout)
    assert accepted["status"] == "downloading" and accepted["download"]["generation"] == 2
    assert accepted["download"]["attempts"] == 1 and accepted["submitAttempts"] == 1
    assert accepted["localMediaPath"] == job.download.relative_path
    again = command(tmp_path, home, "retry-download", job.id)
    assert again.returncode == 0 and json.loads(again.stdout)["download"] == accepted["download"]
    with FileStore.open(home) as store:
        jobs = JobService(store, [], clock=video_clock)
        async with MediaHttpClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=SAMPLE.read_bytes()))
        ) as client:
            complete = await MediaWorker(jobs.media, client).run_once()
            assert complete.status == "succeeded" and complete.download.attempts == 2
    output = command(tmp_path, home, "get", job.id)
    assert output.returncode == 0, output.stderr
    current = json.loads(output.stdout)
    assert current["mediaAvailable"] and current["mediaRefs"][0]["mediaId"] == job.download.media_id
    assert "Signature" not in output.stdout and str(home) not in output.stdout
    (home / job.download.relative_path).unlink()
    missing = command(tmp_path, home, "get", job.id)
    assert not json.loads(missing.stdout)["mediaAvailable"]
    saved = json.loads((home / "state.json").read_bytes())["jobs"][job.id]
    assert saved["submitAttempts"] == 1 and saved["providerTaskId"] == "wan-original"
    assert saved["download"]["attempts"] == 2
