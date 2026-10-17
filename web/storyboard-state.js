// Pure storyboard projections for the workbench panel: no DOM and no network, so
// tests/test_storyboard_state.mjs can drive every rule the panel relies on:
// per-shot state, version history, thumbnails, sequence playback and Job grouping.

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const STATE_LABELS = {
  none: "未登记",
  running: "生成中",
  succeeded: "已完成",
  unavailable: "媒体不可用",
  failed: "任务失败",
  unknown: "提交待核实",
  download_failed: "下载失败",
};
const TONES = {
  none: "idle",
  running: "running",
  succeeded: "succeeded",
  unavailable: "attention",
  failed: "failed",
  unknown: "attention",
  download_failed: "failed",
};

export function assetContentPath(asset) {
  const id = typeof asset === "string" ? asset : asset?.assetId;
  return typeof id === "string" && UUID.test(id) ? `/api/assets/${id}/content` : null;
}

export function shotState(shot, jobs = []) {
  const versions = shot?.versions || [];
  const latest = shot?.latest || versions[versions.length - 1] || null;
  if (!latest || !latest.jobId) return { key: "none", label: STATE_LABELS.none, tone: TONES.none };
  const job = (jobs || []).find((item) => item?.jobId === latest.jobId);
  const status = job ? job.status : latest.status;
  if (!status) return { key: "none", label: STATE_LABELS.none, tone: TONES.none };
  const available = job ? job.mediaAvailable === true : latest.mediaAvailable === true;
  const key =
    status === "succeeded"
      ? available
        ? "succeeded"
        : "unavailable"
      : ["failed", "unknown", "download_failed"].includes(status)
        ? status
        : "running";
  return { key, label: STATE_LABELS[key], tone: TONES[key], status, jobId: latest.jobId };
}

export function shotVersions(shot) {
  return (shot?.versions || []).map((version, index) => ({
    ...version,
    index: index + 1,
    label: `v${index + 1}`,
    fromVersion: version.storyboardVersion,
  }));
}

const mediaContentPath = (mediaId) =>
  typeof mediaId === "string" && UUID.test(mediaId) ? `/api/media/${mediaId}/content` : null;

// The storyboard view is fetched per session while Jobs stream in live, so a
// delivery is read from the Job when the storyboard snapshot has not caught up.
export function jobMediaUrl(jobs, jobId) {
  const job = (jobs || []).find((item) => item?.jobId === jobId);
  if (!job || job.mediaAvailable !== true) return null;
  return mediaContentPath(job.mediaRefs?.[0]?.mediaId);
}

export function latestMediaUrl(shot, jobs = []) {
  const versions = shot?.versions || [];
  for (let index = versions.length - 1; index >= 0; index -= 1) {
    const version = versions[index] || {};
    if (typeof version.mediaUrl === "string" && version.mediaUrl.startsWith("/api/media/")) {
      return version.mediaUrl;
    }
    // A delivered version always carries both; the ID stays the fallback so a
    // server that omits the URL cannot hide local media from the player.
    const fallback = version.mediaAvailable ? mediaContentPath(version.mediaId) : null;
    if (fallback) return fallback;
    const fromJob = jobMediaUrl(jobs, version.jobId);
    if (fromJob) return fromJob;
  }
  return null;
}

// A shot thumbnail prefers its own delivered frame; image-input shots fall back to
// the reference image that is actually in the library.
export function shotThumbnail(shot, assets = [], jobs = []) {
  const media = latestMediaUrl(shot, jobs);
  if (media) return { kind: "video", url: media };
  const known = new Map((assets || []).map((asset) => [asset.assetId, asset]));
  for (const assetId of shot?.referenceAssetIds || []) {
    const asset = known.get(assetId);
    const url = asset?.available === false ? null : assetContentPath(assetId);
    if (asset && url) return { kind: "image", url, assetId };
  }
  return { kind: "none", url: null };
}

export function batchRows(preview) {
  const shots = preview?.shots || [];
  return shots.map((shot) => ({
    shotId: shot.shotId,
    order: shot.order,
    label: `镜头 ${shot.order}`,
    mode: shot.mode,
    durationSeconds: shot.durationSeconds,
    reused: Boolean(shot.reused),
    amount: shot.estimate?.amount ?? null,
  }));
}

export function batchSummary(preview) {
  const rows = batchRows(preview);
  const reused = rows.filter((row) => row.reused).length;
  return {
    rows,
    registered: rows.filter((row) => !row.reused).length,
    reused,
    total: preview?.totalEstimate?.amount ?? null,
    currency: preview?.totalEstimate?.currency ?? "CNY",
    limits: preview?.limits ?? null,
    simulated: Boolean(preview?.simulated),
  };
}

// Client-side sequence playback: ordered playable shots plus the ones with no
// local media yet, which the player reports instead of silently skipping.
export function playList(shots, jobs = []) {
  const items = [];
  const missing = [];
  for (const shot of shots || []) {
    const url = latestMediaUrl(shot, jobs);
    const state = shotState(shot, jobs);
    if (url) items.push({ shotId: shot.id, order: shot.order, url, label: `镜头 ${shot.order}` });
    else missing.push({ shotId: shot.id, order: shot.order, label: `镜头 ${shot.order}`, state: state.key });
  }
  return { items, missing };
}

export function nextPlayable(playlist, shotId) {
  const items = playlist?.items || [];
  if (!items.length) return null;
  if (!shotId) return items[0];
  const index = items.findIndex((item) => item.shotId === shotId);
  if (index === -1) return items[0];
  return items[(index + 1) % items.length];
}

// Jobs stay grouped by the shot that registered them. The storyboard version list
// owns that link for every mode (a mock Job keeps the frozen v1 contract and has no
// shotRef), while a live Job's own shotRef groups it even before a refresh. A Job
// whose shot is not part of this storyboard stays ungrouped so it stays visible.
export function groupJobsByShot(jobs, shots = []) {
  const known = new Set((shots || []).map((item) => item?.id).filter(Boolean));
  const owner = new Map();
  for (const shot of shots || []) {
    for (const version of shot?.versions || []) owner.set(version.jobId, shot.id);
  }
  const byShot = {};
  const ungrouped = [];
  for (const job of jobs || []) {
    const hinted = job?.shotRef?.shotId;
    const shotId = owner.get(job?.jobId) || (known.has(hinted) ? hinted : null);
    if (shotId) (byShot[shotId] ||= []).push(job);
    else ungrouped.push(job);
  }
  for (const list of Object.values(byShot)) list.sort((a, b) => (b.revision || 0) - (a.revision || 0));
  return { byShot, ungrouped };
}

export function shotJobs(shot, jobs) {
  const ids = new Set((shot?.versions || []).map((version) => version.jobId));
  return (jobs || []).filter((job) => ids.has(job?.jobId));
}

// One selection model for the panel: the dialog, the estimate request and the
// registration all read the same token, so a changed storyboard cannot be sent.
export function revisionToken(storyboard, shotIds) {
  if (!storyboard?.storyboardId || !storyboard.currentVersion) return null;
  const ids = [...new Set(shotIds || [])];
  if (!ids.length) return null;
  return {
    storyboardId: storyboard.storyboardId,
    storyboardVersion: storyboard.currentVersion,
    expectedRevision: storyboard.revision,
    shotIds: ids,
  };
}

export function estimateQuery(token) {
  if (!token) return null;
  return (
    `/api/storyboards/${encodeURIComponent(token.storyboardId)}/estimate` +
    `?projectId=${encodeURIComponent(token.projectId || "")}` +
    `&storyboardVersion=${token.storyboardVersion}` +
    `&shotIds=${encodeURIComponent(token.shotIds.join(","))}`
  );
}
