import assert from "node:assert/strict";
import test from "node:test";
import { mergeJobs, upsertJob } from "../web/job-state.js";

const job = (revision, status = "running", extra = {}) => ({
  jobId: "job-1", sessionId: "coffee", projectId: "coffee", runId: "original-run", revision, status, ...extra,
});

test("a completed creator does not prevent newer Job updates", () => {
  const next = upsertJob([job(2)], job(5, "succeeded"), "coffee");
  assert.equal(next[0].status, "succeeded");
  assert.equal(next[0].runId, "original-run");
});
test("duplicate and out of order Job events cannot roll back a final result", () => {
  const current = [job(5, "succeeded")];
  for (const revision of [5, 1, 4, 0]) assert.equal(upsertJob(current, job(revision), "coffee"), current);
});
test("events from another session or project are ignored", () => {
  const current = [job(2)];
  for (const extra of [{ sessionId: "other" }, { projectId: "other" }]) {
    assert.equal(upsertJob(current, job(3, "succeeded", extra), "coffee"), current);
  }
});
test("reconnect and overflow snapshots merge new jobs while preserving newer revisions", () => {
  const current = [job(5, "succeeded")];
  const incoming = [job(3), job(1, "queued", { jobId: "job-2" })];
  const merged = mergeJobs(current, incoming, "coffee");
  assert.equal(merged.length, 2);
  assert.equal(merged.find((item) => item.jobId === "job-1").status, "succeeded");
  assert.equal(merged.find((item) => item.jobId === "job-2").revision, 1);
  assert.equal(mergeJobs(merged, [job(6, "succeeded")], "coffee").find((item) => item.jobId === "job-1").revision, 6);
});
test("a query retry response arriving after a newer SSE update cannot roll it back", () => {
  const current = [job(10, "succeeded")];
  assert.equal(upsertJob(current, job(8, "running", { queryState: "polling" }), "coffee"), current);
});
test("switching sessions cannot retain the old project's jobs", () => {
  assert.deepEqual(mergeJobs([job(5)], [job(1)], "other"), []);
});
test("invalid revisions do not replace known state", () => {
  const current = [job(2)];
  for (const revision of [-1, "3", 1.5, undefined]) assert.equal(upsertJob(current, job(revision), "coffee"), current);
});
