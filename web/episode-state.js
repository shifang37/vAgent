// Pure projections of one episode read view; no DOM, network or state.

const SHOT_STATUS = {
  unregistered: "未登记",
  pending: "生成中",
  delivered: "已交付",
  failed: "需处理",
};

export function sceneGeneration(episode) {
  const shots = episode?.generation?.shots || [];
  const rows = new Map();
  for (const shot of shots) {
    const row = rows.get(shot.sceneId) || {
      total: 0,
      delivered: 0,
      pending: 0,
      failed: 0,
      unregistered: 0,
      jobs: [],
    };
    row.total += 1;
    if (row[shot.status] !== undefined) row[shot.status] += 1;
    if (shot.jobId) row.jobs.push(shot);
    rows.set(shot.sceneId, row);
  }
  return rows;
}

export function shotStatusLabel(status) {
  return SHOT_STATUS[status] || status;
}

export function episodeBudget(episode) {
  const usage = episode?.generation?.budget || {};
  return {
    shotCount: usage.shotCount ?? 0,
    targetDurationSeconds: usage.targetDurationSeconds ?? 0,
    jobCap: usage.jobCap ?? 0,
    usedJobs: usage.usedJobs ?? 0,
    remainingJobs: usage.remainingJobs ?? 0,
    currency: usage.currency || "CNY",
    usedAmount: usage.usedAmount || "0.00",
    episodeCapAmount: usage.episodeCapAmount || "0.00",
    remainingAmount: usage.remainingAmount || "0.00",
    foreignCurrencyJobs: usage.foreignCurrencyJobs || [],
  };
}

export function episodeProgress(episode) {
  const counts = episode?.generation?.counts || {};
  const planned = counts.planned ?? 0;
  const delivered = counts.delivered ?? 0;
  return {
    planned,
    delivered,
    registered: counts.registered ?? 0,
    failed: counts.failed ?? 0,
    pending: counts.pending ?? 0,
    unregistered: counts.unregistered ?? 0,
    complete: planned > 0 && delivered === planned,
  };
}

export function filmStatusLabel(film) {
  if (!film) return "尚未拼接";
  const latest = film.latest || film.versions?.at(-1);
  if (!latest) return "尚未拼接";
  const media = latest.mediaAvailable ? "可播放" : "媒体不可用";
  return `v${film.currentVersion ?? latest.version} · ${media}`;
}

export function sceneFilmPath(scene, projectId) {
  if (!scene?.storyboardId || !projectId) return null;
  return `/api/films/${encodeURIComponent(scene.storyboardId)}?projectId=${encodeURIComponent(projectId)}`;
}

export function sceneSummary(scene, row) {
  const counts = row || { total: scene.shotCount, delivered: 0, pending: 0, failed: 0, unregistered: scene.shotCount };
  const parts = [`${scene.shotCount} 镜 · ${scene.durationSeconds} 秒`];
  if (counts.delivered) parts.push(`${counts.delivered} 已交付`);
  if (counts.pending) parts.push(`${counts.pending} 生成中`);
  if (counts.failed) parts.push(`${counts.failed} 需处理`);
  if (counts.unregistered) parts.push(`${counts.unregistered} 未登记`);
  return parts.join(" · ");
}

export function episodeMeta(episode) {
  if (!episode?.episode) return null;
  return {
    title: episode.episode.title,
    currentVersion: episode.episode.currentVersion,
    revision: episode.episode.revision,
    sceneCount: episode.scenes?.length ?? 0,
    shotCount: episode.episode.shotBudget?.shotCount ?? 0,
    targetDurationSeconds: episode.episode.targetDurationSeconds,
    styleId: episode.episode.styleId,
  };
}
