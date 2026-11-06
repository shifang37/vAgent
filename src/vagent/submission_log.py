"""Write-ahead log for paid provider submissions.

``state.json`` is large and can briefly be locked by a Windows indexer, editor or
antivirus scanner. The provider handle is the only proof that a paid task exists, so it
is appended to a small fsync'd JSONL sidecar *before* the state transaction. On open, a
Job left ``submitting`` (or ``unknown``) adopts the matching handle instead of becoming
an unrecoverable ``SUBMISSION_UNKNOWN`` with no task ID.

The log is an operational sidecar, not schema: it never enters ``state.json`` and the
frozen Job contracts are unchanged. One process owns it at a time via ``instance.lock``.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

LOG_VERSION = 1
LOG_FILE = "submission-handles.jsonl"
ROTATE_BYTES = 1024 * 1024
# Records are only useful while a Job can still be recovered. Recovery keeps entries
# for non-terminal Jobs only; everything else is compacted away at store open.
_KEEP_STATUSES = frozenset(
    {"pending_submit", "submitting", "queued", "running", "downloading", "download_failed", "unknown"}
)


class SubmissionLog:
    def __init__(self, home):
        self.home = Path(home)
        self.path = self.home / LOG_FILE

    def append_intent(self, job_id: str, provider: str, request_id: str | None) -> bool:
        return self._append(
            {"phase": "intent", "jobId": job_id, "provider": provider, "requestId": request_id}
        )

    def append_handle(self, job_id: str, provider: str, request_id: str | None, task_id: str) -> bool:
        return self._append(
            {
                "phase": "handle",
                "jobId": job_id,
                "provider": provider,
                "requestId": request_id,
                "taskId": task_id,
            }
        )

    def _append(self, payload: dict) -> bool:
        record = {
            "version": LOG_VERSION,
            **payload,
            "at": datetime.now(UTC).isoformat(),
        }
        line = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        try:
            self.home.mkdir(parents=True, exist_ok=True)
            self._rotate_if_needed(len(line))
            descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(descriptor, line)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError:
            # The primary state transaction still runs; a missing redundancy record
            # must not change the submission decision by itself.
            return False
        return True

    def _rotate_if_needed(self, incoming: int) -> None:
        try:
            size = self.path.stat().st_size
        except OSError:
            return
        if size + incoming <= ROTATE_BYTES:
            return
        backup = self.path.with_name(f"{LOG_FILE}.1")
        try:
            os.replace(self.path, backup)
        except OSError:
            # Keep appending to the current log rather than dropping the handle.
            return

    def entries(self) -> dict[str, dict[str, dict]]:
        """``jobId -> {"intent"|"handle": record}``; the last record of each phase wins."""
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError:
            return {}
        entries: dict[str, dict[str, dict]] = {}
        for line in raw.splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict) or record.get("version") != LOG_VERSION:
                continue
            job_id, phase = record.get("jobId"), record.get("phase")
            if not isinstance(job_id, str) or phase not in {"intent", "handle"}:
                continue
            if phase == "handle" and not isinstance(record.get("taskId"), str):
                continue
            entries.setdefault(job_id, {})[phase] = record
        return entries

    def handles(self) -> dict[str, dict]:
        """Every Job that has a confirmed provider task ID in the log."""
        return {job_id: phases["handle"] for job_id, phases in self.entries().items() if "handle" in phases}

    def compact(self, state: dict) -> None:
        """Rewrite only the records a future recovery can still need."""
        entries = self.entries()
        if not entries:
            return
        kept: list[dict] = []
        for job_id, phases in entries.items():
            job = state.get("jobs", {}).get(job_id)
            status = job.get("status") if isinstance(job, dict) else None
            if status is None or status in _KEEP_STATUSES:
                kept.extend({**record, "jobId": job_id} for record in phases.values())
        if not kept:
            try:
                self.path.unlink()
            except OSError:
                pass
            return
        kept.sort(key=lambda record: (record.get("at", ""), record.get("phase", "")))
        payload = "\n".join(json.dumps(record, ensure_ascii=False, sort_keys=True) for record in kept)
        temporary = self.path.with_name(f"{LOG_FILE}.{os.getpid()}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                handle.write(payload + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except OSError:
            pass
        finally:
            temporary.unlink(missing_ok=True)
