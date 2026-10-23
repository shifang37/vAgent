"""M7 E1 season domain: ordered episode membership with append-only versions.

A season is a *relationship*, not a film: it owns a title and an ordered list of
one to four episode IDs and nothing else. Characters, styles, scenes, shots,
budgets, Jobs and films stay on the episode (or on the project bible), so a season
never becomes a second source of truth for them.

Schema 9 adds ``seasons``/``batches``/``timelines``; E1 implements this module
only. ``batches`` and ``timelines`` stay empty until E2/E3 add their own objects
and Store validators.
"""

from uuid import uuid4

from pydantic import Field, model_validator

from vagent.contracts import Contract, Identifier, JsonTuple, UtcTimestamp
from vagent.errors import AppError
from vagent.video.episodes import Episode, utc_now

# One project may hold eight seasons; one season freezes one to four episodes.
SEASON_PER_PROJECT_LIMIT = 8
SEASON_EPISODE_LIMIT = 4
SEASON_TITLE_MAX_CHARS = 120


def _require_continuous_versions(versions, label: str) -> None:
    if [item.version for item in versions] != list(range(1, len(versions) + 1)):
        raise ValueError(f"{label} versions must be the continuous 1..N sequence")


class SeasonVersion(Contract):
    """One frozen membership list: the title and the ordered episode IDs."""

    version: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=SEASON_TITLE_MAX_CHARS)
    episode_ids: JsonTuple[Identifier] = Field(min_length=1, max_length=SEASON_EPISODE_LIMIT)
    created_at: UtcTimestamp

    @model_validator(mode="after")
    def unique_members(self):
        if len(set(self.episode_ids)) != len(self.episode_ids):
            raise ValueError("A season version cannot list the same episode twice")
        return self


class Season(Contract):
    id: Identifier
    project_id: Identifier
    current_version: int = Field(ge=1)
    versions: JsonTuple[SeasonVersion] = Field(min_length=1)
    revision: int = Field(ge=0)
    created_at: UtcTimestamp

    @model_validator(mode="after")
    def append_only_history(self):
        _require_continuous_versions(self.versions, "Season")
        if self.current_version != len(self.versions):
            raise ValueError("currentVersion must identify the latest season version")
        return self

    @property
    def latest(self) -> SeasonVersion:
        return self.versions[-1]


def _season_record(state: dict, season_id: str) -> Season:
    raw = state["seasons"].get(season_id)
    if raw is None:
        raise AppError("SEASON_NOT_FOUND", f"季 {season_id} 不存在，未保存。")
    return Season.model_validate(raw)


def _episode_record(state: dict, episode_id: str) -> Episode:
    raw = state["episodes"].get(episode_id)
    if raw is None:
        raise AppError("SEASON_EPISODE_CONFLICT", f"分集 {episode_id} 不存在，季成员未保存。")
    return Episode.model_validate(raw)


def _validate_members(state: dict, project_id: str, episode_ids) -> None:
    """Members must be this project's episodes; ownership is checked by the caller."""
    if not 1 <= len(episode_ids) <= SEASON_EPISODE_LIMIT:
        raise AppError("SEASON_LIMIT", f"一季必须有 1–{SEASON_EPISODE_LIMIT} 集，未保存。")
    if len(set(episode_ids)) != len(episode_ids):
        raise AppError("SEASON_EPISODE_CONFLICT", "一季不能重复列出同一集。")
    for episode_id in episode_ids:
        episode = _episode_record(state, episode_id)
        if episode.project_id != project_id:
            raise AppError("SEASON_EPISODE_CONFLICT", "季成员必须是同一项目的分集。")


def season_summaries(state: dict, project_id: str) -> list[dict]:
    """One project's seasons, oldest first, without episode views or batches."""
    summaries = []
    for raw in state["seasons"].values():
        if raw["projectId"] != project_id:
            continue
        season = Season.model_validate(raw)
        latest = season.latest
        summaries.append(
            {
                "seasonId": season.id,
                "title": latest.title,
                "currentVersion": season.current_version,
                "revision": season.revision,
                "episodeIds": list(latest.episode_ids),
                "episodeCount": len(latest.episode_ids),
                "createdAt": season.created_at,
            }
        )
    summaries.sort(key=lambda item: (item["createdAt"], item["seasonId"]))
    return summaries


def read_season(args: dict, state: dict, project_id: str) -> dict:
    """The current season view shared by the tool, the CLI and the Web panel."""
    summaries = season_summaries(state, project_id)
    season_id = args.get("seasonId")
    if season_id is not None:
        raw = state["seasons"].get(season_id)
        if raw is None or raw["projectId"] != project_id:
            raise AppError("SEASON_NOT_FOUND", "当前项目中没有这个季。")
        season = Season.model_validate(raw)
    elif not summaries:
        return {"seasonId": None, "season": None, "episodes": [], "seasons": [], "batch": None}
    else:
        # Newest season by creation, not by title: renaming never moves the default.
        season = Season.model_validate(state["seasons"][summaries[-1]["seasonId"]])

    episodes = []
    for episode_id in season.latest.episode_ids:
        episode = _episode_record(state, episode_id)
        if episode.project_id != project_id:
            raise AppError("SEASON_EPISODE_CONFLICT", "季成员必须是同一项目的分集。")
        episodes.append(
            {
                "episodeId": episode.id,
                "title": episode.title,
                "currentVersion": episode.current_version,
                "revision": episode.revision,
                "sceneCount": len(episode.latest.scenes),
                "shotCount": episode.latest.shot_budget.shot_count,
                "targetDurationSeconds": episode.latest.target_duration_seconds,
                "seasonId": season.id,
                "seasonVersion": season.current_version,
            }
        )
    return {
        "seasonId": season.id,
        "season": {
            "title": season.latest.title,
            "currentVersion": season.current_version,
            "revision": season.revision,
            "episodeIds": list(season.latest.episode_ids),
            "effectivelyOrdered": True,
            "createdAt": season.created_at,
        },
        "episodes": episodes,
        "seasons": summaries,
        # E1 stores no batch yet; E2 fills this from the latest Batch record.
        "batch": None,
    }


def save_season(args: dict, state: dict, project_id: str) -> dict:
    """Create or append one SeasonVersion; never a Job, model call or provider call."""
    season_id = args.get("seasonId")
    raw = state["seasons"].get(season_id) if season_id else None
    if season_id and (raw is None or raw["projectId"] != project_id):
        raise AppError("SEASON_NOT_FOUND", "当前项目中没有这个季。")
    existing = Season.model_validate(raw) if raw is not None else None
    title = args.get("title")
    if not isinstance(title, str) or not 1 <= len(title) <= SEASON_TITLE_MAX_CHARS:
        raise AppError("SEASON_LIMIT", f"季标题必须是 1–{SEASON_TITLE_MAX_CHARS} 个字符。")
    episode_ids = list(args.get("episodeIds") or [])
    _validate_members(state, project_id, episode_ids)
    if existing is None:
        # A repeated save without seasonId replays the identical season instead of
        # appending a duplicate that would fight over the same episodes.
        existing = next(
            (
                Season.model_validate(item)
                for item in state["seasons"].values()
                if item["projectId"] == project_id
                and item["versions"][-1]["title"] == title
                and item["versions"][-1]["episodeIds"] == episode_ids
            ),
            None,
        )
    if (
        existing is not None
        and existing.latest.title == title
        and list(existing.latest.episode_ids) == episode_ids
    ):
        return {
            "seasonId": existing.id,
            "version": existing.current_version,
            "revision": existing.revision,
            "title": existing.latest.title,
            "episodeIds": episode_ids,
            "replayed": True,
        }
    if existing is not None and existing.revision != args.get("expectedRevision"):
        raise AppError("SEASON_VERSION_CONFLICT", "季记录已变化，请重新读取 revision 后再修改。")
    for episode_id in episode_ids:
        owner = _season_owner(state, project_id, episode_id)
        if owner is not None and (existing is None or owner != existing.id):
            raise AppError(
                "SEASON_EPISODE_CONFLICT",
                f"分集 {episode_id} 已属于另一个季的当前版本；请先在原季移除再加入。",
            )
    if existing is None:
        owned = [item for item in state["seasons"].values() if item["projectId"] == project_id]
        if len(owned) >= SEASON_PER_PROJECT_LIMIT:
            raise AppError("SEASON_LIMIT", f"一个项目最多 {SEASON_PER_PROJECT_LIMIT} 个季，未保存。")

    timestamp = utc_now()
    if existing is None:
        season = Season(
            id=str(uuid4()),
            project_id=project_id,
            current_version=1,
            versions=(
                SeasonVersion(
                    version=1,
                    title=title,
                    episode_ids=tuple(episode_ids),
                    created_at=timestamp,
                ),
            ),
            revision=0,
            created_at=timestamp,
        )
    else:
        version = SeasonVersion(
            version=existing.current_version + 1,
            title=title,
            episode_ids=tuple(episode_ids),
            created_at=timestamp,
        )
        season = existing.model_copy(
            update={
                "current_version": version.version,
                "versions": (*existing.versions, version),
                "revision": existing.revision + 1,
            }
        )
    state["seasons"][season.id] = season.model_dump(mode="json", by_alias=True)
    return {
        "seasonId": season.id,
        "version": season.current_version,
        "revision": season.revision,
        "title": title,
        "episodeIds": episode_ids,
        "replayed": False,
    }


def _season_owner(state: dict, project_id: str, episode_id: str) -> str | None:
    """The season whose current version lists this episode, if any."""
    for raw in state["seasons"].values():
        if raw["projectId"] != project_id:
            continue
        season = Season.model_validate(raw)
        if episode_id in season.latest.episode_ids:
            return season.id
    return None


def validate_season_changes(previous: dict[str, dict], state: dict) -> None:
    """Every Store commit re-checks identity, the project limit and append-only history."""
    seasons = state["seasons"]
    if previous.keys() - seasons.keys():
        raise AppError("SEASON_VERSION_CONFLICT", "已保存的季不能删除。")
    per_project: dict[str, int] = {}
    owners: dict[str, str] = {}
    for key, raw in seasons.items():
        try:
            record = Season.model_validate(raw)
        except ValueError:
            raise AppError("SEASON_VERSION_CONFLICT", "季记录不完整或版本不连续。") from None
        if record.id != key or record.project_id not in state["projects"]:
            raise AppError("SEASON_VERSION_CONFLICT", "季 ID 或所属项目不匹配。")
        per_project[record.project_id] = per_project.get(record.project_id, 0) + 1
        if per_project[record.project_id] > SEASON_PER_PROJECT_LIMIT:
            raise AppError("SEASON_LIMIT", f"一个项目最多 {SEASON_PER_PROJECT_LIMIT} 个季。")
        latest = record.latest
        if not 1 <= len(latest.episode_ids) <= SEASON_EPISODE_LIMIT:
            raise AppError("SEASON_LIMIT", f"一季必须有 1–{SEASON_EPISODE_LIMIT} 集。")
        for episode_id in latest.episode_ids:
            episode = _episode_record(state, episode_id)
            if episode.project_id != record.project_id:
                raise AppError("SEASON_EPISODE_CONFLICT", "季成员必须是同一项目的分集。")
            if episode_id in owners:
                raise AppError("SEASON_EPISODE_CONFLICT", "同一集不能同时属于两个季的当前版本。")
            owners[episode_id] = record.id
        old_raw = previous.get(key)
        if old_raw is None:
            if record.current_version != 1 or record.revision != 0:
                raise AppError("SEASON_VERSION_CONFLICT", "新季必须先保存第 1 版。")
            continue
        old = Season.model_validate(old_raw)
        if (
            record.project_id != old.project_id
            or record.created_at != old.created_at
            or record.revision < old.revision
            or len(record.versions) < len(old.versions)
            or record.versions[: len(old.versions)] != old.versions
        ):
            raise AppError("SEASON_VERSION_CONFLICT", "季版本只增不改，旧版本必须保留。")
