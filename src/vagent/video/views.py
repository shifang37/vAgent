"""Bounded, shared Job representations for tools and local application clients."""

from vagent.video.contracts import Job


def job_snapshot(job: Job) -> dict:
    """Keep the versioned Agent tool result free of execution and provider internals."""
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


def job_view(job: Job) -> dict:
    """CLI/Web provenance belongs to the Job, never to the currently active Run."""
    return {
        **job_snapshot(job),
        "projectId": job.context.project_id,
        "sessionId": job.context.session_id,
        "runId": job.context.run_id,
        "providerTaskId": job.provider_task_id,
        "canRetryQuery": job.query_state == "paused" and job.provider_task_id is not None,
    }
