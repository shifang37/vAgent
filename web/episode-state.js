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

// M6 E4: one whole-episode render is playable only when the task succeeded and
// the committed film media can still be addressed. The projection keeps failure
// diagnostics so the panel can explain a preserved failure instead of hiding it.
export function episodeRender(view) {
  const render = view?.render;
  if (!render) return null;
  const inner = render.render || null;
  const subtitle = inner?.subtitle || null;
  const measurement = inner?.measurements || null;
  const video = measurement?.video || null;
  const verification = inner?.audio?.loudness?.output?.verification || null;
  return {
    renderId: render.renderId || null,
    status: render.status || "unknown",
    filmId: render.filmId || null,
    filmVersion: render.filmVersion ?? null,
    mediaUrl: render.status === "succeeded" ? render.mediaUrl || null : null,
    subtitleMode: subtitle?.mode || null,
    sidecarUrl: subtitle?.sidecarUrl || null,
    durationSeconds: measurement?.durationSeconds ?? null,
    width: video?.width ?? null,
    height: video?.height ?? null,
    loudnessLufs: verification?.integratedLufs ?? null,
    truePeakDb: verification?.truePeakDb ?? null,
    sceneCount: Array.isArray(render.scenes) ? render.scenes.length : 0,
    failedScenes: Array.isArray(render.scenes)
      ? render.scenes.filter((scene) => scene.status === "failed").length
      : 0,
    failureStage: render.failureStage || null,
    errorCode: render.errorCode || null,
  };
}

export function episodePlayback(view) {
  const render = episodeRender(view);
  return render?.mediaUrl ? render : null;
}

export function renderModeName(mode) {
  return { soft: "软字幕", burn: "烧录字幕", both: "软字幕 + 烧录" }[mode] || mode || "无字幕";
}

export function episodeRenderSummary(render) {
  if (!render) return "整集尚未渲染";
  if (render.status !== "succeeded") {
    const failed = render.failedScenes ? ` · ${render.failedScenes} 场失败` : "";
    return `${render.status === "failed" ? "渲染失败" : "渲染状态未知"}${failed}${
      render.errorCode ? ` · ${render.errorCode}` : ""
    }`;
  }
  const parts = [];
  if (render.durationSeconds != null) parts.push(`${render.durationSeconds.toFixed(1)} 秒`);
  if (render.width && render.height) parts.push(`${render.width}×${render.height}`);
  if (render.loudnessLufs != null) parts.push(`${render.loudnessLufs.toFixed(1)} LUFS`);
  if (render.subtitleMode) parts.push(renderModeName(render.subtitleMode));
  return parts.join(" · ") || "整集成片";
}

export function episodeSubtitleToggleLabel(on) {
  return on ? "字幕：开" : "字幕：关";
}

const SRT_TIMING =
  /^(\d{1,2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{3})/;

function srtSeconds(hours, minutes, seconds, millis) {
  return Number(hours) * 3600 + Number(minutes) * 60 + Number(seconds) + Number(millis) / 1000;
}

// Parse the controlled sidecar SRT for the playback overlay. Malformed blocks are
// skipped rather than guessed, so a truncated file cannot invent on-screen text.
export function parseSubtitleSrt(text) {
  const normalized = String(text ?? "")
    .replace(/\r\n?/g, "\n")
    .trim();
  if (!normalized) return [];
  const cues = [];
  for (const block of normalized.split(/\n{2,}/)) {
    const lines = block.split("\n");
    const index = lines.findIndex((line) => SRT_TIMING.test(line.trim()));
    if (index < 0) continue;
    const match = SRT_TIMING.exec(lines[index].trim());
    const start = srtSeconds(match[1], match[2], match[3], match[4]);
    const end = srtSeconds(match[5], match[6], match[7], match[8]);
    const body = lines.slice(index + 1).join("\n").trim();
    if (body && end > start) cues.push({ start, end, text: body });
  }
  return cues.sort((left, right) => left.start - right.start);
}

export function subtitleCueAt(cues, seconds) {
  if (!Array.isArray(cues) || !Number.isFinite(seconds)) return null;
  for (const cue of cues) {
    if (seconds >= cue.start && seconds < cue.end) return cue;
  }
  return null;
}
