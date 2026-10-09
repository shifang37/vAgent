// A Job can outlive its creating Run. Only its own ID, revision and session
// determine whether an incoming view is newer than what the client has seen.
export function upsertJob(jobs, job, sessionId) {
  if (!job || job.sessionId !== sessionId || job.projectId !== sessionId ||
      typeof job.jobId !== "string" || !Number.isInteger(job.revision) || job.revision < 0) return jobs;
  const previous = jobs.find((item) => item.jobId === job.jobId);
  if (previous && previous.revision >= job.revision) return jobs;
  return previous
    ? jobs.map((item) => item.jobId === job.jobId ? job : item)
    : [job, ...jobs];
}

export function mergeJobs(previous, incoming, sessionId) {
  let merged = [];
  for (const job of [...incoming].reverse()) merged = upsertJob(merged, job, sessionId);
  for (const job of previous) merged = upsertJob(merged, job, sessionId);
  return merged;
}
