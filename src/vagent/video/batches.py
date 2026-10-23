"""M7 E2 season batches: one explicit, per-episode-isolated registration call.

A batch is not a background daemon. ``season_generate`` freezes one ordered
selection of one to three episodes of a season's current version, registers each
episode in its own Store transaction (so one bad episode cannot roll back the
others) and records exactly what happened. The Jobs, submissions, downloads and
budgets stay with the existing Job Worker and episode buckets.

The batch record is mutable progress, like a render task: members and targets are
frozen, only status, execution evidence and ``revision`` move. A terminal batch
is immutable; recovering a failed or interrupted one always creates a new batch
whose ``resumeOf`` cites the old record. Opening the Store after a crash turns
every leftover ``running`` batch into ``partial`` or ``failed`` with
``BATCH_INTERRUPTED`` members, so nothing is ever retried automatically.
"""

from typing import Literal
from uuid import UUID, uuid5

from pydantic import Field, model_validator

from vagent.contracts import Contract, Fingerprint, Identifier, JsonTuple, UtcTimestamp
from vagent.errors import AppError
from vagent.video.contracts import Currency, Money, canonical_fingerprint, parse_job
from vagent.video.seasons import Season
from vagent.video.views import delivered_media

# One batch covers at most three episodes; a whole season's 60 Jobs / 180 CNY
# ceiling never widens the per-episode 20 Jobs / 60 CNY bucket.
BATCH_EPISODE_LIMIT = 3
BATCH_JOB_LIMIT = 60
BATCH_AMOUNT_LIMIT = "180.00"
BATCH_PER_PROJECT_LIMIT = 64
BATCH_EPISODE_JOB_LIMIT = 20
BATCH_NAMESPACE = UUID("b7c2a1d4-5c3e-4f6a-8b9d-0e1f2a3b4c5d")

TERMINAL_BATCH_STATUSES = frozenset({"succeeded", "partial", "failed"})


def batch_id_for(project_id: str, operation_key: str) -> str:
    """One batch per explicit call identity, so replaying a call never duplicates it."""
    return str(uuid5(BATCH_NAMESPACE, f"vagent-season-batch:{project_id}:{operation_key}"))


class BatchEstimate(Contract):
    """The summed new-task estimate of one episode; reused Jobs are not billed twice."""

    currency: Currency
    amount: Money


class BatchEpisode(Contract):
    """One member episode: frozen identity plus its own registration outcome."""

    episode_id: Identifier
    episode_version: int = Field(ge=1)
    status: Literal["pending", "running", "succeeded", "failed"] = "pending"
    shot_count: int = Field(default=0, ge=0, le=BATCH_EPISODE_JOB_LIMIT)
    job_ids: JsonTuple[Identifier] = Field(default_factory=tuple, max_length=BATCH_EPISODE_JOB_LIMIT)
    reused_count: int = Field(default=0, ge=0)
    estimate: BatchEstimate | None = None
    error_code: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]*$")
    started_at: UtcTimestamp | None = None
    finished_at: UtcTimestamp | None = None

    @model_validator(mode="after")
    def coherent_outcome(self):
        if self.status == "failed" and self.error_code is None:
            raise ValueError("A failed batch episode must name its error code")
        if self.status != "failed" and self.error_code is not None:
            raise ValueError("Only a failed batch episode carries an error code")
        terminal = self.status in {"succeeded", "failed"}
        if terminal != (self.finished_at is not None):
            raise ValueError("finishedAt is required exactly for a terminal episode")
        if self.started_at is None and self.status != "pending":
            raise ValueError("Only a pending episode has no startedAt")
        if self.status == "succeeded" and not self.job_ids:
            raise ValueError("A succeeded episode must cite the Jobs it registered")
        if len(set(self.job_ids)) != len(self.job_ids):
            raise ValueError("A batch episode cannot cite the same Job twice")
        return self


def batch_status(episodes) -> str:
    """Derive the batch status from its members; never store an independent fact."""
    statuses = [member.status for member in episodes]
    if any(status in {"pending", "running"} for status in statuses):
        return "running"
    if all(status == "succeeded" for status in statuses):
        return "succeeded"
    if any(status == "succeeded" for status in statuses):
        return "partial"
    return "failed"


class Batch(Contract):
    """One explicit season generation attempt over one to three episodes."""

    id: Identifier
    project_id: Identifier
    season_id: Identifier
    season_version: int = Field(ge=1)
    episode_ids: JsonTuple[Identifier] = Field(min_length=1, max_length=BATCH_EPISODE_LIMIT)
    status: Literal["running", "succeeded", "partial", "failed"]
    episodes: JsonTuple[BatchEpisode] = Field(min_length=1, max_length=BATCH_EPISODE_LIMIT)
    resume_of: Identifier | None = None
    fingerprint: Fingerprint
    created_at: UtcTimestamp
    updated_at: UtcTimestamp
    revision: int = Field(ge=0)

    @model_validator(mode="after")
    def coherent_members(self):
        if len(set(self.episode_ids)) != len(self.episode_ids):
            raise ValueError("A batch cannot list the same episode twice")
        if tuple(self.episode_ids) != tuple(member.episode_id for member in self.episodes):
            raise ValueError("Batch members must follow episodeIds exactly")
        if self.status != batch_status(self.episodes):
            raise ValueError("Batch status must be derived from its members")
        if self.updated_at < self.created_at:
            raise ValueError("updatedAt cannot precede createdAt")
        if self.resume_of == self.id:
            raise ValueError("A batch cannot resume itself")
        return self


def batch_fingerprint(
    project_id: str,
    season_id: str,
    season_version: int,
    episode_versions,
    mode: str,
    selection,
) -> str:
    """The canonical identity of one batch request: no time, call or resume ID."""
    return canonical_fingerprint(
        {
            "projectId": project_id,
            "seasonId": season_id,
            "seasonVersion": season_version,
            "episodes": [
                {"episodeId": episode_id, "episodeVersion": version}
                for episode_id, version in episode_versions
            ],
            "mode": mode,
            "selection": list(selection),
        }
    )


def new_batch(
    *,
    batch_id: str,
    project_id: str,
    season_id: str,
    season_version: int,
    members,
    fingerprint: str,
    resume_of: str | None,
    timestamp: str,
) -> Batch:
    """Freeze one batch in its initial ``running`` state; registration follows."""
    return Batch(
        id=batch_id,
        project_id=project_id,
        season_id=season_id,
        season_version=season_version,
        episode_ids=tuple(member.episode_id for member in members),
        status="running",
        episodes=tuple(members),
        resume_of=resume_of,
        fingerprint=fingerprint,
        created_at=timestamp,
        updated_at=timestamp,
        revision=0,
    )


def _advance(record: Batch, members, timestamp: str) -> Batch:
    """The only way a batch progresses: members change first, status is derived."""
    return record.model_copy(
        update={
            "episodes": tuple(members),
            "status": batch_status(members),
            "updated_at": timestamp,
            "revision": record.revision + 1,
        }
    )


def mark_running(record: Batch, episode_id: str, timestamp: str) -> Batch:
    members = [
        member.model_copy(update={"status": "running", "started_at": timestamp})
        if member.episode_id == episode_id
        else member
        for member in record.episodes
    ]
    return _advance(record, members, timestamp)


def complete_member(
    record: Batch,
    episode_id: str,
    *,
    shot_count: int,
    job_ids,
    reused_count: int,
    estimate: BatchEstimate | None,
    timestamp: str,
) -> Batch:
    members = []
    for member in record.episodes:
        if member.episode_id != episode_id:
            members.append(member)
            continue
        members.append(
            member.model_copy(
                update={
                    "status": "succeeded",
                    "shot_count": shot_count,
                    "job_ids": tuple(job_ids),
                    "reused_count": reused_count,
                    "estimate": estimate,
                    "error_code": None,
                    "started_at": member.started_at or timestamp,
                    "finished_at": timestamp,
                }
            )
        )
    return _advance(record, members, timestamp)


def fail_member(record: Batch, episode_id: str, error_code: str, timestamp: str) -> Batch:
    members = []
    for member in record.episodes:
        if member.episode_id != episode_id:
            members.append(member)
            continue
        members.append(
            member.model_copy(
                update={
                    "status": "failed",
                    "error_code": error_code,
                    "started_at": member.started_at or timestamp,
                    "finished_at": timestamp,
                }
            )
        )
    return _advance(record, members, timestamp)


def _delivery_status(job) -> str:
    """One registered Job's delivery state, from the Job and its local media only."""
    if job is None:
        return "failed"
    _, available = delivered_media(job)
    if available:
        return "delivered"
    if (
        job.status in {"failed", "unknown", "download_failed"}
        or job.query_state == "paused"
        or getattr(job, "runtime_block", None) is not None
    ):
        return "failed"
    return "pending"


def batch_view(state: dict, batch: Batch) -> dict:
    """The one batch projection shared by ``season_read``, the CLI and the Web panel."""
    jobs = {job_id: parse_job(raw) for job_id, raw in state["jobs"].items()}
    episodes = []
    total_amount = 0
    currency = None
    new_jobs = 0
    for member in batch.episodes:
        delivered = failed = pending = 0
        for job_id in member.job_ids:
            status = _delivery_status(jobs.get(job_id))
            if status == "delivered":
                delivered += 1
            elif status == "failed":
                failed += 1
            else:
                pending += 1
        episode = state["episodes"].get(member.episode_id) or {}
        episodes.append(
            {
                "episodeId": member.episode_id,
                "episodeVersion": member.episode_version,
                "status": member.status,
                "shotCount": member.shot_count,
                "jobIds": list(member.job_ids),
                "jobCount": len(member.job_ids),
                "reusedCount": member.reused_count,
                "estimate": member.estimate.model_dump(mode="json", by_alias=True)
                if member.estimate
                else None,
                "errorCode": member.error_code,
                "startedAt": member.started_at,
                "finishedAt": member.finished_at,
                "deliveredShots": delivered,
                "failedShots": failed,
                "pendingShots": pending,
                "title": episode.get("title"),
                "currentVersion": episode.get("currentVersion"),
                "stale": member.episode_version != episode.get("currentVersion"),
            }
        )
        new_jobs += max(0, member.shot_count - member.reused_count)
        if member.estimate is not None:
            currency = currency or member.estimate.currency
            if member.estimate.currency == currency:
                total_amount += int(member.estimate.amount.replace(".", ""))
    total = None
    if currency is not None:
        total = {"currency": currency, "amount": format_amount(total_amount)}
    return {
        "batchId": batch.id,
        "seasonId": batch.season_id,
        "seasonVersion": batch.season_version,
        "status": batch.status,
        "resumeOf": batch.resume_of,
        "fingerprint": batch.fingerprint,
        "revision": batch.revision,
        "createdAt": batch.created_at,
        "updatedAt": batch.updated_at,
        "episodeIds": list(batch.episode_ids),
        "episodes": episodes,
        "newJobCount": new_jobs,
        "totalEstimate": total,
    }


def format_amount(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


def _project_batches(state: dict, project_id: str) -> list[Batch]:
    return [Batch.model_validate(raw) for raw in state["batches"].values() if raw["projectId"] == project_id]


def batch_summaries(state: dict, project_id: str, season_id: str | None = None) -> list[dict]:
    """Newest-first batch summaries of one project (optionally one season)."""
    records = _project_batches(state, project_id)
    if season_id is not None:
        records = [record for record in records if record.season_id == season_id]
    records.sort(key=lambda record: (record.created_at, record.id), reverse=True)
    return [
        {
            "batchId": record.id,
            "seasonId": record.season_id,
            "seasonVersion": record.season_version,
            "status": record.status,
            "resumeOf": record.resume_of,
            "revision": record.revision,
            "episodeIds": list(record.episode_ids),
            "createdAt": record.created_at,
            "updatedAt": record.updated_at,
        }
        for record in records
    ]


def read_batch(args: dict, state: dict, project_id: str) -> dict | None:
    """The latest batch of one season, or exactly the requested batch.

    ``None`` means this project/season has no batch yet. An explicitly requested
    unknown or cross-project batch is a ``BATCH_CONFLICT``, never an empty view.
    """
    batch_id = args.get("batchId")
    season_id = args.get("seasonId")
    if batch_id is not None:
        raw = state["batches"].get(batch_id)
        if raw is None or raw["projectId"] != project_id:
            raise AppError("BATCH_CONFLICT", "当前项目中没有这个批次。")
        record = Batch.model_validate(raw)
        if season_id is not None and record.season_id != season_id:
            raise AppError("BATCH_CONFLICT", "这个批次不属于指定的季。")
        return batch_view(state, record)
    if season_id is None:
        return None
    records = [record for record in _project_batches(state, project_id) if record.season_id == season_id]
    if not records:
        return None
    record = max(records, key=lambda item: (item.created_at, item.id))
    return batch_view(state, record)


def running_batch(state: dict, project_id: str, season_id: str) -> Batch | None:
    """The one unfinished batch of a season, if any; concurrent batches are refused."""
    for record in _project_batches(state, project_id):
        if record.season_id == season_id and record.status == "running":
            return record
    return None


def resume_target(state: dict, project_id: str, batch_id: str) -> Batch:
    raw = state["batches"].get(batch_id)
    if raw is None or raw["projectId"] != project_id:
        raise AppError("BATCH_CONFLICT", "当前项目中没有这个批次，未登记任何任务。")
    record = Batch.model_validate(raw)
    if record.status == "running":
        raise AppError("BATCH_CONFLICT", "这个批次仍在进行中，不能恢复。")
    if not any(member.status != "succeeded" for member in record.episodes):
        raise AppError("BATCH_CONFLICT", "这个批次没有可恢复的失败集。")
    return record


def recover_interrupted_batches(state: dict, timestamp: str) -> None:
    """Only the Store opener calls this: leftover running batches become terminal.

    Members that never reached a terminal state are marked ``BATCH_INTERRUPTED``;
    the old batch is never rewritten or resumed automatically.
    """
    for raw in state["batches"].values():
        if raw.get("status") != "running":
            continue
        record = Batch.model_validate(raw)
        succeeded = any(member.status == "succeeded" for member in record.episodes)
        episodes = []
        for member in record.episodes:
            if member.status in {"pending", "running"}:
                episodes.append(
                    member.model_copy(
                        update={
                            "status": "failed",
                            "error_code": "BATCH_INTERRUPTED",
                            "started_at": member.started_at or timestamp,
                            "finished_at": timestamp,
                        }
                    )
                )
            else:
                episodes.append(member)
        status = "partial" if succeeded else "failed"
        recovered = record.model_copy(
            update={
                "status": status,
                "episodes": tuple(episodes),
                "updated_at": timestamp,
                "revision": record.revision + 1,
            }
        )
        state["batches"][record.id] = recovered.model_dump(mode="json", by_alias=True)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AppError("BATCH_CONFLICT", message)


def validate_batch_changes(previous: dict[str, dict], state: dict, *, initial: bool = False) -> None:
    """Every Store commit proves frozen targets, append-only history and revisions.

    ``initial`` is the Store-open pass: it validates identities, membership and Job
    references, but not the creation or per-commit transition rules, which compare
    against the record's own previous revision.
    """
    batches = state["batches"]
    _require(not (previous.keys() - batches.keys()), "已保存的批次不能删除。")
    per_project: dict[str, int] = {}
    for key, raw in batches.items():
        try:
            record = Batch.model_validate(raw)
        except ValueError:
            _require(False, "批次记录不完整或成员状态与顶层状态不一致。")
        _require(record.id == key, "批次 ID 不匹配。")
        _require(record.project_id in state["projects"], "批次所属项目不存在。")
        per_project[record.project_id] = per_project.get(record.project_id, 0) + 1
        _require(
            per_project[record.project_id] <= BATCH_PER_PROJECT_LIMIT,
            f"一个项目最多 {BATCH_PER_PROJECT_LIMIT} 个批次。",
        )
        season_raw = state["seasons"].get(record.season_id)
        _require(
            season_raw is not None and season_raw["projectId"] == record.project_id,
            "批次引用的季不存在或不属于同一项目。",
        )
        season = Season.model_validate(season_raw)
        version = next((item for item in season.versions if item.version == record.season_version), None)
        _require(version is not None, "批次引用的季版本不存在。")
        _require(
            all(episode_id in version.episode_ids for episode_id in record.episode_ids),
            "批次成员必须属于所引用的季版本。",
        )
        for episode_id in record.episode_ids:
            episode = state["episodes"].get(episode_id)
            _require(
                episode is not None and episode["projectId"] == record.project_id,
                "批次成员必须是同一项目已保存的分集。",
            )
        seen_jobs: set[str] = set()
        for member in record.episodes:
            for job_id in member.job_ids:
                _require(job_id not in seen_jobs, "同一批次不能重复引用同一个任务。")
                seen_jobs.add(job_id)
                job = state["jobs"].get(job_id)
                _require(
                    job is not None and job["context"]["projectId"] == record.project_id,
                    "批次引用的任务不存在或跨项目。",
                )
        if record.resume_of is not None:
            target = batches.get(record.resume_of)
            _require(
                target is not None
                and target["projectId"] == record.project_id
                and target["status"] != "running",
                "恢复批次必须指向同一项目已结束的旧批次。",
            )
        old_raw = previous.get(key)
        if initial:
            continue
        if old_raw is None:
            _require(
                record.revision == 0 and record.status == "running",
                "新批次必须以 running、revision 0 登记。",
            )
            continue
        old = Batch.model_validate(old_raw)
        if record == old:
            # A transaction that does not touch the batch (a Job poll, an episode
            # save, a render commit) must not be read as a batch advance.
            continue
        if old.status in TERMINAL_BATCH_STATUSES:
            _require(record == old, "终态批次不可改写；恢复必须创建新批次。")
            continue
        _require(
            record.project_id == old.project_id
            and record.season_id == old.season_id
            and record.season_version == old.season_version
            and record.episode_ids == old.episode_ids
            and record.resume_of == old.resume_of
            and record.fingerprint == old.fingerprint
            and record.created_at == old.created_at,
            "批次的目标、成员与指纹在创建后不可修改。",
        )
        _require(
            record.revision == old.revision + 1,
            "批次每次推进必须且只能增加一个 revision。",
        )
        _require(record.updated_at >= old.updated_at, "批次的更新时间不能回退。")
        for before, after in zip(old.episodes, record.episodes, strict=True):
            if after == before:
                continue
            _require(
                before.status not in {"succeeded", "failed"},
                "批次成员的终态不可改写。",
            )
            allowed = {"pending": {"running", "failed"}, "running": {"succeeded", "failed"}}
            _require(
                after.status in allowed.get(before.status, set()),
                "批次成员状态只能从 pending 到 running，再到 succeeded/failed。",
            )
            _require(
                after.started_at is not None or after.status == "failed",
                "批次成员开始后必须有 startedAt。",
            )
            if before.job_ids:
                _require(
                    after.job_ids == before.job_ids,
                    "批次成员已登记的任务列表不可修改。",
                )
