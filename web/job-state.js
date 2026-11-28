// A Job can outlive its creating Run, and a second session bound to the same
// project sees its project's Jobs too. Scope filtering uses the project id when
// supplied and keeps the historical sessionId-equals-projectId check otherwise.
export function upsertJob(jobs, job, sessionId, projectId = null) {
  const scopeMatch = projectId
    ? job?.projectId === projectId
    : job?.sessionId === sessionId && job?.projectId === sessionId;
  if (!job || !scopeMatch ||
      typeof job.jobId !== "string" || !Number.isInteger(job.revision) || job.revision < 0) return jobs;
  const previous = jobs.find((item) => item.jobId === job.jobId);
  if (previous && previous.revision >= job.revision) return jobs;
  return previous
    ? jobs.map((item) => item.jobId === job.jobId ? job : item)
    : [job, ...jobs];
}

export function mergeJobs(previous, incoming, sessionId, projectId = null) {
  let merged = [];
  for (const job of [...incoming].reverse()) merged = upsertJob(merged, job, sessionId, projectId);
  for (const job of previous) merged = upsertJob(merged, job, sessionId, projectId);
  return merged;
}

export function mediaContentPath(job) {
  if (job.mode !== "live" || job.status !== "succeeded" || job.mediaAvailable !== true ||
      job.mediaAvailability?.status !== "available" || job.mediaRefs?.length !== 1) return null;
  const id = job.mediaRefs[0].mediaId;
  if (typeof id !== "string" || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(id)) return null;
  return `/api/media/${id}/content`;
}
