// Pure subtitle projections for the workbench panel: no DOM and no network, so
// tests/test_subtitle_state.mjs drives every rule the subtitle section relies on:
// version labels, the generate/save request, soft/burn export bodies and the
// controlled SRT/ASS download links.

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

export const SUBTITLE_ERROR_LABELS = {
  SUBTITLE_CUE_CONFLICT: "字幕时间与镜头时间轴冲突",
  SUBTITLE_OVERFLOW: "字幕超出时长、字符或条数上限",
  SUBTITLE_TRACK_MISSING: "还没有可用的字幕轨",
  RENDER_SOURCE_MISSING: "缺少已绑定的场记或成片",
  RENDER_TIMELINE_MISMATCH: "字幕时间线与这个成片版本不一致",
  RENDER_PROFILE_UNSUPPORTED: "导出档位或源规格不支持",
  RENDER_TIMEOUT: "本地导出超时",
  RENDER_ENCODE_FAILED: "本地编码失败",
  RENDER_VALIDATION_FAILED: "导出文件未通过校验",
  FONT_MISSING: "包内字幕字体缺失或校验失败",
  VERSION_CONFLICT: "字幕记录已变化，请刷新后重试",
};

export function subtitleErrorLabel(error) {
  if (!error) return "";
  return SUBTITLE_ERROR_LABELS[error.code] || error.message || "字幕操作未完成";
}

export function hasSubtitle(track) {
  return Boolean(track && track.trackId && Array.isArray(track.versions) && track.versions.length);
}

// Server order is v1..vN; the panel shows the newest first but keeps the labels.
export function subtitleVersions(track) {
  if (!hasSubtitle(track)) return [];
  return track.versions
    .map((version, index) => ({
      ...version,
      index: index + 1,
      label: `v${index + 1}`,
      current: index + 1 === track.currentVersion,
      cueLabel: `${version.cueCount} 条`,
    }))
    .reverse();
}

export function latestSubtitle(track) {
  return subtitleVersions(track)[0] || null;
}

export function subtitleSummary(track) {
  if (!hasSubtitle(track)) return "还没有字幕";
  const latest = latestSubtitle(track);
  const source = latest.timelineSource === "film" ? "成片时间线" : "分镜时间线";
  return [`字幕 ${latest.label}`, latest.cueLabel, source].join(" · ");
}

export function renderModeLabel(mode) {
  if (mode === "soft") return "软字幕";
  if (mode === "burn") return "烧录字幕";
  if (mode === "both") return "软字幕 + 烧录";
  return "";
}

// One explicit action token: the storyboard snapshot and the request share a body.
export function subtitleSaveBody(storyboard, clientRequestId) {
  if (!storyboard?.storyboardId || typeof clientRequestId !== "string" || !clientRequestId) return null;
  return {
    clientRequestId,
    scope: "storyboard",
    storyboardId: storyboard.storyboardId,
  };
}

export function subtitleExportBody(track, mode, clientRequestId) {
  if (!hasSubtitle(track)) return null;
  if (!["soft", "burn", "both"].includes(mode)) return null;
  if (typeof clientRequestId !== "string" || !clientRequestId) return null;
  return {
    clientRequestId,
    trackId: track.trackId,
    trackVersion: track.currentVersion,
    mode,
    profile: "720p-h264-aac-v1",
  };
}

export function subtitleDownloadUrl(track, version, kind = "srt") {
  if (!hasSubtitle(track) || !UUID.test(track.trackId)) return null;
  const index = version?.version || (Number.isInteger(version) ? version : null);
  if (!Number.isInteger(index) || index < 1) return null;
  const suffix = kind === "ass" ? "ass" : "srt";
  const url = `/api/subtitles/${track.trackId}/${index}/${suffix}`;
  return url.startsWith("/api/subtitles/") ? url : null;
}

export function subtitleTimelineLabel(version) {
  const timeline = version?.timeline || [];
  if (!timeline.length) return "";
  const total = timeline.reduce((sum, item) => sum + (item.durationSeconds || 0), 0);
  return `${timeline.length} 段 · ${total.toFixed(2)} 秒`;
}

export function subtitleCueLabel(version) {
  if (!version || typeof version.cueCount !== "number") return "";
  return `${version.cueCount} 条字幕 · ${subtitleTimelineLabel(version)}`;
}
