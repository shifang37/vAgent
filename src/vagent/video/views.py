"""Bounded, shared representations for tools and local application clients.

Job views keep execution and provider internals out of tool results; the M2
storyboard view is the one shape the CLI, the Web panel and the storyboard tools
share, so ordering, version history and delivered media agree across all three.
"""

from vagent.video.contracts import Job, JobV3, LiveJob, parse_job


def job_snapshot(job: Job) -> dict:
    """Keep the versioned Agent tool result free of execution and provider internals."""
    if isinstance(job, LiveJob):
        return live_job_snapshot(job)
    return {
        "jobId": job.id,
        "revision": job.revision,
        "mode": job.mode,
        "simulated": True,
        "mediaAvailable": False,
        "status": job.status,
        "queryState": job.query_state,
        "request": job.request.model_dump(mode="json", by_alias=True),
        "submitAttempts": job.submit_attempts,
        "queryAttempts": job.query_attempts,
        "createdAt": job.created_at,
        "updatedAt": job.updated_at,
        "nextPollAt": job.next_poll_at,
        "error": job.error.model_dump(mode="json", by_alias=True) if job.error else None,
        "result": job.result.model_dump(mode="json", by_alias=True) if job.result else None,
    }


def live_job_snapshot(job: Job) -> dict:
    download = None
    if job.download:
        raw = job.download.model_dump(mode="json", by_alias=True)
        download = {
            key: raw[key]
            for key in (
                "mediaId",
                "phase",
                "generation",
                "attempts",
                "windowAttempts",
                "nextAttemptAt",
                "startedAt",
                "deadlineAt",
                "repair",
            )
        }
        download["error"] = job.download.error.public() if job.download.error else None
    view = {
        "jobId": job.id,
        "revision": job.revision,
        "mode": "live",
        "simulated": False,
        "mediaAvailable": job.media_availability.status == "available",
        "status": job.status,
        "lastProviderStatus": job.last_provider_status,
        "queryState": job.query_state,
        "queryPauseReason": job.query_pause_reason,
        "request": {
            **job.request.public_arguments(),
            "region": job.request.region,
            "parameters": job.request.parameters.model_dump(mode="json", by_alias=True),
        },
        "submitAttempts": job.submit_attempts,
        "queryAttempts": job.query_attempts,
        "createdAt": job.created_at,
        "updatedAt": job.updated_at,
        "nextPollAt": job.next_poll_at,
        "cost": job.cost.model_dump(mode="json", by_alias=True),
        "download": download,
        "mediaAvailability": job.media_availability.model_dump(mode="json", by_alias=True),
        "mediaRefs": [ref.model_dump(mode="json", by_alias=True) for ref in job.result.media_refs]
        if job.result
        else [],
        "runtimeBlock": job.runtime_block.model_dump(mode="json", by_alias=True)
        if job.runtime_block
        else None,
        "error": job.error.public() if job.error else None,
        "result": job.result.model_dump(mode="json", by_alias=True) if job.result else None,
    }
    if isinstance(job, JobV3):
        view["shotRef"] = job.shot_ref.model_dump(mode="json", by_alias=True)
    return view


def job_view(job: Job) -> dict:
    """CLI/Web provenance belongs to the Job, never to the currently active Run."""
    return {
        **job_snapshot(job),
        "projectId": job.context.project_id,
        "sessionId": job.context.session_id,
        "runId": job.context.run_id,
        "providerTaskId": job.provider_task_id,
        "canRetryQuery": job.query_state == "paused" and job.provider_task_id is not None,
        **(
            {
                "canRetryDownload": job.status == "download_failed"
                or (
                    job.status == "succeeded"
                    and job.media_availability.status == "unavailable"
                    and job.download.phase in {"failed", "committed"}
                )
            }
            if isinstance(job, LiveJob)
            else {}
        ),
    }


def cli_job_view(job: Job) -> dict:
    view = job_view(job)
    if isinstance(job, LiveJob) and job.download:
        view["localMediaPath"] = job.download.relative_path
    return view


def delivered_media(job: Job | None) -> tuple[str | None, bool]:
    """A shot's local MP4 lives on its Job; M2 never writes ShotVersion.mediaId."""
    if job is None or not isinstance(job, LiveJob) or job.status != "succeeded" or job.result is None:
        return None, False
    return job.result.media_refs[0].media_id, job.media_availability.status == "available"


def storyboard_summary(record) -> dict:
    """Identity and version facts of one storyboard, without its shot snapshots."""
    return {
        "storyboardId": record.id,
        "projectId": record.project_id,
        "title": record.title,
        "currentVersion": record.current_version,
        "versionCount": len(record.versions),
        "shotCount": len(record.versions[-1].shots),
        "shotVersionCount": len(record.shot_versions),
        "revision": record.revision,
        "createdAt": record.created_at,
    }


def storyboard_view(state: dict, record) -> dict:
    """Ordered shots with their registration history and delivered local media.

    The Web panel, the CLI and the storyboard tools all read this one shape, so
    version history, per-shot status and media links cannot drift between them.
    """
    jobs = {job_id: parse_job(raw) for job_id, raw in state["jobs"].items()}
    by_shot: dict[str, list] = {}
    for item in record.shot_versions:
        by_shot.setdefault(item.shot_id, []).append(item)
    shots = []
    for shot in sorted(record.versions[-1].shots, key=lambda item: item.order):
        versions = []
        for item in by_shot.get(shot.id, []):
            job = jobs.get(item.job_id)
            media_id, available = delivered_media(job)
            versions.append(
                {
                    "shotVersionId": item.id,
                    "storyboardVersion": item.storyboard_version,
                    "current": item.storyboard_version == record.current_version,
                    "jobId": item.job_id,
                    "mediaId": item.media_id or media_id,
                    "mediaAvailable": available,
                    "mediaUrl": f"/api/media/{media_id}/content" if available else None,
                    "estimate": item.estimate.model_dump(mode="json", by_alias=True)
                    if item.estimate
                    else None,
                    "status": job.status if job is not None else None,
                    "simulated": job is not None and job.mode == "mock",
                    "createdAt": item.created_at,
                }
            )
        shots.append(
            {
                **shot.model_dump(mode="json", by_alias=True),
                "versions": versions,
                "latest": versions[-1] if versions else None,
            }
        )
    return {
        **storyboard_summary(record),
        "sourceRefs": [reference.model_dump(mode="json", by_alias=True) for reference in record.source_refs],
        "shots": shots,
    }


def storyboard_summaries(state: dict, project_id: str) -> list[dict]:
    """Newest-first summaries of one project's storyboards."""
    from vagent.video.storyboards import Storyboard

    records = [
        Storyboard.model_validate(raw)
        for raw in state["storyboards"].values()
        if raw["projectId"] == project_id
    ]
    records.sort(key=lambda record: (record.created_at, record.id), reverse=True)
    return [storyboard_summary(record) for record in records]


def current_storyboard_view(state: dict, project_id: str) -> dict | None:
    """The project's most recent storyboard, or None when it has none yet."""
    summaries = storyboard_summaries(state, project_id)
    if not summaries:
        return None
    from vagent.video.storyboards import Storyboard

    return storyboard_view(
        state, Storyboard.model_validate(state["storyboards"][summaries[0]["storyboardId"]])
    )
