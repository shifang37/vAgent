"""Atomic, single-writer JSON storage; domain writes and operation results commit together."""

import copy
import hashlib
import json
import os
import threading
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal, TypeVar
from uuid import uuid4

from langchain_core.messages import messages_from_dict
from pydantic import BaseModel, ConfigDict, Field, JsonValue
from pydantic.alias_generators import to_camel

from vagent.config import assert_id
from vagent.errors import AppError, failure
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


def now() -> str:
    return datetime.now(UTC).isoformat()


def atomic_write_bytes(path: Path, content: bytes) -> None:
    """Replace one local file only after its complete contents have been flushed."""
    temporary = path.with_name(f"{path.stem}-{uuid4()}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
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


class FileStore:
    def __init__(self, home: Path, state: dict):
        self.home = home
        self._state = state
        self._mutex = threading.RLock()
        self._closed = False

    @classmethod
    def open(cls, home: Path | str) -> "FileStore":
        home = Path(home).resolve()
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = home / "instance.lock"
        try:
            handle = lock.open("x", encoding="utf-8")
        except FileExistsError:
            raise AppError(
                "STORE_LOCKED", "数据目录已被锁定。确认没有 vagent 进程后，再手动移除 instance.lock。"
            ) from None
        try:
            with handle:
                json.dump({"pid": os.getpid(), "createdAt": now()}, handle)
            try:
                raw = (home / "state.json").read_bytes()
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
                recover_interrupted_jobs(draft, now())
                recover_interrupted_batches(draft, now())

            store.transaction(recover)
            from vagent.video.films import cleanup_stale_parts

            cleanup_stale_parts(store.home, store.snapshot())
            return store
        except BaseException:
            lock.unlink(missing_ok=True)
            raise

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

    def transaction(self, mutate: Callable[[dict], T]) -> T:
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
            return copy.deepcopy(value)

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
