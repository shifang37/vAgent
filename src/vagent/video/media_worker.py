"""One independent download loop with durable attempts and recoverable file publication."""

import asyncio
import math
from contextlib import suppress
from datetime import datetime
from weakref import WeakKeyDictionary

from vagent.errors import AppError
from vagent.video.contracts import JobV2, parse_job
from vagent.video.jobs import after_seconds
from vagent.video.media import MediaError, changed_download, disk_error, failed_download, file_io


async def disk_io(function, *args):
    try:
        return await file_io(function, *args)
    except OSError as error:
        raise disk_error(error) from None


class MediaWorker:
    _store_locks: WeakKeyDictionary = WeakKeyDictionary()

    def __init__(self, media, client, *, idle_interval_seconds=0.25):
        if not math.isfinite(idle_interval_seconds) or idle_interval_seconds <= 0:
            raise ValueError("Worker idle interval must be positive and finite")
        self.media, self.client, self.jobs = media, client, media.jobs
        self.files = media.files
        self.idle_interval_seconds = idle_interval_seconds
        self._turn_lock = self._store_locks.setdefault(media.store, asyncio.Lock())
        self._stopping = asyncio.Event()
        self._task = None

    def start(self):
        if self._task is None or self._task.done():
            self._stopping.clear()
            self._task = asyncio.create_task(self.run(), name="video-media-worker")
        return self._task

    async def stop(self):
        self._stopping.set()
        if self._task:
            if not self._task.done():
                self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task

    async def run(self):
        while not self._stopping.is_set():
            if await self.run_once() is not None:
                await asyncio.sleep(0)
                continue
            try:
                async with asyncio.timeout(self.idle_interval_seconds):
                    await self._stopping.wait()
            except TimeoutError:
                pass

    def _save(self, job, **changes):
        return self.jobs.save(
            changed_download(job, self.jobs.timestamp(), **changes), expected_revision=job.revision
        )

    def _fail(self, job, error, *, failure_at=None):
        latest = self.jobs.get(job.id)
        updated = failed_download(latest, error, self.jobs.timestamp(), failure_at=failure_at)
        return self.jobs.save(updated, expected_revision=latest.revision)

    async def run_once(self):
        async with self._turn_lock:
            if self._stopping.is_set():
                return None
            now = datetime.fromisoformat(self.jobs.timestamp())
            candidates = []
            for raw in self.media.store.snapshot()["jobs"].values():
                job = parse_job(raw)
                if not isinstance(job, JobV2) or job.download is None:
                    continue
                record = job.download
                if record.phase in {"prepared", "writing"} or (
                    record.phase == "pending"
                    and record.next_attempt_at is not None
                    and datetime.fromisoformat(record.next_attempt_at) <= now
                ):
                    candidates.append(job)
            if not candidates:
                return None
            job = min(
                candidates, key=lambda value: (value.download.next_attempt_at or value.updated_at, value.id)
            )
            if job.download.phase == "writing":
                return self._fail(
                    job, MediaError("DOWNLOAD_NETWORK", retryable=True), failure_at=job.download.deadline_at
                )
            try:
                async with asyncio.timeout(job.download_policy.total_timeout_seconds):
                    if job.download.phase == "prepared":
                        return await self._finish(job)
                    return await self._download(job)
            except TimeoutError:
                return self._fail(job, MediaError("DOWNLOAD_TIMEOUT", retryable=True))
            except MediaError as error:
                return self._fail(job, error)
            except AppError as error:
                if error.code != "JOB_REVISION_CONFLICT":
                    raise
                # A concurrent availability check may advance a repair's revision.
                # Its prepared intent survives; the next iteration completes it.
                return self.jobs.get(job.id)

    def _existing_final(self, job):
        try:
            if job.download.prepared is None:
                with self.files.open(job.download.relative_path):
                    raise MediaError("MEDIA_FILE_CONFLICT")
            with self.files.verified(job.download.relative_path, job.download.prepared):
                return True
        except FileNotFoundError:
            return False

    async def _download(self, job):
        if await disk_io(self._existing_final, job):
            job = self._save(self.jobs.get(job.id), download={"phase": "prepared", "nextAttemptAt": None})
            return await self._finish(job)
        if job.download.window_attempts >= job.download_policy.max_attempts_per_window:
            raise MediaError("DOWNLOAD_NETWORK")
        timestamp = self.jobs.timestamp()
        job = self._save(
            job,
            download={
                "phase": "writing",
                "attempts": job.download.attempts + 1,
                "windowAttempts": job.download.window_attempts + 1,
                "nextAttemptAt": None,
                "startedAt": timestamp,
                "deadlineAt": after_seconds(timestamp, job.download_policy.total_timeout_seconds),
                "error": None,
            },
        )
        part = job.download.relative_path + ".part"
        await disk_io(self.files.remove_part, part)
        try:
            # Directory and file leases are held until every asynchronous write has
            # completed, including cancellation and total-timeout paths.
            with self.files.open(part, writing=True) as file:
                await self.client.download(job, file, timestamp=timestamp)
                prepared = await disk_io(self.files.prepare, file, job, self.jobs.timestamp())
        except OSError as error:
            raise disk_error(error) from None
        job = self._save(
            self.jobs.get(job.id),
            download={
                "phase": "prepared",
                "prepared": prepared.model_dump(mode="json", by_alias=True),
                "nextAttemptAt": None,
            },
        )
        return await self._finish(job)

    def _publish_file(self, job):
        relative, prepared = job.download.relative_path, job.download.prepared
        if self._existing_final(job):
            # A previous process may have published the file but not its index.
            self.files.remove_part(relative + ".part")
            return True
        try:
            with self.files.verified(relative + ".part", prepared):
                pass
        except FileNotFoundError:
            return False
        try:
            self.files.promote(relative)
        except FileExistsError:
            # No-clobber publication preserved the other file; compare it before use.
            self._existing_final(job)
        with self.files.verified(relative, prepared):
            pass
        return True

    async def _finish(self, job):
        if not await disk_io(self._publish_file, job):
            return self._fail(
                job, MediaError("DOWNLOAD_INCOMPLETE", retryable=True), failure_at=job.download.deadline_at
            )
        # Store failures escape and leave phase=prepared. Filesystem work never
        # takes place in the transaction that commits index/result/status together.
        return self.media.commit(self.jobs.get(job.id))
