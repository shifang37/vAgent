import hashlib
import re
import stat
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from vagent.errors import AppError
from vagent.tools import Arguments, ToolDefinition, ToolRegistry

SKILL_LIMIT = 32
SKILL_MAX_BYTES = 16384


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
    """One immutable skill snapshot plus the isolated user-root diagnostics."""

    def __init__(self, skills: dict[str, dict], diagnostics: list[dict] | None = None):
        self._skills = skills
        self.diagnostics = list(diagnostics or [])

    @classmethod
    def discover(cls, root: Path | None = None, *, user_root: Path | None = None) -> "SkillCatalog":
        """Discover skills from an explicit root or the default builtin + user roots.

        An explicit root (``VAGENT_SKILLS_DIR``) keeps the original full-replacement
        and fail-fast semantics. Without it, the packaged root is strict and
        ``VAGENT_HOME/skills`` is merged after it: builtin names win, bad user
        files are isolated with a diagnostic, and the combined catalog is bounded.
        """
        if root is not None:
            results = cls._scan(Path(root), strict=True)
            return cls({result["entry"]["name"]: result["entry"] for result in results if result["entry"]})
        results = cls._scan(builtin_skills_root(), strict=True)
        skills = {result["entry"]["name"]: result["entry"] for result in results if result["entry"]}
        diagnostics = []
        if user_root is not None:
            cls._merge_user_root(Path(user_root), skills, diagnostics)
        return cls(skills, diagnostics)

    @classmethod
    def _merge_user_root(cls, root: Path, skills: dict[str, dict], diagnostics: list[dict]) -> None:
        if not root.exists() and not is_link(root):
            return  # A missing user root is an empty extension point.
        for result in sorted(cls._scan(root, strict=False), key=lambda item: item["name"]):
            if result["entry"] is None:
                if result["code"]:
                    diagnostics.append(
                        {"code": result["code"], "name": result["name"], "message": result["message"]}
                    )
                continue
            name = result["entry"]["name"]
            if name in skills:
                diagnostics.append(
                    {
                        "code": "SKILL_NAME_CONFLICT",
                        "name": name,
                        "message": "用户 skill 与已加载的 skill 重名，已忽略；内置名称优先。",
                    }
                )
                continue
            if len(skills) >= SKILL_LIMIT:
                diagnostics.append(
                    {
                        "code": "SKILL_LIMIT",
                        "name": name,
                        "message": f"有效 skill 已达 {SKILL_LIMIT} 个，本条未加载。",
                    }
                )
                continue
            skills[name] = result["entry"]

    @classmethod
    def _scan(cls, root: Path, *, strict: bool) -> list[dict]:
        results: list[dict] = []
        if is_link(root) or not root.is_dir():
            results.append(cls._failure("root", "INVALID_SKILL_ROOT", "Skill 根目录必须是普通目录。"))
            if strict:
                raise AppError("INVALID_SKILL_ROOT", "Skill 根目录必须是普通目录。")
            return results
        directories = []
        for entry in sorted(root.iterdir()):
            if is_link(entry):
                results.append(
                    cls._failure(entry.name, "INVALID_SKILL_FILE", "Skill 目录不允许符号链接或目录联接。")
                )
                if strict:
                    raise AppError("INVALID_SKILL_FILE", "Skill 目录不允许符号链接或目录联接。")
                continue
            if entry.is_dir():
                directories.append(entry)
        if strict and len(directories) > SKILL_LIMIT:
            raise AppError("SKILL_LIMIT", f"首版最多加载 {SKILL_LIMIT} 个 skill。")
        for directory in directories:
            results.append(cls._scan_skill(directory, strict=strict))
        return results

    @classmethod
    def _scan_skill(cls, directory: Path, *, strict: bool) -> dict:
        def fail(code: str, message: str) -> dict:
            if strict:
                raise AppError(code, message)
            return cls._failure(directory.name, code, message)

        file = directory / "SKILL.md"
        if is_link(file):
            return fail("INVALID_SKILL_FILE", "Skill 文件不允许符号链接。")
        if not file.exists():
            return {"name": directory.name, "entry": None, "code": None, "message": None}
        if not file.is_file() or file.stat().st_size > SKILL_MAX_BYTES:
            return fail("INVALID_SKILL_FILE", "Skill 必须是不超过 16 KiB 的普通文件。")
        try:
            raw = file.read_bytes()
            if len(raw) > SKILL_MAX_BYTES:
                raise ValueError("Skill file too large")
            source = raw.decode("utf-8")
            match = re.fullmatch(r"---\r?\n(.*?)\r?\n---\r?\n(.*)", source, re.DOTALL)
            if not match:
                raise ValueError("Missing frontmatter")
            metadata = Metadata.model_validate(yaml.load(match[1], Loader=UniqueLoader))
            if metadata.name != directory.name or not match[2].strip():
                raise ValueError("Invalid directory name or empty instructions")
        except (ValueError, TypeError, yaml.YAMLError, ValidationError, OSError):
            return fail("INVALID_SKILL", "Skill 的 name、description、目录名或正文不符合要求。")
        return {
            "name": metadata.name,
            "entry": {
                **metadata.model_dump(),
                "version": hashlib.sha256(raw).hexdigest()[:12],
                "instructions": match[2].strip(),
            },
            "code": None,
            "message": None,
        }

    @staticmethod
    def _failure(name: str, code: str, message: str) -> dict:
        return {"name": name, "entry": None, "code": code, "message": message}

    def list(self) -> list[dict]:
        return [{k: v for k, v in skill.items() if k != "instructions"} for skill in self._skills.values()]

    def read(self, name: str) -> dict:
        if name not in self._skills:
            raise AppError("SKILL_NOT_FOUND", "没有这个已注册的 skill，请从元信息列表中选择。")
        return dict(self._skills[name])


def discover_for(config) -> SkillCatalog:
    """The catalog shared by the CLI, the Web service and headless planning."""
    user_root = None if config.skills_root else Path(config.home) / "skills"
    return SkillCatalog.discover(config.skills_root, user_root=user_root)


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
