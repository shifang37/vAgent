"""Production second-interrupt crash fixture; all resources and models are offline."""

import asyncio
import os
import sys

from conftest import VideoClock
from test_wait_runtime import Harness

from vagent.storage import FileStore


async def main():
    phase = sys.argv[2]
    with FileStore.open(sys.argv[1]) as store:
        clock = VideoClock()
        harness = Harness(store, clock)
        first_job = harness.job("owner-0")
        clock.advance(0.1)
        second_job = harness.job("owner-1")

        def event(event):
            if event["type"] != phase:
                return
            raw = store.snapshot()["waits"].get(event.get("waitId"))
            if raw and raw["context"]["toolCallId"] == "await-1":
                os._exit(73)

        harness.on_event = event
        harness.coordinator.waits.on_event = event
        await harness.begin([first_job, second_job])
        assert (await harness.worker.run_once()).id == first_job
        await harness.coordinator.run_once()
        assert (await harness.worker.run_once()).id == second_job
        await harness.coordinator.run_once()
        raise AssertionError(f"Crash point not reached: {phase}")


if __name__ == "__main__":
    asyncio.run(main())
