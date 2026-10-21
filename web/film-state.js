// Pure film projections for the workbench panel: no DOM and no network, so
// tests/test_film_state.mjs can drive every rule the film section relies on:
// version labels, the explicit assemble request and the playable output.

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

export const FILM_ERROR_LABELS = {
  FILM_INPUT_MISSING: "还有镜头没有生成记录",
  FILM_INPUT_NOT_READY: "还有镜头的最新版本未交付",
  FILM_INPUT_INCOMPATIBLE: "镜头编码不能直接拼接",
  FILM_MEDIA_INVALID: "成片或镜头文件校验失败",
  ASSEMBLER_UNAVAILABLE: "本地拼接组件不可用",
  FILM_ASSEMBLY_FAILED: "本地拼接失败",
  VERSION_CONFLICT: "分镜已变化，请刷新后重试",
};

export function filmErrorLabel(error) {
  if (!error) return "";
  return FILM_ERROR_LABELS[error.code] || error.message || "拼接未完成";
}

export function hasFilm(film) {
  return Boolean(film && film.filmId && Array.isArray(film.versions) && film.versions.length);
}

// Versions are newest first for display, but labels keep the server order: v1 is
// always the first assembled film, and a new assembly appends vN+1.
export function filmVersions(film) {
  if (!hasFilm(film)) return [];
  return film.versions
    .map((version, index) => ({
      ...version,
      index: index + 1,
      label: `v${index + 1}`,
      current: index + 1 === film.currentVersion,
      shots: (version.inputs || []).length,
      durationLabel:
        typeof version.durationSeconds === "number" ? `${version.durationSeconds.toFixed(2)} 秒` : "",
    }))
    .reverse();
}

export function latestFilmVersion(film) {
  return filmVersions(film)[0] || null;
}

// The player only ever shows a version the server marked available; a missing file
// must not look playable just because the version record still exists.
export function filmPlayback(film) {
  const versions = filmVersions(film);
  const playable = versions.find((version) => version.mediaAvailable && version.mediaUrl);
  if (!playable) return { url: null, version: null, versions };
  return { url: playable.mediaUrl, version: playable, versions };
}

export function filmSummary(film) {
  if (!hasFilm(film)) return "还没有成片";
  const latest = filmVersions(film)[0];
  const parts = [`成片 ${latest.label}`, `${latest.shots} 个镜头`];
  if (latest.durationLabel) parts.push(latest.durationLabel);
  if (!latest.mediaAvailable) parts.push("本地文件不可用");
  return parts.join(" · ");
}

// One explicit action token: the body the panel sends and the revision it renders
// come from the same storyboard snapshot, so a changed storyboard cannot be sent.
export function assembleBody(storyboard, clientRequestId) {
  if (!storyboard?.storyboardId || !storyboard.currentVersion) return null;
  if (typeof clientRequestId !== "string" || !clientRequestId) return null;
  if (typeof storyboard.revision !== "number") return null;
  return {
    clientRequestId,
    storyboardId: storyboard.storyboardId,
    expectedRevision: storyboard.revision,
  };
}

export function filmRequestPath(storyboard, projectId) {
  const id = storyboard?.storyboardId;
  if (typeof id !== "string" || !UUID.test(id)) return null;
  return `/api/films/${encodeURIComponent(id)}?projectId=${encodeURIComponent(projectId || "")}`;
}

// Displaying a film version must never leak a local path: only controlled URLs.
export function filmDownloadUrl(version) {
  if (!version?.mediaAvailable || typeof version.mediaUrl !== "string") return null;
  if (!version.mediaUrl.startsWith("/api/media/")) return null;
  const separator = version.mediaUrl.includes("?") ? "&" : "?";
  return `${version.mediaUrl}${separator}download=1`;
}

export function filmTimelineLabel(version) {
  const timeline = version?.timeline || [];
  if (!timeline.length) return "";
  const total = timeline.reduce((sum, item) => sum + (item.durationSeconds || 0), 0);
  return `${timeline.length} 段 · ${total.toFixed(2)} 秒时间线`;
}
