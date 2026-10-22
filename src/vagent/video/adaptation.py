"""M5 E1: deterministic script/novelette adaptation (``text-split-zh-v1``).

The contract is offline and reproducible: the same text always yields the same
chapter/scene/dialogue/shot draft, with no model call, randomness, clock, ID or
filesystem access. Everything here is a pure function of its arguments, so a
draft can be re-derived from the stored scenes later instead of being stored twice.

Chapter, scene and shot boundaries follow the frozen rules in
docs/M5_CONTRACTS.md §4; any text the rules cannot adapt raises an explicit
``AppError`` instead of silently dropping dialogue.
"""

import re
from dataclasses import dataclass
from decimal import Decimal

from vagent.errors import AppError
from vagent.video.contracts import SHOT_DURATION_MAX, SHOT_DURATION_MIN, WAN_PRICE_VERSION
from vagent.video.episodes import (
    EPISODE_COST_CAP,
    EPISODE_JOB_CAP,
    EPISODE_MAX_SECONDS,
    EPISODE_MAX_SHOTS,
    EPISODE_MIN_SECONDS,
    EPISODE_MIN_SHOTS,
    SCENE_LIMIT,
    SCENE_MAX_SHOTS,
    SHOT_ESTIMATE_AMOUNT,
    SHOT_ESTIMATE_CURRENCY,
    SOURCE_TEXT_MAX_CHARACTERS,
    TEXT_SPLIT_CONTRACT,
)

# --- Frozen algorithm constants ---------------------------------------------
# A scene is bounded by beats as well as by size, so neither one enormous
# paragraph nor a long run of tiny ones can collapse the text into a single scene.
SCENE_PARAGRAPH_LIMIT = 6
SCENE_CHARACTER_LIMIT = 900
# Drafting starts from one shot per beat and is then fitted into 10..20 shots.
SCENE_DRAFT_SHOTS = 3
# A plain 5-second shot keeps the estimate aligned with the frozen Wan price.
TARGET_SECONDS_PER_SHOT = 5

CHAPTER_MARKERS = (
    re.compile(r"^#{1,6}\s*(.+)$"),
    re.compile(r"^第[0-9一二三四五六七八九十百零]+[章节回](.*)$"),
)
SCENE_MARKERS = (
    re.compile(r"^(内景|外景|场景)[:：\s]*(.*)$"),
    re.compile(r"^第[0-9一二三四五六七八九十]+场[:：\s]*(.*)$"),
    re.compile(r"^(INT|EXT)\.?\s+(.+)$", re.IGNORECASE),
)
DIVIDER = re.compile(r"^(?:-{3,}|\*{3,}|_{3,})$")

QUOTE_PATTERNS = (re.compile(r"「([^」]{1,300})」"), re.compile(r"“([^”]{1,300})”"))
SPEAKER_PREFIX = re.compile(r"^\s*([\u4e00-\u9fa5A-Za-z0-9]{1,8})\s*[：:]")
SPEAKER_VERB = re.compile(
    r"([\u4e00-\u9fa5]{2,4})(?:低声说|轻声说|说道|问道|答道|喊道|说|问|道|答)"
)
SENTENCE_END = re.compile(r"[。！？；\n]")

NOT_A_NAME = frozenset(
    {
        "我",
        "你",
        "他",
        "她",
        "它",
        "我们",
        "你们",
        "他们",
        "她们",
        "大家",
        "谁",
        "有人",
        "一个",
        "这时",
        "然后",
        "于是",
        "但是",
        "可是",
        "所以",
        "突然",
        "接着",
        # Pronouns only: roles such as 女孩/老人 are real cast members in a script.
    }
)

# Trailing function words that a verb-attribution regex can swallow ("陈默没有问").
NAME_TAIL_STOPWORDS = ("没有", "已经", "正在", "一直", "忽然", "突然", "也", "又", "还", "就", "却", "不", "在")

# Frozen lexicon: only these words decide a scene's time of day and location.
TIME_OF_DAY_WORDS = (
    ("清晨", "dawn"),
    ("黎明", "dawn"),
    ("早晨", "dawn"),
    ("黄昏", "dusk"),
    ("傍晚", "dusk"),
    ("深夜", "night"),
    ("午夜", "night"),
    ("夜晚", "night"),
    ("雨夜", "night"),
    ("夜里", "night"),
    ("白天", "day"),
    ("日间", "day"),
)
INTERIOR_WORDS = ("内景", "室内", "屋里", "房间", "办公室", "咖啡店", "咖啡馆", "车厢", "电梯")
EXTERIOR_WORDS = ("外景", "室外", "街道", "街头", "门口", "广场", "天台", "巷子", "雨中")
# Most specific place first: a scene inside a café is "咖啡店", not "内景".
LOCATION_WORDS = (
    "咖啡店",
    "咖啡馆",
    "办公室",
    "车厢",
    "电梯",
    "街道",
    "街头",
    "门口",
    "广场",
    "天台",
    "巷子",
    "房间",
    "屋里",
    "室内",
    "室外",
    "内景",
    "外景",
    "雨中",
)
TIME_LABELS = {"day": "白天", "night": "夜晚", "dawn": "清晨", "dusk": "黄昏"}

FRAMINGS = ("wide", "medium", "close", "extreme_close", "over_shoulder", "aerial")
MOVEMENTS = ("缓慢推进", "平稳跟拍", "固定镜头", "横向移动", "微微上摇", "轻轻前推")


class _Scene:
    """Mutable draft state for one scene; never leaves this module."""

    def __init__(self, index: int, heading: str, paragraphs: list[str]):
        self.index = index
        self.heading = heading
        self.paragraphs = paragraphs
        self.dialogue: list[Dialogue] = []
        self.location: str | None = None
        self.time_of_day = "unknown"
        self.characters: tuple[str, ...] = ()
        self.shot_count = 1
        self.duration_seconds = TARGET_SECONDS_PER_SHOT

    @property
    def synopsis(self) -> str:
        # A pure marker paragraph ("内景 咖啡店门口 夜") is not a synopsis.
        for paragraph in self.paragraphs:
            if _scene_marker(paragraph) == paragraph.strip():
                continue
            for sentence in SENTENCE_END.split(paragraph):
                if sentence.strip():
                    return sentence.strip()[:200]
        return self.heading


@dataclass(frozen=True)
class Dialogue:
    text: str
    speaker: str | None
    paragraph: int


def normalize(text: str) -> str:
    """One canonical text form: no BOM/CRLF, no full-width blanks, no blank runs."""
    if not isinstance(text, str):
        raise AppError("INVALID_ARGUMENTS", "正文必须是字符串。")
    cleaned = text.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")
    cleaned = cleaned.replace("\u3000", " ").replace("\t", " ")
    cleaned = "\n".join(line.rstrip() for line in cleaned.split("\n"))
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def split_chapters(text: str) -> list[tuple[str, str]]:
    """Chapter titles and bodies, honouring Markdown headings and 第N章/节/回."""
    chapters: list[tuple[str, list[str]]] = [("", [])]
    for line in text.split("\n"):
        stripped = line.strip()
        if DIVIDER.match(stripped):
            continue
        marker = None
        for pattern in CHAPTER_MARKERS:
            found = pattern.match(stripped)
            if found:
                marker = (found.group(1) or "").strip()
                break
        if marker:
            # A heading with no body yet renames the current chapter instead of
            # creating an empty one, so consecutive headings cannot split a section.
            if chapters[-1][1]:
                chapters.append((marker, []))
            else:
                chapters[-1] = (marker, chapters[-1][1])
            continue
        chapters[-1][1].append(line)
    result = []
    for title, lines in chapters:
        body = "\n".join(lines).strip()
        if body:
            result.append((title, body))
    if not result and text.strip():
        result.append(("", text.strip()))
    return result


def split_paragraphs(body: str) -> list[str]:
    blocks, current = [], []
    for line in body.split("\n"):
        if line.strip():
            current.append(line.strip())
        elif current:
            blocks.append("\n".join(current))
            current = []
    if current:
        blocks.append("\n".join(current))
    return blocks


def _scene_marker(paragraph: str) -> str | None:
    first = paragraph.split("\n")[0].strip()
    for pattern in SCENE_MARKERS:
        found = pattern.match(first)
        if found:
            groups = [group.strip() for group in found.groups() if group and group.strip()]
            return " ".join(groups) if groups else first
    return None


def _detect_time_of_day(paragraphs: list[str]) -> str:
    """Marker lines win over narration, so a scene keeps the time it opens with."""
    marker_lines = [paragraphs[0].split("\n")[0]] if paragraphs else []
    for scope in (marker_lines, paragraphs[:2]):
        for text in scope:
            for word, value in TIME_OF_DAY_WORDS:
                if word in text:
                    return value
            if "夜" in text:
                return "night"
            if "日" in text or "晨" in text:
                return "day"
    return "unknown"


def _detect_location(paragraphs: list[str]) -> str | None:
    for scope in (lambda text: text.split("\n")[0], lambda text: text):
        for paragraph in paragraphs[:2]:
            candidate = scope(paragraph)
            for word in LOCATION_WORDS:
                if word in candidate:
                    return word
    return None


def split_scenes(chapter_title: str, body: str) -> list[_Scene]:
    """Group paragraphs into beats; explicit markers, size caps and time shifts split."""
    paragraphs = split_paragraphs(body)
    if not paragraphs:
        return []
    groups: list[list[str]] = []
    current: list[str] = []
    for paragraph in paragraphs:
        if current:
            oversized = len(current) >= SCENE_PARAGRAPH_LIMIT or sum(
                len(item) for item in current
            ) >= SCENE_CHARACTER_LIMIT
            shifted = len(current) >= 2 and _detect_time_of_day(
                [paragraph]
            ) != _detect_time_of_day(current) != "unknown"
            if _scene_marker(paragraph) or oversized or shifted:
                groups.append(current)
                current = []
        current.append(paragraph)
    if current:
        groups.append(current)

    scenes = []
    for position, group in enumerate(groups, start=1):
        marker = _scene_marker(group[0])
        heading = marker or (f"{chapter_title} {position}" if chapter_title else f"场景 {position}")
        scene = _Scene(index=position, heading=heading[:120], paragraphs=group)
        scene.location = _detect_location(group)
        scene.time_of_day = _detect_time_of_day(group)
        scene.dialogue = extract_dialogue(group)
        scene.characters = tuple(dict.fromkeys(item.speaker for item in scene.dialogue if item.speaker))
        scenes.append(scene)
    return scenes


def _clean_name(name: str | None) -> str | None:
    if not name:
        return None
    cleaned = name.strip()
    for stopword in NAME_TAIL_STOPWORDS:
        if cleaned.endswith(stopword) and len(cleaned) > len(stopword) + 1:
            cleaned = cleaned[: -len(stopword)]
    if cleaned in NOT_A_NAME or len(cleaned) < 2:
        return None
    return cleaned


def _attribution(paragraph: str, quote_start: int) -> str | None:
    """The speaker named just before a quote, if the text attributes it."""
    prefix = SPEAKER_PREFIX.match(paragraph)
    if prefix and prefix.end() <= quote_start:
        return _clean_name(prefix.group(1))
    matches = list(SPEAKER_VERB.finditer(paragraph[:quote_start]))
    if matches:
        return _clean_name(matches[-1].group(1))
    return None


def extract_dialogue(paragraphs: list[str]) -> list[Dialogue]:
    """Every non-empty quoted line, in text order, with its deterministic attribution."""
    found: list[Dialogue] = []
    for position, paragraph in enumerate(paragraphs):
        hits = []
        for pattern in QUOTE_PATTERNS:
            for match in pattern.finditer(paragraph):
                hits.append((match.start(), match.group(1)))
        for start, line in sorted(hits):
            text = line.strip()
            if text:
                found.append(Dialogue(text=text, speaker=_attribution(paragraph, start), paragraph=position))
    return found


def _quoted_count(text: str) -> int:
    total = 0
    for pattern in QUOTE_PATTERNS:
        total += sum(1 for match in pattern.finditer(text) if match.group(1).strip())
    return total


def recognize_characters(scenes: list[_Scene]) -> list[dict]:
    """Deterministic cast: dialogue speakers by first appearance, with their counts."""
    seen: dict[str, int] = {}
    first: dict[str, int] = {}
    for scene in scenes:
        for item in scene.dialogue:
            if not item.speaker:
                continue
            seen[item.speaker] = seen.get(item.speaker, 0) + 1
            first.setdefault(item.speaker, scene.index)
    return [
        {"name": name, "dialogueCount": count, "firstScene": first[name]}
        for name, count in seen.items()
    ]


def _scene_shot_targets(scenes: list[_Scene]) -> list[int]:
    """Fit per-scene shot counts into the frozen 10..20 total, deterministically."""
    if len(scenes) * SCENE_MAX_SHOTS < EPISODE_MIN_SHOTS:
        raise AppError(
            "EPISODE_SHOT_BUDGET",
            f"切分出 {len(scenes)} 个场景，最多容纳 {len(scenes) * SCENE_MAX_SHOTS} 个镜头，"
            f"不足每集最少的 {EPISODE_MIN_SHOTS} 个镜头。",
        )
    targets = [max(1, min(SCENE_DRAFT_SHOTS, len(scene.paragraphs))) for scene in scenes]
    total = sum(targets)
    cursor = 0
    while total < EPISODE_MIN_SHOTS:
        index = cursor % len(targets)
        if targets[index] < SCENE_MAX_SHOTS:
            targets[index] += 1
            total += 1
        cursor += 1
    cursor = 0
    while total > EPISODE_MAX_SHOTS:
        index = cursor % len(targets)
        if targets[index] > 1:
            targets[index] -= 1
            total -= 1
        cursor += 1
    if sum(targets) != total:  # pragma: no cover - defensive
        raise AppError("EPISODE_SHOT_BUDGET", "无法把镜头数压进冻结上限。")
    return targets


def _scene_durations(targets: list[int], total_seconds: int) -> list[int]:
    """Integer per-scene seconds that respect the shot count and add up exactly."""
    shots = sum(targets)
    low = [SHOT_DURATION_MIN * count for count in targets]
    high = [SHOT_DURATION_MAX * count for count in targets]
    if not sum(low) <= total_seconds <= sum(high):
        raise AppError("EPISODE_DURATION_LIMIT", "冻结的时长区间无法容纳这些镜头。")
    values = [
        max(low[index], min(high[index], round(total_seconds * count / shots)))
        for index, count in enumerate(targets)
    ]
    while sum(values) < total_seconds:
        for index in range(len(values)):
            if sum(values) >= total_seconds:
                break
            if values[index] < high[index]:
                values[index] += 1
    while sum(values) > total_seconds:
        for index in range(len(values)):
            if sum(values) <= total_seconds:
                break
            if values[index] > low[index]:
                values[index] -= 1
    if sum(values) != total_seconds:  # pragma: no cover - defensive
        raise AppError("EPISODE_DURATION_LIMIT", "无法在冻结约束内分配每场时长。")
    return values


def _shot_durations(scene_seconds: int, shots: int) -> list[int]:
    base, remainder = divmod(scene_seconds, shots)
    values = [base + (1 if position < remainder else 0) for position in range(shots)]
    if any(not SHOT_DURATION_MIN <= value <= SHOT_DURATION_MAX for value in values):
        raise AppError("EPISODE_DURATION_LIMIT", "无法在冻结约束内分配镜头时长。")
    return values


def draft_shots(scene: _Scene) -> list[dict]:
    """Shot drafts re-derived from a stored scene: pure, stable, no random IDs."""
    dialogue = list(scene.dialogue)
    shots = []
    for position in range(scene.shot_count):
        line = dialogue[position % len(dialogue)] if dialogue else None
        cast = [line.speaker] if line and line.speaker else list(scene.characters)
        if position == 0:
            details = [item for item in (scene.location, TIME_LABELS.get(scene.time_of_day)) if item]
            tail = f"，交代{'、'.join(details)}。" if details else "。"
            description = f"{scene.heading}：建立镜头{tail}"
        elif line is not None:
            description = f"{scene.heading}：{line.speaker or '画外'}的台词镜头——{line.text}"
        else:
            description = f"{scene.heading}：{scene.synopsis}"
        shots.append(
            {
                "order": position + 1,
                "description": description[:2000],
                "framing": FRAMINGS[position % len(FRAMINGS)],
                "cameraMovement": MOVEMENTS[position % len(MOVEMENTS)],
                "characterNames": [name for name in cast if name],
            }
        )
    return shots


def draft_stored_scene(version: dict) -> list[dict]:
    """Shot drafts for one *stored* SceneVersion: same rules, no stored draft.

    The frozen scene keeps heading/synopsis/dialogue/shot counts, which is exactly
    what the drafting rules read, so the drafts stay reproducible without keeping
    a second copy of them in the Store.
    """
    scene = _Scene(index=int(version.get("index") or 1), heading=version["heading"], paragraphs=[version["synopsis"]])
    scene.location = version.get("location")
    scene.time_of_day = version.get("timeOfDay") or "unknown"
    scene.dialogue = [
        Dialogue(
            text=line["text"],
            speaker=line.get("speaker"),
            paragraph=int(line.get("paragraph") or 0),
        )
        for line in version.get("dialogue") or ()
    ]
    scene.characters = tuple(dict.fromkeys(item.speaker for item in scene.dialogue if item.speaker))
    scene.shot_count = int(version["shotCount"])
    scene.duration_seconds = int(version["durationSeconds"])
    shots = draft_shots(scene)
    for shot, seconds in zip(
        shots, _shot_durations(scene.duration_seconds, scene.shot_count), strict=True
    ):
        shot["durationSeconds"] = seconds
    return shots


def plan_episode(text: str, *, title: str | None = None) -> dict:
    """The whole ``text-split-zh-v1`` draft: chapters, scenes, dialogue, cast, shots, budget."""
    cleaned = normalize(text)
    if not cleaned:
        raise AppError("TEXT_SPLIT_FAILED", "正文为空，无法切分。")
    if len(cleaned) > SOURCE_TEXT_MAX_CHARACTERS:
        raise AppError(
            "EPISODE_SOURCE_TOO_LONG",
            f"正文 {len(cleaned)} 字符，超过单集上限 {SOURCE_TEXT_MAX_CHARACTERS} 字符。",
        )
    chapters = split_chapters(cleaned)
    if not chapters:
        raise AppError("TEXT_SPLIT_FAILED", "没有可用的章节内容。")

    scenes: list[_Scene] = []
    for chapter_title, body in chapters:
        scenes.extend(split_scenes(chapter_title, body))
    if not scenes:
        raise AppError("TEXT_SPLIT_FAILED", "没有可用的场景内容。")
    if len(scenes) > SCENE_LIMIT:
        raise AppError(
            "EPISODE_SHOT_BUDGET",
            f"切分出 {len(scenes)} 个场景，超过单集上限 {SCENE_LIMIT} 个；请拆分文本后分集改编。",
        )

    quoted = _quoted_count(cleaned)
    extracted = sum(len(scene.dialogue) for scene in scenes)
    if extracted != quoted:
        raise AppError("TEXT_SPLIT_FAILED", f"对白数量不一致：正文 {quoted} 条，抽取 {extracted} 条。")

    targets = _scene_shot_targets(scenes)
    total_shots = sum(targets)
    total_seconds = max(
        EPISODE_MIN_SECONDS, min(EPISODE_MAX_SECONDS, total_shots * TARGET_SECONDS_PER_SHOT)
    )
    durations = _scene_durations(targets, total_seconds)
    for scene, shots, seconds in zip(scenes, targets, durations, strict=True):
        scene.shot_count = shots
        scene.duration_seconds = seconds

    shot_rows: list[dict] = []
    scene_rows: list[dict] = []
    for scene in scenes:
        for shot, seconds in zip(
            draft_shots(scene), _shot_durations(scene.duration_seconds, scene.shot_count), strict=True
        ):
            shot["sceneIndex"] = scene.index
            shot["durationSeconds"] = seconds
            shot_rows.append(shot)
        scene_rows.append(
            {
                "index": scene.index,
                "heading": scene.heading,
                "location": scene.location,
                "timeOfDay": scene.time_of_day,
                "synopsis": scene.synopsis,
                "characterNames": list(scene.characters),
                "shotCount": scene.shot_count,
                "durationSeconds": scene.duration_seconds,
                "dialogue": [
                    {"speaker": item.speaker, "text": item.text, "paragraph": item.paragraph}
                    for item in scene.dialogue
                ],
            }
        )

    total_amount = Decimal(SHOT_ESTIMATE_AMOUNT) * total_shots
    if total_amount > Decimal(EPISODE_COST_CAP):
        raise AppError("EPISODE_COST_LIMIT", "逐镜头估算合计超过每集成本上限。")
    characters = recognize_characters(scenes)
    warnings = []
    if quoted and not characters:
        warnings.append("对白没有归属说话人，未识别出角色；请先在角色圣经中登记姓名。")
    if any(not scene.dialogue for scene in scenes):
        warnings.append("部分场景没有对白，镜头草案按场景摘要生成。")
    return {
        "textSplitContract": TEXT_SPLIT_CONTRACT,
        "title": (title or chapters[0][0] or scenes[0].heading)[:120],
        "sourceCharacters": len(cleaned),
        "dialogueCount": extracted,
        "chapters": [
            {"title": chapter_title or "正文", "characterCount": len(normalize(body))}
            for chapter_title, body in chapters
        ],
        "characters": characters,
        "scenes": scene_rows,
        "shots": shot_rows,
        "shotBudget": {
            "shotCount": total_shots,
            "targetDurationSeconds": total_seconds,
            "sceneMaxShots": SCENE_MAX_SHOTS,
            "jobCap": EPISODE_JOB_CAP,
            "perShotAmount": SHOT_ESTIMATE_AMOUNT,
            "currency": SHOT_ESTIMATE_CURRENCY,
            "episodeCapAmount": EPISODE_COST_CAP,
            "priceVersion": WAN_PRICE_VERSION,
        },
        "estimate": {
            "shotCount": total_shots,
            "amount": format(total_amount, ".2f"),
            "currency": SHOT_ESTIMATE_CURRENCY,
            "perShotAmount": SHOT_ESTIMATE_AMOUNT,
            "priceVersion": WAN_PRICE_VERSION,
        },
        "warnings": warnings,
    }
