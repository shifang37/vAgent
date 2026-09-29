import hashlib
import re
import stat
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from vagent.errors import AppError
from vagent.tools import Arguments, ToolDefinition, ToolRegistry


class Metadata(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    description: str = Field(min_length=1, max_length=600)


class SkillRead(Arguments):
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")


class UniqueLoader(yaml.SafeLoader):
    """Reject duplicate keys and aliases in small, trusted skill frontmatter."""

    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise ValueError("YAML aliases are not supported")
        return super().compose_node(parent, index)

    def construct_mapping(self, node, deep=False):
        keys = [self.construct_object(key, deep=deep) for key, _ in node.value]
        if len(set(keys)) != len(keys):
            raise ValueError("Duplicate metadata key")
        return super().construct_mapping(node, deep=deep)


def builtin_skills_root() -> Path:
    packaged = Path(__file__).parent / "builtin_skills"
    return packaged if packaged.is_dir() else Path(__file__).parents[2] / "skills"


def is_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except FileNotFoundError:
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


class SkillCatalog:
    def __init__(self, skills: dict[str, dict]):
        self._skills = skills

    @classmethod
    def discover(cls, root: Path | None = None) -> "SkillCatalog":
        root = root or builtin_skills_root()
        if is_link(root) or not root.is_dir():
            raise AppError("INVALID_SKILL_ROOT", "Skill 根目录必须是普通目录。")
        directories = []
        for entry in sorted(root.iterdir()):
            if is_link(entry):
                raise AppError("INVALID_SKILL_FILE", "Skill 目录不允许符号链接或目录联接。")
            if entry.is_dir():
                directories.append(entry)
        if len(directories) > 32:
            raise AppError("SKILL_LIMIT", "首版最多加载 32 个 skill。")
        skills = {}
        for directory in directories:
            file = directory / "SKILL.md"
            if is_link(file):
                raise AppError("INVALID_SKILL_FILE", "Skill 文件不允许符号链接。")
            if not file.exists():
                continue
            if not file.is_file() or file.stat().st_size > 16384:
                raise AppError("INVALID_SKILL_FILE", "Skill 必须是不超过 16 KiB 的普通文件。")
            try:
                raw = file.read_bytes()
                if len(raw) > 16384:
                    raise AppError("INVALID_SKILL_FILE", "Skill 文件超过 16 KiB。")
                source = raw.decode("utf-8")
                match = re.fullmatch(r"---\r?\n(.*?)\r?\n---\r?\n(.*)", source, re.DOTALL)
                if not match:
                    raise ValueError("Missing frontmatter")
                metadata = Metadata.model_validate(yaml.load(match[1], Loader=UniqueLoader))
                if metadata.name != directory.name or not match[2].strip():
                    raise ValueError("Invalid directory name or empty instructions")
            except (ValueError, TypeError, yaml.YAMLError, ValidationError):
                raise AppError(
                    "INVALID_SKILL", "Skill 的 name、description、目录名或正文不符合要求。"
                ) from None
            skills[metadata.name] = {
                **metadata.model_dump(),
                "version": hashlib.sha256(raw).hexdigest()[:12],
                "instructions": match[2].strip(),
            }
        return cls(skills)

    def list(self) -> list[dict]:
        return [{k: v for k, v in skill.items() if k != "instructions"} for skill in self._skills.values()]

    def read(self, name: str) -> dict:
        if name not in self._skills:
            raise AppError("SKILL_NOT_FOUND", "没有这个已注册的 skill，请从元信息列表中选择。")
        return dict(self._skills[name])


def register_skill_tool(registry: ToolRegistry, catalog: SkillCatalog) -> ToolRegistry:
    return registry.register(
        ToolDefinition(
            "skill_read",
            "按名称读取已注册 skill 的说明；任务相关时按需调用。",
            SkillRead,
            "read",
            lambda args, _state, _project: catalog.read(args["name"]),
        )
    )
