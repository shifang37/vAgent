// Pure projections and edit math for one episode timeline; no DOM, network or state.
//
// The server is the authority: this module only mirrors `timeline-zh-v1` well enough
// to disable impossible edits before a request. Every number here is an integer
// millisecond, matching the frozen clip windows.

export const TIMELINE_CLIP_LIMIT = 20;
export const TIMELINE_TRANSITION_LIMIT = 19;
export const TIMELINE_MIN_CLIP_MS = 1000;
export const TIMELINE_MIN_TOTAL_MS = 1000;
export const TIMELINE_MAX_TOTAL_MS = 90000;
export const TIMELINE_TITLE_LIMIT = 4;
export const TIMELINE_CONTRACT = "timeline-zh-v1";

export function timelineMeta(view) {
  const latest = view?.latest;
  if (!latest) return null;
  return {
    timelineId: view.timelineId,
    episodeId: view.episodeId,
    version: latest.version,
    currentVersion: view.currentVersion ?? latest.version,
    revision: view.revision ?? 0,
    episodeVersion: latest.episodeVersion,
    clipCount: latest.clipCount ?? (latest.clips || []).length,
    fingerprint: latest.fingerprint,
    createdAt: latest.createdAt,
    stale: Boolean(view.stale),
  };
}

export function timelineMetaText(meta) {
  if (!meta) return "尚未保存时间线";
  return `v${meta.currentVersion} · revision ${meta.revision} · ${meta.clipCount} 个镜头${meta.stale ? " · 已过期" : ""}`;
}

// A new draft starts from the current timeline when one exists, otherwise from every
// delivered shot in episode order (a full-length first cut).
export function timelineDraft(editor) {
  const latest = editor?.timeline?.latest;
  if (latest && Array.isArray(latest.clips) && latest.clips.length) {
    return latest.clips.map((clip) => ({
      shotId: clip.shotId,
      shotVersionId: clip.shotVersionId,
      storyboardId: clip.storyboardId,
      sceneId: clip.sceneId || null,
      inMs: clip.inMs,
      outMs: clip.outMs,
      title: clip.title || clip.shotId,
      mediaUrl: clip.mediaUrl || null,
      sourceDurationMs: clip.sourceDurationMs ?? clip.outMs,
      mediaAvailable: Boolean(clip.mediaAvailable),
    }));
  }
  const shots = Array.isArray(editor?.shots) ? editor.shots : [];
  return shots
    .filter((shot) => shot.shotVersionId)
    .map((shot) => ({
      shotId: shot.shotId,
      shotVersionId: shot.shotVersionId,
      storyboardId: shot.storyboardId,
      sceneId: shot.sceneId || null,
      inMs: 0,
      outMs: shot.durationMs,
      title: shot.title || shot.shotId,
      mediaUrl: shot.mediaUrl || null,
      sourceDurationMs: shot.durationMs,
      mediaAvailable: Boolean(shot.mediaAvailable),
    }));
}

export function timelineDraftTransitions(editor) {
  const latest = editor?.timeline?.latest;
  if (!latest || !Array.isArray(latest.transitions)) return [];
  return latest.transitions
    .filter((item) => item.type !== "cut")
    .map((item) => ({
      afterShotId: item.afterShotId,
      type: item.type,
      durationMs: item.durationMs,
    }));
}

export function timelineTransitionMs(transitions) {
  return (Array.isArray(transitions) ? transitions : []).reduce(
    (total, item) => total + (item.type === "cut" ? 0 : Number(item.durationMs) || 0),
    0,
  );
}

export function timelineSummary(clips, transitions) {
  const rows = Array.isArray(clips) ? clips : [];
  const rawMs = rows.reduce((total, clip) => total + Math.max(0, clip.outMs - clip.inMs), 0);
  const transitionMs = timelineTransitionMs(transitions);
  return {
    clipCount: rows.length,
    rawMs,
    transitionMs,
    effectiveMs: Math.max(0, rawMs - transitionMs),
  };
}

export function timelineSummaryText(summary) {
  if (!summary || !summary.clipCount) return "尚无镜头";
  const seconds = (summary.effectiveMs / 1000).toFixed(1);
  const trimmed = summary.transitionMs
    ? ` · 转场 ${(summary.transitionMs / 1000).toFixed(1)} 秒（裁剪 ${(summary.rawMs / 1000).toFixed(1)} 秒）`
    : "";
  return `${summary.clipCount} 个镜头 · 有效 ${seconds} 秒${trimmed}`;
}

function transitionFor(transitions, shotId) {
  return (Array.isArray(transitions) ? transitions : []).find((item) => item.afterShotId === shotId) || null;
}

export function timelineTransitionFor(transitions, shotId) {
  return transitionFor(transitions, shotId);
}

// Set (or clear, with cut/0) the one transition after a boundary.
export function timelineSetTransition(transitions, afterShotId, type, durationMs) {
  const rows = (Array.isArray(transitions) ? transitions : []).filter(
    (item) => item.afterShotId !== afterShotId,
  );
  if (!type || type === "cut" || !durationMs) return rows;
  rows.push({ afterShotId, type, durationMs: Math.round(durationMs) });
  return rows;
}

export function timelineMove(clips, index, delta) {
  const rows = Array.isArray(clips) ? [...clips] : [];
  const target = index + delta;
  if (index < 0 || index >= rows.length || target < 0 || target >= rows.length) return rows;
  const [moved] = rows.splice(index, 1);
  rows.splice(target, 0, moved);
  return rows;
}

export function timelineTrim(clips, index, inMs, outMs) {
  const rows = Array.isArray(clips) ? [...clips] : [];
  if (index < 0 || index >= rows.length) return rows;
  const clip = { ...rows[index] };
  const low = Math.max(0, Math.round(Number(inMs) || 0));
  const high = Math.min(
    Number(clip.sourceDurationMs) || Number(outMs) || 0,
    Math.round(Number(outMs) || 0),
  );
  clip.inMs = low;
  clip.outMs = high;
  rows[index] = clip;
  return rows;
}

function trimmedWindows(clips) {
  return (Array.isArray(clips) ? clips : []).map((clip) => ({
    shotId: clip.shotId,
    durationMs: Math.max(0, clip.outMs - clip.inMs),
  }));
}

export function timelineValidation(clips, transitions) {
  const rows = Array.isArray(clips) ? clips : [];
  if (!rows.length) return { ok: false, error: "时间线至少需要一个镜头。" };
  if (rows.length > TIMELINE_CLIP_LIMIT) {
    return { ok: false, error: `时间线最多 ${TIMELINE_CLIP_LIMIT} 个镜头。` };
  }
  const ids = rows.map((clip) => clip.shotId);
  if (new Set(ids).size !== ids.length) return { ok: false, error: "同一镜头不能重复出现。" };
  for (const clip of rows) {
    const length = clip.outMs - clip.inMs;
    if (clip.inMs < 0 || clip.outMs <= clip.inMs) {
      return { ok: false, error: `镜头「${clip.title || clip.shotId}」的裁剪窗口非法。` };
    }
    if (length < TIMELINE_MIN_CLIP_MS) {
      return { ok: false, error: `镜头「${clip.title || clip.shotId}」裁剪后不足 1 秒。` };
    }
    if (clip.sourceDurationMs != null && clip.outMs > clip.sourceDurationMs) {
      return { ok: false, error: `镜头「${clip.title || clip.shotId}」的裁剪终点超出源时长。` };
    }
  }
  const active = Array.isArray(transitions) ? transitions : [];
  if (active.length > TIMELINE_TRANSITION_LIMIT) {
    return { ok: false, error: `转场最多 ${TIMELINE_TRANSITION_LIMIT} 个。` };
  }
  const windows = trimmedWindows(rows);
  const position = new Map(ids.map((shotId, index) => [shotId, index]));
  for (const transition of active) {
    const index = position.get(transition.afterShotId);
    if (index == null) return { ok: false, error: "转场引用了时间线中不存在的镜头。" };
    if (index === rows.length - 1) return { ok: false, error: "最后一个镜头之后不能有转场。" };
    const duration = Number(transition.durationMs) || 0;
    if (duration < 100 || duration > 1000) {
      return { ok: false, error: "转场时长必须在 0.1–1.0 秒之间。" };
    }
    if (duration > windows[index].durationMs / 3 || duration > windows[index + 1].durationMs / 3) {
      return { ok: false, error: "转场不能超过相邻任一镜头时长的三分之一。" };
    }
  }
  const summary = timelineSummary(rows, active);
  if (summary.effectiveMs < TIMELINE_MIN_TOTAL_MS || summary.effectiveMs > TIMELINE_MAX_TOTAL_MS) {
    return { ok: false, error: "有效时长必须在 1.000–90.000 秒之间。" };
  }
  if (active.length && new Set(active.map((item) => item.afterShotId)).size !== active.length) {
    return { ok: false, error: "同一镜头边界只能设置一个转场。" };
  }
  return { ok: true, error: null, summary };
}

export function timelineSaveBody({
  clientRequestId,
  episodeId,
  timelineId,
  clips,
  transitions,
  subtitle,
  audio,
  titleCards,
  expectedRevision,
}) {
  return {
    clientRequestId,
    episodeId,
    clips: (clips || []).map((clip) => ({
      shotId: clip.shotId,
      shotVersionId: clip.shotVersionId,
      storyboardId: clip.storyboardId,
      ...(clip.sceneId ? { sceneId: clip.sceneId } : {}),
      inMs: clip.inMs,
      outMs: clip.outMs,
    })),
    transitions: (transitions || [])
      .filter((item) => item.type !== "cut" && item.durationMs)
      .map((item) => ({
        afterShotId: item.afterShotId,
        type: item.type,
        durationMs: item.durationMs,
      })),
    ...(subtitle ? { subtitle: { trackId: subtitle.trackId, trackVersion: subtitle.trackVersion } } : {}),
    audio: (audio || []).map((item) => ({
      audioId: item.audioId,
      audioVersion: item.audioVersion,
      kind: item.kind,
      gainDb: Number(item.gainDb) || 0,
      startMs: item.kind === "voice" ? 0 : Math.max(0, Math.round(Number(item.startMs) || 0)),
    })),
    titleCards: (titleCards || []).map((card) => ({
      text: card.text,
      startMs: card.startMs,
      durationMs: card.durationMs,
    })),
    ...(timelineId ? { timelineId } : {}),
    ...(timelineId && expectedRevision != null ? { expectedRevision } : {}),
  };
}

export function timelineAudioRows(editor) {
  const rows = Array.isArray(editor?.audio) ? editor.audio : [];
  return rows.map((item) => ({
    audioId: item.audioId,
    audioVersion: item.audioVersion,
    kind: item.kind,
    title: item.title || item.audioId,
    durationSeconds: item.durationSeconds ?? 0,
    mediaUrl: item.mediaUrl || null,
  }));
}

export function timelineSubtitleOptions(editor) {
  const tracks = Array.isArray(editor?.subtitleTracks) ? editor.subtitleTracks : [];
  return tracks.map((track) => ({
    trackId: track.trackId,
    version: track.versions?.[track.versions.length - 1]?.version ?? track.currentVersion ?? 1,
    label: `整集字幕 v${track.versions?.[track.versions.length - 1]?.version ?? track.currentVersion ?? 1}`,
    cueCount: track.versions?.[track.versions.length - 1]?.cueCount ?? 0,
  }));
}

// The export is the deliverable; the preview only plays source clips in edit order.
export function timelinePreview(clips, transitions) {
  const rows = Array.isArray(clips) ? clips : [];
  const active = Array.isArray(transitions) ? transitions : [];
  let start = 0;
  return rows.map((clip, index) => {
    const outgoing = transitionFor(active, clip.shotId);
    const durationMs = Math.max(0, clip.outMs - clip.inMs);
    const entry = {
      shotId: clip.shotId,
      title: clip.title || clip.shotId,
      mediaUrl: clip.mediaUrl || null,
      inMs: clip.inMs,
      outMs: clip.outMs,
      effectiveStartMs: start,
      effectiveDurationMs: durationMs - (outgoing ? outgoing.durationMs : 0),
      incomingTransition: index > 0 ? (transitionFor(active, rows[index - 1].shotId)?.type ?? "cut") : "cut",
      outgoingTransition: outgoing?.type ?? "cut",
      index,
    };
    start += entry.effectiveDurationMs;
    return entry;
  });
}

export function timelineRenderBody({ clientRequestId, timelineId, timelineVersion, profile, mode }) {
  return {
    clientRequestId,
    scope: "timeline",
    timelineId,
    ...(timelineVersion ? { timelineVersion } : {}),
    profile: profile || "720p-h264-aac-v1",
    subtitleMode: mode || "soft",
    confirm: true,
  };
}

const TIMELINE_ERRORS = {
  TIMELINE_INVALID: "时间线的镜头、转场、字幕或标题卡非法。",
  TIMELINE_NOT_FOUND: "当前项目中没有这条时间线或指定版本。",
  TIMELINE_TRIM_INVALID: "裁剪窗口越界、倒序或不足 1 秒。",
  TIMELINE_SOURCE_MISSING: "时间线已过期或镜头/字幕/音频未交付，请重新保存后再渲染。",
  TIMELINE_VERSION_CONFLICT: "时间线已被修改，请重新读取 revision。",
  SUBTITLE_TRACK_MISSING: "时间线需要本集的整集字幕轨。",
  AUDIO_ASSET_MISSING: "时间线引用的音频资产缺失。",
  AUDIO_LICENSE_MISSING: "导入音频必须填写许可与来源。",
  AUDIO_FORMAT_UNSUPPORTED: "只接受 WAV 或 MP4/M4A 音频。",
  AUDIO_TOO_LARGE: "音频超过 25 MB。",
  AUDIO_TOO_LONG: "音频超过 60 秒。",
  CONFIRMATION_REQUIRED: "请勾选确认后再提交。",
  RENDER_SOURCE_MISSING: "缺少已交付镜头或媒体。",
  RENDER_TIMELINE_MISMATCH: "输出时长与时间线不一致，未交付。",
  RENDER_ENCODE_FAILED: "本地编码失败，未交付成片。",
  RENDER_VALIDATION_FAILED: "导出文件未通过校验，未交付成片。",
  LOUDNESS_OUT_OF_RANGE: "响度超出容差，未交付。",
};

export function timelineErrorLabel(code) {
  return TIMELINE_ERRORS[code] || "时间线操作失败。";
}
