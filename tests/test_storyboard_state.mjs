import assert from "node:assert/strict";
import test from "node:test";
import {
  assetContentPath,
  batchSummary,
  estimateQuery,
  groupJobsByShot,
  latestMediaUrl,
  nextPlayable,
  playList,
  revisionToken,
  shotJobs,
  shotState,
  shotThumbnail,
  shotVersions,
} from "../web/storyboard-state.js";

const mediaId = "22222222-2222-2222-2222-222222222222";
const assetId = "33333333-3333-3333-3333-333333333333";

const version = (extra = {}) => ({
  shotVersionId: "v-1",
  storyboardVersion: 1,
  jobId: "job-1",
  status: "queued",
  mediaAvailable: false,
  mediaUrl: null,
  ...extra,
});
const shot = (extra = {}) => ({
  id: "11111111-1111-1111-1111-111111111111",
  order: 1,
  mode: "t2v",
  durationSeconds: 5,
  referenceAssetIds: [],
  versions: [],
  latest: null,
  ...extra,
});

test("a shot without a registration is unregistered, not finished", () => {
  assert.equal(shotState(shot()).key, "none");
  assert.equal(shotState(shot()).tone, "idle");
});

test("cloud success without local media never reads as delivered", () => {
  const pending = shot({ versions: [version()], latest: version() });
  assert.equal(shotState(pending).key, "running");
  const unavailable = shot({
    versions: [version({ status: "succeeded", mediaAvailable: false })],
    latest: version({ status: "succeeded", mediaAvailable: false }),
  });
  assert.equal(shotState(unavailable).key, "unavailable");
  const delivered = shot({
    versions: [version({ status: "succeeded", mediaAvailable: true, mediaUrl: `/api/media/${mediaId}/content` })],
    latest: version({ status: "succeeded", mediaAvailable: true }),
  });
  assert.equal(shotState(delivered).key, "succeeded");
});

test("terminal failures keep their own state and tone", () => {
  for (const status of ["failed", "unknown", "download_failed"]) {
    const current = shot({ versions: [version({ status })], latest: version({ status }) });
    assert.equal(shotState(current).key, status);
    assert.ok(["failed", "attention"].includes(shotState(current).tone));
  }
});

test("versions are numbered from the registration history", () => {
  const redone = shot({
    versions: [version(), version({ shotVersionId: "v-2", storyboardVersion: 2, jobId: "job-2" })],
    latest: version({ shotVersionId: "v-2", storyboardVersion: 2, jobId: "job-2" }),
  });
  const rows = shotVersions(redone);
  assert.deepEqual(rows.map((row) => row.label), ["v1", "v2"]);
  assert.deepEqual(rows.map((row) => row.fromVersion), [1, 2]);
});

test("delivered media and shot states follow the live Jobs once they arrive", () => {
  const jobs = [
    { jobId: "job-1", status: "succeeded", mediaAvailable: true, mediaRefs: [{ mediaId }] },
    { jobId: "job-2", status: "succeeded", mediaAvailable: false, mediaRefs: [] },
    { jobId: "job-3", status: "running", mediaAvailable: false, mediaRefs: [] },
  ];
  const shots = [
    { id: "a", order: 1, versions: [{ jobId: "job-1", status: "queued", mediaAvailable: false }] },
    { id: "b", order: 2, versions: [{ jobId: "job-2", status: "queued", mediaAvailable: false }] },
    { id: "c", order: 3, versions: [{ jobId: "job-3", status: "queued", mediaAvailable: false }] },
  ];
  assert.equal(latestMediaUrl(shots[0], jobs), `/api/media/${mediaId}/content`);
  assert.equal(latestMediaUrl(shots[1], jobs), null);
  assert.equal(latestMediaUrl(shots[0]), null); // No Job list: nothing is claimed.
  assert.equal(shotState(shots[0], jobs).key, "succeeded");
  assert.equal(shotState(shots[1], jobs).key, "unavailable");
  assert.equal(shotState(shots[2], jobs).key, "running");
  assert.deepEqual(shotThumbnail(shots[0], [], jobs), {
    kind: "video",
    url: `/api/media/${mediaId}/content`,
  });
  const playlist = playList(shots, jobs);
  assert.deepEqual(playlist.items.map((item) => item.order), [1]);
  assert.deepEqual(
    playlist.missing.map((item) => item.label),
    ["镜头 2", "镜头 3"],
  );
});

test("the player uses the newest delivered media, never a pending version", () => {
  // A redo never deletes an earlier delivery: the old Job and its file stay, so the
  // shot keeps a playable frame while the new registration runs.
  const redone = shot({
    versions: [version({ mediaUrl: `/api/media/${mediaId}/content` }), version({ shotVersionId: "v-2", mediaUrl: null })],
  });
  assert.equal(latestMediaUrl(redone), `/api/media/${mediaId}/content`);
  // Availability loss is reported per version, and it removes the player.
  const lost = shot({
    versions: [version({ status: "succeeded", mediaAvailable: false, mediaUrl: null })],
  });
  assert.equal(latestMediaUrl(lost), null);
  // A delivered version keeps its identity even without an explicit URL.
  const byId = shot({ versions: [version({ mediaAvailable: true, mediaId })] });
  assert.equal(latestMediaUrl(byId), `/api/media/${mediaId}/content`);
  for (const bad of ["../state.json", "not-a-uuid", undefined]) {
    assert.equal(
      latestMediaUrl(shot({ versions: [version({ mediaAvailable: true, mediaId: bad })] })),
      null,
    );
  }
});

test("thumbnails use local media first and library images second", () => {
  assert.deepEqual(shotThumbnail(shot()), { kind: "none", url: null });
  const image = shot({ mode: "i2v", referenceAssetIds: [assetId] });
  assert.deepEqual(shotThumbnail(image, [{ assetId }]), {
    kind: "image",
    url: `/api/assets/${assetId}/content`,
    assetId,
  });
  // A foreign or non-canonical id never becomes a same-origin path.
  assert.equal(shotThumbnail(shot({ referenceAssetIds: ["../state.json"] }), []).url, null);
  assert.equal(assetContentPath("not-a-uuid"), null);
  const media = shot({
    versions: [version({ mediaUrl: `/api/media/${mediaId}/content` })],
  });
  assert.deepEqual(shotThumbnail(media, [{ assetId }]), {
    kind: "video",
    url: `/api/media/${mediaId}/content`,
  });
});

test("the confirmation summary separates new registrations from reused jobs", () => {
  const preview = {
    simulated: false,
    shots: [
      { shotId: "a", order: 1, mode: "t2v", durationSeconds: 5, reused: false, estimate: { amount: "3.00" } },
      { shotId: "b", order: 2, mode: "t2v", durationSeconds: 5, reused: true, estimate: { amount: "3.00" } },
    ],
    totalEstimate: { currency: "CNY", amount: "6.00" },
    limits: { maxRunCost: "12.00", remainingAmount: "6.00" },
  };
  const summary = batchSummary(preview);
  assert.equal(summary.registered, 1);
  assert.equal(summary.reused, 1);
  assert.equal(summary.total, "6.00");
  assert.equal(summary.limits.remainingAmount, "6.00");
  assert.deepEqual(summary.rows.map((row) => row.label), ["镜头 1", "镜头 2"]);
  assert.equal(batchSummary({ simulated: true, shots: [] }).total, null);
});

test("sequence playback keeps order and reports shots without media", () => {
  const shots = [
    shot({ id: "a", order: 1, versions: [version({ mediaUrl: `/api/media/${mediaId}/content` })] }),
    shot({ id: "b", order: 2, versions: [version({ status: "queued" })] }),
    shot({
      id: "c",
      order: 3,
      versions: [version({ status: "succeeded", mediaAvailable: true, mediaId })],
    }),
  ];
  const playlist = playList(shots);
  assert.deepEqual(playlist.items.map((item) => item.order), [1, 3]);
  assert.deepEqual(
    playlist.missing.map((item) => [item.label, item.state]),
    [["镜头 2", "running"]],
  );
  assert.equal(nextPlayable(playlist, "a").shotId, "c");
  assert.equal(nextPlayable(playlist, "c").shotId, "a");
  assert.equal(nextPlayable(playlist).shotId, "a");
  assert.equal(nextPlayable({ items: [] }, "a"), null);
});

test("jobs are grouped by the shot that registered them", () => {
  const shots = [
    { id: "a", versions: [{ jobId: "job-1" }, { jobId: "job-2" }] },
    { id: "b", versions: [{ jobId: "job-3" }] },
  ];
  const jobs = [
    { jobId: "job-1", revision: 1 },
    { jobId: "job-2", revision: 3 },
    { jobId: "job-3", revision: 2 },
    // A mock shot Job keeps the frozen v1 contract: no shotRef at all.
    { jobId: "job-4" },
    // A Job whose shot is no longer in this storyboard must stay visible.
    { jobId: "job-5", shotRef: { shotId: "gone" } },
    // A live Job's own shotRef groups it before the storyboard refresh lands.
    { jobId: "job-6", shotRef: { shotId: "b" } },
  ];
  const grouped = groupJobsByShot(jobs, shots);
  assert.deepEqual(Object.keys(grouped.byShot).sort(), ["a", "b"]);
  assert.deepEqual(grouped.byShot.a.map((job) => job.jobId), ["job-2", "job-1"]);
  assert.deepEqual(grouped.byShot.b.map((job) => job.jobId), ["job-3", "job-6"]);
  assert.deepEqual(grouped.ungrouped.map((job) => job.jobId), ["job-4", "job-5"]);
  assert.equal(groupJobsByShot(jobs).ungrouped.length, 6);
  assert.deepEqual(shotJobs(shots[0], jobs).map((job) => job.jobId), ["job-1", "job-2"]);
});

test("one selection token carries the storyboard revision into the request", () => {
  const storyboard = { storyboardId: "sb-1", currentVersion: 2, revision: 5 };
  const token = revisionToken(storyboard, ["b", "a", "b"]);
  assert.deepEqual(token, {
    storyboardId: "sb-1",
    storyboardVersion: 2,
    expectedRevision: 5,
    shotIds: ["b", "a"],
  });
  assert.equal(revisionToken(storyboard, []), null);
  assert.equal(revisionToken(null, ["a"]), null);
  const query = estimateQuery({ ...token, projectId: "coffee" });
  assert.equal(
    query,
    "/api/storyboards/sb-1/estimate?projectId=coffee&storyboardVersion=2&shotIds=b%2Ca",
  );
  assert.equal(estimateQuery(null), null);
});
