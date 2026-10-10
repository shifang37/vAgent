"""Bounded, shared Job representations for tools and local application clients."""

from vagent.video.contracts import Job, JobV2


def job_snapshot(job: Job) -> dict:
    """Keep the versioned Agent tool result free of execution and provider internals."""
    if isinstance(job, JobV2):
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


def live_job_snapshot(job: JobV2) -> dict:
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
    return {
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
            if isinstance(job, JobV2)
            else {}
        ),
    }


def cli_job_view(job: Job) -> dict:
    view = job_view(job)
    if isinstance(job, JobV2) and job.download:
        view["localMediaPath"] = job.download.relative_path
    return view
