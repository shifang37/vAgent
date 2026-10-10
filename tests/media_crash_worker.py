"""Process-kill checkpoints for the file/JSON commit protocol; test harness only."""

import asyncio
import os
import sys
from pathlib import Path

import httpx
from conftest import VideoClock
from media_support import SAMPLE

from vagent.storage import FileStore
from vagent.video.jobs import JobService
from vagent.video.media_http import MediaHttpClient
from vagent.video.media_worker import MediaWorker


async def main():
    home, stage = Path(sys.argv[1]), sys.argv[2]
    with FileStore.open(home) as store:
        print(os.getpid(), flush=True)
        jobs = JobService(store, [], clock=VideoClock())
        if stage == "intent":
            os._exit(73)

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                data = SAMPLE.read_bytes()
                yield data[:65536]
                if stage == "writing":
                    os._exit(73)
                yield data[65536:]

        def handler(_):
            with (home / "test-downloads.txt").open("a", encoding="ascii") as log:
                log.write("download\n")
            return httpx.Response(200, stream=Stream())

        def stop_after(function):
            def wrapper(*args, **kwargs):
                value = function(*args, **kwargs)
                os._exit(73)
                return value

            return wrapper

        if stage == "validated":
            jobs.media.files.prepare = stop_after(jobs.media.files.prepare)
        if stage == "prepared":
            original = jobs.save

            def save(job, **kwargs):
                value = original(job, **kwargs)
                if value.download.phase == "prepared":
                    os._exit(73)
                return value

            jobs.save = save
        if stage == "renamed":
            jobs.media.files.promote = stop_after(jobs.media.files.promote)
        if stage == "committed":
            jobs.media.commit = stop_after(jobs.media.commit)
        async with MediaHttpClient(transport=httpx.MockTransport(handler)) as client:
            await MediaWorker(jobs.media, client).run_once()
        raise AssertionError("Crash checkpoint was not reached")


if __name__ == "__main__":
    asyncio.run(main())
