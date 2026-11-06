"""Atomic, single-writer JSON storage; domain writes and operation results commit together."""

import copy
import errno
import hashlib
import json
import os
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal, TypeVar
from uuid import uuid4

from langchain_core.messages import messages_from_dict
from pydantic import BaseModel, ConfigDict, Field, JsonValue
from pydantic.alias_generators import to_camel

from vagent.config import assert_id
from vagent.errors import AppError, failure
from vagent.submission_log import SubmissionLog
from vagent.video.audio import AudioAsset, AudioMedia, validate_audio_changes
from vagent.video.batches import recover_interrupted_batches, validate_batch_changes
from vagent.video.contracts import Job, MediaAsset, StoredJob
from vagent.video.episodes import Character, Episode, Scene, Style
from vagent.video.films import Film, FilmMedia
from vagent.video.renders import RenderTask
from vagent.video.seasons import Season, validate_season_changes
from vagent.video.storyboards import Asset, Storyboard
from vagent.video.subtitles import SubtitleTrack, validate_subtitle_changes
from vagent.video.timelines import validate_timeline_changes
from vagent.waiting import WAIT_EXECUTION_VERSIONS, StoredWait, WaitBinding, parse_wait

T = TypeVar("T")

# Windows indexers, antivirus scanners and editors hold short-lived handles on
# state.json. A replace that loses that race is retried for well under a second
# instead of turning one transient lock into a failed paid operation.
WRITE_ATTEMPTS = 6
WRITE_RETRY_BASE_SECONDS = 0.02
WRITE_RETRY_MAX_SECONDS = 0.32
_TRANSIENT_WINERRORS = frozenset({5, 32, 33})
_TRANSIENT_ERRNOS = frozenset({errno.EACCES, errno.EPERM, errno.EBUSY})

# Growth control for a long-lived home directory (FX-20). Every value is a bound, not a
# correctness input: pruning old events/model calls never changes a Run's verdict.
MAX_EVENTS_PER_RUN = 500
RETENTION_DAYS = 30
TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "cancelled"})
LAST_GOOD_FILE = "state.last-good.json"
GC_MARKER_FILE = "gc.json"
SNAPSHOT_EVERY = 20
# Keep the exact pre-restore bytes next to a manual ``recover --from last-good``.
INVALID_STORE_BACKUP_PREFIX = "state-invalid-"


def now() -> str:
    return datetime.now(UTC).isoformat()


def _transient_lock_error(error: OSError) -> bool:
    if getattr(error, "winerror", None) in _TRANSIENT_WINERRORS:
        return True
    return error.errno in _TRANSIENT_ERRNOS


def _retry_transient(operation: Callable[[], T], *, attempts: int = WRITE_ATTEMPTS) -> T:
    delay = WRITE_RETRY_BASE_SECONDS
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except OSError as error:
            if not _transient_lock_error(error) or attempt == attempts:
                raise
            time.sleep(delay)
            delay = min(WRITE_RETRY_MAX_SECONDS, delay * 2)
    raise AssertionError("unreachable")


def read_bytes_with_retry(path: Path) -> bytes:
    """Read a store file through the same transient-lock window as a write."""
    return _retry_transient(path.read_bytes)


def atomic_write_bytes(path: Path, content: bytes) -> None:
    """Replace one local file only after its complete contents have been flushed."""
    temporary = path.with_name(f"{path.stem}-{uuid4()}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            _retry_transient(lambda: os.replace(temporary, path))
        except OSError as error:
            if not _transient_lock_error(error):
                raise
            raise AppError(
                "STORAGE_BUSY", "本地状态文件被占用，写入未完成；已保留原文件，请稍后重试。"
            ) from error
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_json(path: Path, value: dict) -> None:
    atomic_write_bytes(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8"))


class Record(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid", strict=True)


class PlanStep(Record):
    text: str
    status: Literal["pending", "in_progress", "completed"]


class Project(Record):
    id: str
    revision: int = Field(ge=0)
    goal: str
    audience: str
    style: str
    constraints: list[str]
    plan: list[PlanStep]
    content_limits: dict[Literal["all", "brief", "script", "storyboard"], Annotated[int, Field(ge=1)]] = (
        Field(default_factory=dict)
    )


class StoredMessage(Record):
    type: Literal["human", "ai", "tool"]
    data: dict[str, Any]


class Session(Record):
    id: str
    messages: list[StoredMessage]
    latest_run_id: str | None = None


class ContentCheck(Record):
    characters: int = Field(ge=1)
    max_characters: int | None = Field(default=None, ge=1)
    method: Literal["unicode_non_whitespace_v1"]


class ArtifactVersion(Record):
    version: int = Field(ge=1)
    title: str
    content: str
    created_at: str
    content_check: ContentCheck | None = None


class Artifact(Record):
    id: str
    project_id: str
    kind: Literal["brief", "script", "storyboard"]
    versions: list[ArtifactVersion] = Field(min_length=1)


class SavedPolicy(Record):
    max_steps: int = Field(gt=0)
    max_tool_calls: int = Field(gt=0)
    timeout_seconds: float = Field(gt=0, allow_inf_nan=False)


class ModelCall(Record):
    step: int = Field(ge=1)
    status: Literal["started", "responded", "failed", "cancelled", "interrupted"]
    started_at: str
    finished_at: str | None = None
    duration_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cache_hit_tokens: int | None = Field(default=None, ge=0)
    cache_miss_tokens: int | None = Field(default=None, ge=0)
    cache_usage_source: Literal["reported", "derived", "missing", "invalid"] = "missing"
    error_code: str | None = None


class RunRecordV1(Record):
    id: str
    session_id: str
    request_id: str
    prompt: str
    model: str
    status: Literal["running", "completed", "failed", "cancelled", "interrupted"]
    messages: list[StoredMessage]
    model_steps: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    context_bytes: int = Field(default=0, ge=0)
    dropped_messages: int = Field(default=0, ge=0)
    answer: str
    error_code: str | None = None
    created_at: str
    updated_at: str
    execution_version: int | None = None
    context_signature: str | None = None
    context_version: int = Field(default=1, ge=1)
    policy: SavedPolicy | None = None
    resumable: bool = False
    active_seconds: float = Field(default=0, ge=0, allow_inf_nan=False)
    in_flight_seconds: float = Field(default=0, ge=0, allow_inf_nan=False)
    tool_call_keys: list[str] = Field(default_factory=list)
    usage_start_step: int | None = Field(default=None, ge=1)
    model_calls: list[ModelCall] = Field(default_factory=list)
    read_only: bool = False
    answer_cache: dict[str, int] = Field(default_factory=dict)
    events: list[dict[str, Any]] = Field(default_factory=list)
    # E3 headless plan/authorization/delivery, or None for every ordinary Run.
    headless: dict[str, Any] | None = None


class RunRecordV2(RunRecordV1):
    status: Literal["running", "waiting_external", "completed", "failed", "cancelled", "interrupted"]
    video_mode: Literal["off", "mock"] = "off"
    tool_features: dict[str, dict[str, JsonValue]] = Field(default_factory=dict)
    active_wait_id: str | None = None
    external_wait_seconds: float = Field(default=0, ge=0, allow_inf_nan=False)
    external_wait_started_at: str | None = None
    wait_resume_error: dict[str, str] | None = None


class RunRecord(RunRecordV2):
    video_mode: Literal["off", "mock", "live"] = "off"


class Success(Record):
    ok: Literal[True]
    data: Any


class ErrorDetail(Record):
    code: str
    message: str


class Failure(Record):
    ok: Literal[False]
    error: ErrorDetail


class Operation(Record):
    fingerprint: str
    result: Success | Failure = Field(discriminator="ok")


class DatabaseV1(Record):
    schema_version: Literal[1]
    projects: dict[str, Project]
    sessions: dict[str, Session]
    artifacts: dict[str, Artifact]
    runs: dict[str, RunRecordV1]
    operations: dict[str, Operation]


class DatabaseV2(DatabaseV1):
    schema_version: Literal[2]
    runs: dict[str, RunRecordV2]
    jobs: dict[str, Job]
    waits: dict[str, WaitBinding]


class DatabaseV3(DatabaseV2):
    schema_version: Literal[3]
    runs: dict[str, RunRecord]
    jobs: dict[str, StoredJob]
    media: dict[str, MediaAsset]


class DatabaseV4(DatabaseV3):
    schema_version: Literal[4]
    storyboards: dict[str, Storyboard]
    assets: dict[str, Asset]
    # M2 adds the set wait next to the frozen single-Job binding.
    waits: dict[str, StoredWait]


class DatabaseV5(DatabaseV4):
    """Schema 5 adds the film domain; the media index becomes a typed union."""

    schema_version: Literal[5]
    media: dict[str, MediaAsset | FilmMedia]
    films: dict[str, Film]


class Database(DatabaseV5):
    """Schema 6 adds the M5 episode domain as four empty-capable collections."""

    schema_version: Literal[6]
    episodes: dict[str, Episode]
    scenes: dict[str, Scene]
    characters: dict[str, Character]
    styles: dict[str, Style]


class DatabaseV7(Database):
    """Schema 7 adds the M6 audio/subtitle domains and the audio media branch."""

    schema_version: Literal[7]
    media: dict[str, MediaAsset | FilmMedia | AudioMedia]
    audio: dict[str, AudioAsset]
    subtitle_tracks: dict[str, SubtitleTrack]


class DatabaseV8(DatabaseV7):
    """Schema 8 adds the E3 render task collection; film/media shapes stay a union."""

    schema_version: Literal[8]
    renders: dict[str, RenderTask]


class DatabaseV9(DatabaseV8):
    """Schema 9 adds the M7 season/batch/timeline collections.

    E1 implements seasons only; ``batches`` and ``timelines`` stay empty mappings
    until E2/E3 add their own records and Store validators. They are typed as JSON
    objects here so the v9 envelope never has to change shape again.
    """

    schema_version: Literal[9]
    seasons: dict[str, Season]
    batches: dict[str, dict]
    timelines: dict[str, dict]


def migrated_state(state: dict) -> dict:
    """Mechanical envelope upgrade; never re-encode historical domain objects."""
    version = state["schemaVersion"]
    if type(version) is not int or version not in {1, 2, 3, 4, 5, 6, 7, 8, 9}:
        raise ValueError("Unsupported schema version")
    upgraded = copy.deepcopy(state)
    if version == 1:
        upgraded.update(jobs={}, waits={})
    if version < 3:
        upgraded.update(media={})
    if version < 4:
        upgraded.update(storyboards={}, assets={})
    if version < 5:
        upgraded.update(schemaVersion=5, films={})
    if version < 6:
        upgraded.update(schemaVersion=6, episodes={}, scenes={}, characters={}, styles={})
    if version < 7:
        upgraded.update(schemaVersion=7, audio={}, subtitleTracks={})
    if version < 8:
        upgraded.update(schemaVersion=8, renders={})
    if version < 9:
        upgraded.update(schemaVersion=9, seasons={}, batches={}, timelines={})
    return upgraded


def repair_stale_runs(store: "FileStore") -> list[str]:
    """Close persisted ``running`` Runs that no live execution owns.

    A failed final write can leave a Run marked ``running`` while its execution lock is
    already free, which would answer every later start with ``RUN_BUSY`` and, for older
    execution versions without a wait coordinator, could not even be stopped. The
    caller must prove the lock is free first (an active execution always holds it).
    """
    if not any(run["status"] == "running" for run in store.snapshot()["runs"].values()):
        return []

    def repair(draft):
        repaired = []
        for run in draft["runs"].values():
            if run["status"] != "running":
                continue
            run.update(status="interrupted", updatedAt=now())
            # The lost attempt's elapsed time is unknown; charge its reservation once.
            run["activeSeconds"] = run.get("activeSeconds", 0) + run.get("inFlightSeconds", 0)
            run["inFlightSeconds"] = 0
            for call in run.get("modelCalls", []):
                if call["status"] == "started":
                    call.update(status="interrupted", errorCode="INTERRUPTED")
            repaired.append(run["id"])
        return repaired

    return store.transaction(repair)


def load_snapshot(home: Path | str) -> dict:
    """Read ``state.json`` without locking, migrating, recovering or creating anything.

    This is the read-only counterpart of :meth:`FileStore.open` for statistics scripts:
    a ``running`` Run stays ``running`` and a missing data directory is an error instead
    of a freshly created empty store. Schema upgrades are applied in memory only.
    """
    home = Path(home).resolve()
    try:
        raw = read_bytes_with_retry(home / "state.json")
    except (FileNotFoundError, NotADirectoryError):
        raise AppError("STORE_NOT_FOUND", "数据目录不存在或没有 state.json；未创建任何文件。") from None
    except OSError as error:
        raise AppError("STORE_UNREADABLE", "本地状态文件无法读取；未执行任何写入。") from error
    try:
        state = json.loads(raw)
        version = state["schemaVersion"]
        if type(version) is not int or version not in {1, 2, 3, 4, 5, 6, 7, 8, 9}:
            raise ValueError("Unsupported schema version")
        if version < 9:
            state = migrated_state(state)
        DatabaseV9.model_validate(state)
    except (ValueError, TypeError, KeyError):
        raise AppError("INVALID_STORE", "本地状态损坏或版本不支持，未执行任何写入。") from None
    return state


def append_event(run: dict, event: dict, *, limit: int = MAX_EVENTS_PER_RUN) -> dict:
    """Append one journal event while keeping ``sequence`` monotonic after trimming.

    ``assistant.delta`` never reaches the journal (the draft is live-only), so the
    remaining volume is one event per model/tool/Run transition; a 500-event window is
    several orders of magnitude above a normal Run and still bounded. The number of
    dropped events is derivable without a new stored field: the first retained event's
    ``sequence`` minus one. That keeps every frozen ``RunRecord`` schema hash unchanged.
    """
    events = run.setdefault("events", [])
    last = events[-1].get("sequence", 0) if events else 0
    appended = {**event, "sequence": int(last) + 1, "at": now()}
    events.append(appended)
    overflow = len(events) - limit
    if overflow > 0:
        del events[:overflow]
    return appended


def dropped_events(run: dict) -> int:
    """How many journal events this Run no longer carries (0 before any trimming)."""
    events = run.get("events") or []
    if not events:
        return 0
    return max(0, int(events[0].get("sequence", 1)) - 1)


def collect_garbage(
    state: dict,
    *,
    now: str | None = None,
    days: int = RETENTION_DAYS,
    apply: bool = True,
) -> dict:
    """Prune detail of Runs that finished longer than ``days`` ago (FX-20).

    Only *detail* is removed: an old terminal Run keeps its id, status, counters, answer
    and artifacts, so ``inspect`` still explains what happened. Events, per-call usage,
    cached tool results (``operations``) and their waits are the collections that grow
    without bound. Active Runs (``running`` / ``waiting_external`` / ``interrupted``)
    are never touched, and nothing is written unless ``apply`` is true.
    """
    if type(days) is not int or days < 1:
        raise AppError("INVALID_ARGUMENTS", "保留天数必须是至少 1 的整数。")
    reference = now or datetime.now(UTC).isoformat()
    cutoff = (datetime.fromisoformat(reference) - timedelta(days=days)).isoformat()
    before_bytes = len(json.dumps(state, ensure_ascii=False).encode("utf-8"))
    stale = sorted(
        run_id
        for run_id, run in state["runs"].items()
        if run.get("status") in TERMINAL_RUN_STATUSES
        and str(run.get("updatedAt") or run.get("createdAt") or "") < cutoff
    )
    stale_set = set(stale)
    operation_keys = sorted(key for key in state["operations"] if key.split(":", 1)[0] in stale_set)
    wait_ids = sorted(
        wait_id
        for wait_id, wait in state["waits"].items()
        if (wait.get("context") or {}).get("runId") in stale_set
    )
    report = {
        "at": reference,
        "cutoff": cutoff,
        "days": days,
        "runs": len(stale),
        "events": sum(len(state["runs"][run_id].get("events", [])) for run_id in stale),
        "modelCalls": sum(len(state["runs"][run_id].get("modelCalls", [])) for run_id in stale),
        "operations": len(operation_keys),
        "waits": len(wait_ids),
        "beforeBytes": before_bytes,
        "afterBytes": before_bytes,
    }
    if not apply or not (stale or operation_keys or wait_ids):
        return report
    for run_id in stale:
        run = state["runs"][run_id]
        run["events"] = []
        run["modelCalls"] = []
    for key in operation_keys:
        state["operations"].pop(key, None)
    for wait_id in wait_ids:
        state["waits"].pop(wait_id, None)
    report["afterBytes"] = len(json.dumps(state, ensure_ascii=False).encode("utf-8"))
    report["freedBytes"] = report["beforeBytes"] - report["afterBytes"]
    return report


def record_gc_marker(home: Path | str, report: dict) -> Path:
    """Remember when the last ``gc --apply`` ran, in a sidecar rather than state.json.

    The v9 envelope is frozen; a marker file keeps ``state.json`` byte-shape compatible
    with every program that already reads it.
    """
    path = Path(home) / GC_MARKER_FILE
    atomic_write_json(path, {"lastGcAt": report["at"], "days": report["days"]})
    return path


def last_gc_at(home: Path | str) -> str | None:
    try:
        payload = json.loads(read_bytes_with_retry(Path(home) / GC_MARKER_FILE))
    except (OSError, ValueError):
        return None
    value = payload.get("lastGcAt") if isinstance(payload, dict) else None
    return value if isinstance(value, str) else None


def restore_last_good(home: Path | str) -> dict:
    """Restore ``state.json`` from the last-good snapshot after an explicit request.

    The snapshot is only ever written by a *successful* commit, so it is the newest
    bytes this program knows to be valid. The unreadable current file is preserved as
    ``state-invalid-<uuid>.json`` first: recovery must never destroy evidence.
    """
    home = Path(home).resolve()
    snapshot = home / LAST_GOOD_FILE
    try:
        raw = read_bytes_with_retry(snapshot)
    except (FileNotFoundError, NotADirectoryError):
        raise AppError("NOT_FOUND", "没有可用的 last-good 快照；未修改任何文件。") from None
    except OSError as error:
        raise AppError("STORE_UNREADABLE", "last-good 快照无法读取；未修改任何文件。") from error
    try:
        state = json.loads(raw)
        DatabaseV9.model_validate(migrated_state(state) if state["schemaVersion"] < 9 else state)
    except (ValueError, TypeError, KeyError):
        raise AppError("INVALID_STORE", "last-good 快照本身损坏；未修改任何文件。") from None
    current = home / "state.json"
    if current.exists():
        atomic_write_bytes(home / f"{INVALID_STORE_BACKUP_PREFIX}{uuid4()}.json", current.read_bytes())
    atomic_write_bytes(current, raw)
    return {"restoredFrom": str(snapshot), "schemaVersion": state["schemaVersion"]}


def _process_exists(pid: int) -> bool:
    """Whether ``pid`` names a live process, without ever signalling it.

    POSIX uses the harmless signal 0. On Windows ``os.kill`` with any non-console signal
    calls ``TerminateProcess``, so liveness is checked with ``OpenProcess`` and
    ``GetExitCodeProcess`` instead.
    """
    if type(pid) is not int or pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            process_query_limited_information = 0x1000
            still_active = 259
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.restype = wintypes.HANDLE
            handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
            if not handle:
                return False
            try:
                exit_code = wintypes.DWORD()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return False
                return exit_code.value == still_active
            finally:
                kernel32.CloseHandle(handle)
        except OSError:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _lock_is_stale(lock: Path) -> bool:
    """A lock is stale only when its recorded PID is provably gone.

    An unreadable body is *not* treated as stale: without a PID we cannot prove the
    owner is dead, and stealing a live writer's lock is worse than a manual cleanup.
    """
    try:
        payload = json.loads(read_bytes_with_retry(lock))
    except (OSError, ValueError):
        return False
    pid = payload.get("pid") if isinstance(payload, dict) else None
    return not _process_exists(pid)


class FileStore:
    def __init__(self, home: Path, state: dict):
        self.home = home
        self._state = state
        self._mutex = threading.RLock()
        self._closed = False
        self._commits = 0

    @classmethod
    def open(cls, home: Path | str) -> "FileStore":
        home = Path(home).resolve()
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = home / "instance.lock"
        cls._acquire_lock(lock)
        created = not (home / "state.json").exists()
        try:
            try:
                raw = read_bytes_with_retry(home / "state.json")
            except FileNotFoundError:
                state = {
                    "schemaVersion": 9,
                    "projects": {},
                    "sessions": {},
                    "artifacts": {},
                    "runs": {},
                    "operations": {},
                    "jobs": {},
                    "waits": {},
                    "media": {},
                    "storyboards": {},
                    "assets": {},
                    "films": {},
                    "episodes": {},
                    "scenes": {},
                    "characters": {},
                    "styles": {},
                    "audio": {},
                    "subtitleTracks": {},
                    "renders": {},
                    "seasons": {},
                    "batches": {},
                    "timelines": {},
                }
            else:
                try:
                    state = json.loads(raw)
                    version = state["schemaVersion"]
                    if type(version) is not int or version not in {1, 2, 3, 4, 5, 6, 7, 8, 9}:
                        raise ValueError("Unsupported schema version")
                    {
                        1: DatabaseV1,
                        2: DatabaseV2,
                        3: DatabaseV3,
                        4: DatabaseV4,
                        5: DatabaseV5,
                        6: Database,
                        7: DatabaseV7,
                        8: DatabaseV8,
                        9: DatabaseV9,
                    }[version].model_validate(state)
                    for collection in (state["sessions"], state["runs"]):
                        for item in collection.values():
                            messages_from_dict(item["messages"])
                    from vagent.video.episodes import (
                        validate_character_changes,
                        validate_episode_changes,
                        validate_scene_changes,
                        validate_style_changes,
                    )
                    from vagent.video.films import validate_film_changes
                    from vagent.video.jobs import validate_job_changes
                    from vagent.video.renders import validate_render_changes
                    from vagent.video.storyboards import (
                        validate_asset_changes,
                        validate_storyboard_changes,
                    )

                    checked = migrated_state(state) if version < 9 else state
                    validate_job_changes(checked["jobs"], checked)
                    validate_storyboard_changes(checked["storyboards"], checked)
                    validate_asset_changes(checked["assets"], checked)
                    validate_film_changes(checked["films"], checked)
                    validate_episode_changes(checked["episodes"], checked)
                    validate_scene_changes(checked["scenes"], checked)
                    validate_character_changes(checked["characters"], checked)
                    validate_style_changes(checked["styles"], checked)
                    validate_audio_changes(checked["audio"], checked)
                    validate_subtitle_changes(checked["subtitleTracks"], checked)
                    validate_render_changes(checked["renders"], checked)
                    validate_season_changes(checked.get("seasons", {}), checked)
                    validate_batch_changes(checked.get("batches", {}), checked, initial=True)
                    validate_timeline_changes(checked.get("timelines", {}), checked)
                except (ValueError, TypeError, KeyError, AppError):
                    raise AppError(
                        "INVALID_STORE", "本地状态损坏或版本不支持，已保留原文件，未重置数据。"
                    ) from None
                if version < 9:
                    # Keep the exact pre-migration bytes, including legacy message encoding.
                    atomic_write_bytes(home / f"state-v{version}-{uuid4()}.json", raw)
                    state = migrated_state(state)
                    DatabaseV9.model_validate(state)
                    # Migration and runtime recovery are distinct commits. In particular,
                    # no fingerprints/default fields or SQLite checkpoints are rewritten.
                    atomic_write_json(home / "state.json", state)
            store = cls(home, state)
            submission_log = SubmissionLog(home)
            handles = submission_log.handles()

            def recover(draft: dict) -> None:
                from vagent.video.jobs import recover_interrupted_jobs

                for task in draft.get("renders", {}).values():
                    if task.get("status") == "running":
                        task["status"] = "failed"
                        task["errorCode"] = "RENDER_INTERRUPTED"
                        task["updatedAt"] = now()
                        task["revision"] = task.get("revision", 0) + 1
                        for scene in task.get("scenes", []):
                            if scene.get("status") in {"pending", "running"}:
                                scene.update(
                                    status="failed",
                                    failureStage="source",
                                    errorCode="RENDER_INTERRUPTED",
                                )
                for run in draft["runs"].values():
                    if run["status"] == "running":
                        run.update(status="interrupted", updatedAt=now())
                        # An abruptly lost model request has unknown elapsed time. Charge
                        # its reserved timeout once, never the time spent offline.
                        run["activeSeconds"] = run.get("activeSeconds", 0) + run.get("inFlightSeconds", 0)
                        run["inFlightSeconds"] = 0
                        for call in run.get("modelCalls", []):
                            if call["status"] == "started":
                                call.update(status="interrupted", errorCode="INTERRUPTED")
                        if run.get("executionVersion") in WAIT_EXECUTION_VERSIONS:
                            bindings = [
                                parse_wait(raw)
                                for raw in draft["waits"].values()
                                if raw["context"]["runId"] == run["id"]
                                and raw["autoResume"]
                                and raw["status"] != "stopped"
                            ]
                            binding = max(
                                bindings,
                                key=lambda b: (b.context.model_step, b.batch_index or 0, b.started_at),
                                default=None,
                            )
                            boundary = (
                                binding.claimed_model_steps
                                if binding and binding.claimed_model_steps is not None
                                else binding.context.model_step
                                if binding
                                else -1
                            )
                            if binding and run["modelSteps"] <= boundary:
                                # Reserve the unfinished Run slot even before a model
                                # configuration is available to inspect SQLite. The
                                # coordinator still must verify the actual checkpoint.
                                run.update(
                                    status="waiting_external",
                                    activeWaitId=None if binding.status == "delivered" else binding.id,
                                )
                                if binding.status in {"preparing", "armed", "ready"}:
                                    run["externalWaitStartedAt"] = (
                                        run.get("externalWaitStartedAt") or binding.started_at
                                    )
                recover_interrupted_jobs(draft, now(), handles=handles)
                recover_interrupted_batches(draft, now())

            store.transaction(recover, write_if_changed=not created)
            submission_log.compact(store.snapshot())
            from vagent.video.films import cleanup_stale_parts

            cleanup_stale_parts(store.home, store.snapshot())
            return store
        except BaseException:
            lock.unlink(missing_ok=True)
            raise

    @staticmethod
    def _acquire_lock(lock: Path) -> None:
        """Create the single-writer lock, reclaiming one whose owner PID is gone.

        A crash leaves ``instance.lock`` behind forever; refusing to open then would
        make the whole home unusable until a human deleted the file. Only a proven-dead
        PID is reclaimed, so a live process (even a different user's) is never preempted.
        """
        for _ in range(3):
            try:
                handle = lock.open("x", encoding="utf-8")
            except FileExistsError:
                if not _lock_is_stale(lock):
                    raise AppError(
                        "STORE_LOCKED",
                        "数据目录已被锁定。确认没有 vagent 进程后，再手动移除 instance.lock。",
                    ) from None
                lock.unlink(missing_ok=True)
                continue
            with handle:
                json.dump({"pid": os.getpid(), "createdAt": now()}, handle)
            return
        raise AppError("STORE_LOCKED", "数据目录锁正在被其他进程更新，请稍后重试。")

    def snapshot(self) -> dict:
        with self._mutex:
            return copy.deepcopy(self._state)

    @contextmanager
    def locked(self):
        """Keep auxiliary local files under this live instance's single-writer lock."""
        with self._mutex:
            if self._closed:
                raise AppError("STORE_CLOSED", "状态存储已关闭。")
            yield

    def transaction(self, mutate: Callable[[dict], T], *, write_if_changed: bool = False) -> T:
        from vagent.video.episodes import (
            validate_character_changes,
            validate_episode_changes,
            validate_scene_changes,
            validate_style_changes,
        )
        from vagent.video.films import validate_film_changes
        from vagent.video.jobs import validate_job_changes, validate_media_changes
        from vagent.video.renders import validate_render_changes
        from vagent.video.storyboards import validate_asset_changes, validate_storyboard_changes

        with self.locked():
            draft = copy.deepcopy(self._state)
            value = mutate(draft)
            if write_if_changed and draft == self._state:
                # Opening a healthy store must not rewrite (or re-mtime) state.json:
                # the recovery pass is a no-op on almost every start.
                return copy.deepcopy(value)
            DatabaseV9.model_validate(draft)
            validate_job_changes(self._state["jobs"], draft)
            validate_media_changes(self._state["media"], draft)
            validate_storyboard_changes(self._state["storyboards"], draft)
            validate_asset_changes(self._state["assets"], draft)
            validate_film_changes(self._state["films"], draft)
            validate_episode_changes(self._state["episodes"], draft)
            validate_scene_changes(self._state["scenes"], draft)
            validate_character_changes(self._state["characters"], draft)
            validate_style_changes(self._state["styles"], draft)
            validate_audio_changes(self._state["audio"], draft)
            validate_subtitle_changes(self._state["subtitleTracks"], draft)
            validate_render_changes(self._state.get("renders", {}), draft)
            validate_season_changes(self._state.get("seasons", {}), draft)
            validate_batch_changes(self._state.get("batches", {}), draft)
            validate_timeline_changes(self._state.get("timelines", {}), draft)
            atomic_write_json(self.home / "state.json", draft)
            self._state = draft
            self._commits += 1
            if self._commits % SNAPSHOT_EVERY == 0:
                self._write_last_good(draft)
            return copy.deepcopy(value)

    def _write_last_good(self, draft: dict) -> None:
        """Refresh the explicit-recovery snapshot; a copy failure never fails the write."""
        try:
            encoded = json.dumps(draft, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
            atomic_write_bytes(self.home / LAST_GOOD_FILE, encoded)
        except OSError:
            pass

    def snapshot_last_good(self) -> Path:
        """Explicitly refresh ``state.last-good.json`` from the current committed state."""
        encoded = json.dumps(self._state, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
        atomic_write_bytes(self.home / LAST_GOOD_FILE, encoded)
        return self.home / LAST_GOOD_FILE

    def ensure_session(self, session_id: str) -> None:
        assert_id(session_id)

        def create(draft: dict) -> None:
            draft["sessions"].setdefault(session_id, {"id": session_id, "messages": []})
            draft["projects"].setdefault(
                session_id,
                {
                    "id": session_id,
                    "revision": 0,
                    "goal": "",
                    "audience": "",
                    "style": "",
                    "constraints": [],
                    "plan": [],
                },
            )

        self.transaction(create)

    @staticmethod
    def operation_fingerprint(name: str, args: dict) -> str:
        # Match the original JSON encoding so existing journal entries remain usable.
        encoded = json.dumps({"name": name, "args": args}, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def operation_result(self, key: str, name: str, args: dict) -> dict | None:
        with self._mutex:
            return copy.deepcopy(
                self._operation_result(self._state, key, self.operation_fingerprint(name, args))
            )

    @staticmethod
    def _operation_result(state: dict, key: str, fingerprint: str) -> dict | None:
        for raw in state["waits"].values():
            context = raw["context"]
            wait_key = f"{context['runId']}:{context['modelStep']}:{context['toolCallId']}"
            if wait_key == key and raw.get("operationFingerprint") not in {None, fingerprint}:
                return failure("OPERATION_CONFLICT", "同一调用 ID 不能用于不同操作。")
        existing = state["operations"].get(key)
        if existing is None:
            return None
        if existing["fingerprint"] != fingerprint:
            return failure("OPERATION_CONFLICT", "同一调用 ID 不能用于不同操作。")
        return existing["result"]

    def operation(self, key: str, name: str, args: dict, mutate: Callable[[dict], Any]) -> dict:
        fingerprint = self.operation_fingerprint(name, args)

        def execute(draft: dict) -> dict:
            previous = self._operation_result(draft, key, fingerprint)
            if previous is not None:
                return previous
            before = copy.deepcopy(draft)
            try:
                result = {"ok": True, "data": copy.deepcopy(mutate(draft))}
            except Exception as error:
                draft.clear()
                draft.update(before)
                result = (
                    failure(error.code, str(error))
                    if isinstance(error, AppError)
                    else failure("TOOL_ERROR", "工具执行失败，未保存修改。")
                )
            draft["operations"][key] = {"fingerprint": fingerprint, "result": result}
            return result

        return self.transaction(execute)

    def close(self) -> None:
        with self._mutex:
            if not self._closed:
                (self.home / "instance.lock").unlink()
                self._closed = True

    def __enter__(self) -> "FileStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
