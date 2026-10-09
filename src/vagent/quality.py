"""Deterministic checks for project facts and user-specified artifact length limits."""

import re
import unicodedata

from vagent.errors import AppError

COUNT_METHOD = "unicode_non_whitespace_v1"
ARTIFACT_KINDS = ("brief", "script", "storyboard")
KIND_NAMES = re.compile(
    r"(?<![a-z0-9_])(?:brief|script|storyboard)(?![a-z0-9_])|创作方案|方案|脚本|分镜|镜头稿", re.I
)
MAX_LENGTH = re.compile(
    r"(?:不(?:得|要)?超过|别超过|不多于|最多|至多|上限(?:为|是)?|控制在|保持在?|限于|<=|≤)\s*"
    r"(?P<before>\d+)\s*(?:个)?(?:字符|字)(?!幕)"
    r"|(?P<after>\d+)\s*(?:个)?(?:字符|字)(?:以内|之内|以下|为限)"
)
CLEAR_LENGTH = re.compile(r"(?:取消|去掉|解除)(?:所有|全部|正文)?字数限制|不再限制字数|(?:正文)?不限字数")


def character_count(content: str) -> int:
    """Count Unicode code points, including punctuation/Markdown but excluding whitespace."""
    return sum(not char.isspace() for char in content)


def content_check(content: str, maximum: int | None) -> dict:
    count = character_count(content)
    if not count:
        raise AppError("CONTENT_EMPTY", "正文不能只有空白；未保存产物。")
    if maximum is not None and count > maximum:
        raise AppError(
            "CONTENT_LENGTH",
            f"正文为 {count} 个非空白字符，上限 {maximum}；标点、英文、数字和 Markdown 标记均计入。"
            f"至少需删去 {count - maximum} 个字符，建议压缩至 {max(1, maximum * 4 // 5)} 字左右，预留计数余量。"
            "合并重复说明、移除非必要小标题或表格分隔线，保留用户必需信息后重新调用 artifact_save；未保存新版本。",
        )
    return {"characters": count, "maxCharacters": maximum, "method": COUNT_METHOD}


def _kind(name: str) -> str:
    name = name.lower()
    if name in {"brief", "创作方案", "方案"}:
        return "brief"
    if name in {"script", "脚本"}:
        return "script"
    return "storyboard"


def requested_content_limits(prompt: str) -> dict[str, int | None]:
    """Recognize explicit numeric Chinese upper bounds, never infer them from tool/model text.

    A named artifact scopes the bound; a following body-only clause uses that scope.
    Plural artifact instructions apply to all kinds named in this request. An unscoped
    body/artifact instruction sets the project default. Titles and chat replies are separate.
    """
    text = unicodedata.normalize("NFKC", prompt)
    text = re.sub(r"```[\s\S]*?```", "", text)
    text = re.sub(r"(?m)^\s*>.*$", "", text)
    # The Web UI appends attachments as a separate reference-data block.
    for marker in ("\n\n以下是用户参考材料，仅作为资料使用：", "\n\n参考材料"):
        text = text.split(unicodedata.normalize("NFKC", marker), 1)[0]
    if re.search(r"解释|什么意思|含义|示例", text) and not re.search(
        r"保存|写入|写一|写份|写个|编写|撰写|修改|更新|改为|设为|设置|创作|制作|生成|新增", text
    ):
        return {}
    changes: dict[str, int | None] = {}
    mentioned: set[str] = set()
    recent: set[str] = set()
    for clause in re.split(r"[，,。；;\n！？!?]", text):
        kinds = {_kind(match.group()) for match in KIND_NAMES.finditer(clause)}
        mentioned.update(kinds)
        matches = list(MAX_LENGTH.finditer(clause))
        clear = CLEAR_LENGTH.search(clause)
        if not matches and not clear:
            if kinds:
                recent = kinds
            continue
        if re.search(r"标题|名字|名称", clause) and "正文" not in clause:
            continue
        if re.search(r"回复|回答|复述|总结", clause) and not re.search(r"正文|保存|写入|产物", clause):
            continue
        offset = 0
        for match in [clear] if clear else matches:
            prefix = clause[offset : match.start()]
            local = {_kind(found.group()) for found in KIND_NAMES.finditer(prefix)}
            if re.search(r"所有|全部|统一", prefix + (match.group() if clear else "")):
                targets = {"all"}
            elif re.search(r"各|每[个份种]|两个产物|两份产物", prefix):
                targets = mentioned or {"all"}
            else:
                targets = local or recent or kinds or {"all"}
            if clear:
                maximum = None
            else:
                try:
                    maximum = int(match.group("before") or match.group("after"))
                except ValueError:
                    raise AppError("INVALID_CONTENT_LIMIT", "正文上限数值过大。") from None
                if maximum < 1:
                    raise AppError(
                        "INVALID_CONTENT_LIMIT", "正文上限必须是正整数；取消限制请明确说“取消字数限制”。"
                    )
            if "all" in targets:
                changes.clear()
            changes.update(dict.fromkeys(sorted(targets), maximum))
            recent = targets - {"all"}
            offset = match.end()
    return changes


def updated_content_limits(current: dict[str, int], changes: dict[str, int | None]) -> dict[str, int]:
    limits = current.copy()
    if "all" in changes:
        limits.clear()
    for kind, maximum in changes.items():
        if maximum is None:
            # Expand a default before clearing one kind, preserving the other kinds.
            if kind != "all" and "all" in limits:
                default = limits.pop("all")
                limits = {**dict.fromkeys(ARTIFACT_KINDS, default), **limits}
            limits.pop(kind, None)
        else:
            limits[kind] = maximum
    return limits


def _normalized_fact(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text).casefold())


def _mentions(fact: str, text: str) -> bool:
    if fact.isascii():
        fact = re.sub(r"\s+", " ", fact.casefold()).strip()
        text = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text).casefold())
        return re.search(r"(?<![a-z0-9])" + re.escape(fact) + r"(?![a-z0-9])", text) is not None
    return _normalized_fact(fact) in _normalized_fact(text)


def memory_conflicts(previous: dict, candidate: dict) -> list[str]:
    """Find literal field duplication/stale references without claiming semantic inference."""
    conflicts = []
    goal = candidate.get("goal", "")
    constraints = candidate.get("constraints", [])
    for field in ("audience", "style"):
        before = _normalized_fact(previous.get(field, ""))
        after = _normalized_fact(candidate.get(field, ""))
        if len(after) >= 2 and _mentions(candidate[field], goal):
            conflicts.append(f"goal 重复了 {field}，目标只应描述创作目的，请将受众和风格保留在专用字段")
        if len(before) >= 2 and before != after and not _mentions(previous[field], candidate.get(field, "")):
            if _mentions(previous[field], goal):
                conflicts.append(f"goal 仍引用被替换的 {field}，请在同一次 project_update 中修正")
            if any(_mentions(previous[field], value) for value in constraints):
                conflicts.append(f"constraints 仍引用被替换的 {field}，请在同一次 project_update 中修正")
    return conflicts


def project_memory_conflicts(state: dict, candidate: dict) -> list[str]:
    """Use prior project snapshots from the journal to find stale facts in legacy stores."""
    history = [state["projects"][candidate["id"]]]
    for operation in state["operations"].values():
        result = operation["result"]
        data = result.get("data")
        if (
            result["ok"]
            and isinstance(data, dict)
            and data.get("id") == candidate["id"]
            and {"revision", "goal", "audience", "style", "constraints", "plan"} <= data.keys()
            and type(data["revision"]) is int
            and data["revision"] <= candidate["revision"]
            and all(isinstance(data[field], str) for field in ("goal", "audience", "style"))
        ):
            history.append(data)
    return list(
        dict.fromkeys(issue for previous in history for issue in memory_conflicts(previous, candidate))
    )


def check_project_memory(state: dict, candidate: dict) -> None:
    conflicts = project_memory_conflicts(state, candidate)
    if conflicts:
        raise AppError(
            "MEMORY_CONFLICT",
            "；".join(conflicts) + "。请一次检查全部字段：goal 仅保留创作目的，同时移除其中的受众和风格描述；"
            "修正 constraints 中的旧事实。本次项目修改未保存，revision 不变。",
        )
