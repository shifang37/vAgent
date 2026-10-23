// Pure projections of one season read view; no DOM, network or state.

export const SEASON_EPISODE_LIMIT = 4;
export const SEASON_TITLE_MAX_CHARS = 120;

export function seasonMeta(view) {
  const season = view?.season;
  if (!season) return null;
  return {
    seasonId: view.seasonId || null,
    title: season.title,
    currentVersion: season.currentVersion ?? 1,
    revision: season.revision ?? 0,
    episodeCount: (season.episodeIds || []).length,
    episodeIds: [...(season.episodeIds || [])],
    createdAt: season.createdAt || null,
  };
}

export function seasonMembers(view) {
  const rows = view?.episodes;
  if (!Array.isArray(rows)) return [];
  return rows.map((episode, index) => ({
    order: index + 1,
    episodeId: episode.episodeId,
    title: episode.title || episode.episodeId,
    currentVersion: episode.currentVersion ?? 1,
    revision: episode.revision ?? 0,
    sceneCount: episode.sceneCount ?? 0,
    shotCount: episode.shotCount ?? 0,
    targetDurationSeconds: episode.targetDurationSeconds ?? 0,
  }));
}

export function seasonMetaText(meta) {
  if (!meta) return "尚未保存任何季";
  return `${meta.title} · 当前版本 ${meta.currentVersion} · revision ${meta.revision} · ${meta.episodeCount} 集`;
}

// The season picker lists what this project already saved; the panel adds its own
// "new season" entry, so this projection never invents one.
export function seasonPickerOptions(view) {
  const rows = view?.seasons;
  if (!Array.isArray(rows)) return [];
  return rows.map((season) => ({
    value: season.seasonId,
    label: `${season.title} · v${season.currentVersion} · ${season.episodeCount} 集`,
    episodeIds: [...(season.episodeIds || [])],
  }));
}

// One checkbox row per project episode. An episode owned by *another* season's
// current version is disabled: the contract requires removing it there first.
export function seasonEpisodeRows(projectEpisodes, seasons, currentSeasonId, selection) {
  const chosen = Array.isArray(selection) ? selection : [];
  const known = new Set(projectEpisodes.map((episode) => episode.episodeId));
  const rows = [];
  for (const episode of projectEpisodes) {
    const index = chosen.indexOf(episode.episodeId);
    const owner = (seasons || []).find(
      (season) => season.seasonId !== currentSeasonId && (season.episodeIds || []).includes(episode.episodeId),
    );
    rows.push({
      episodeId: episode.episodeId,
      title: episode.title || episode.episodeId,
      order: index >= 0 ? index + 1 : 0,
      selected: index >= 0,
      ownerSeasonId: owner?.seasonId || null,
      ownerTitle: owner?.title || null,
      disabled: Boolean(owner) && index < 0,
      shotCount: episode.shotCount ?? 0,
      sceneCount: episode.sceneCount ?? 0,
      targetDurationSeconds: episode.targetDurationSeconds ?? 0,
    });
  }
  // A member that no longer exists in the project is surfaced, not silently dropped.
  const missing = chosen.filter((episodeId) => !known.has(episodeId));
  return { rows, missing };
}

export function seasonValidation(title, episodeIds) {
  const text = String(title ?? "").trim();
  const members = Array.isArray(episodeIds) ? episodeIds : [];
  if (!text) return { ok: false, error: "季标题不能为空。" };
  if (text.length > SEASON_TITLE_MAX_CHARS) {
    return { ok: false, error: `季标题不能超过 ${SEASON_TITLE_MAX_CHARS} 个字符。` };
  }
  if (members.length < 1) return { ok: false, error: "请至少选择 1 集。" };
  if (members.length > SEASON_EPISODE_LIMIT) {
    return { ok: false, error: `一季最多 ${SEASON_EPISODE_LIMIT} 集。` };
  }
  if (new Set(members).size !== members.length) {
    return { ok: false, error: "同一集不能重复选择。" };
  }
  return { ok: true, error: null, title: text, episodeIds: [...members] };
}

export function seasonSaveBody({ clientRequestId, title, episodeIds, seasonId, expectedRevision }) {
  return {
    clientRequestId,
    title: String(title ?? "").trim(),
    episodeIds: [...(episodeIds || [])],
    ...(seasonId ? { seasonId } : {}),
    ...(seasonId ? { expectedRevision } : {}),
  };
}
