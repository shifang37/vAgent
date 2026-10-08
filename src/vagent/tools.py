"""Tool allowlist, shared JSON schemas, and project-scoped execution."""

import copy
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Literal
from uuid import uuid4

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from pydantic.alias_generators import to_camel

from vagent.errors import AppError, failure, public_error
from vagent.quality import check_project_memory, content_check, project_memory_conflicts
from vagent.storage import FileStore, now


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, alias_generator=to_camel)


class ProjectUpdate(Arguments):
    expected_revision: int = Field(ge=0)
    goal: str | None = Field(
        default=None, max_length=2000, description="创作目的与核心信息，不重复受众或风格。"
    )
    audience: str | None = Field(default=None, max_length=500, description="受众的唯一事实来源。")
    style: str | None = Field(default=None, max_length=500, description="风格的唯一事实来源。")
    constraints: list[Annotated[str, Field(max_length=500)]] | None = Field(default=None, max_length=20)

    @model_validator(mode="after")
    def reject_null_updates(self):
        if any(getattr(self, name) is None for name in self.model_fields_set):
            raise ValueError("修改字段不能为 null；不修改的字段请省略")
        return self


class Step(Arguments):
    text: str = Field(min_length=1, max_length=200)
    status: Literal["pending", "in_progress", "completed"]


class PlanUpdate(Arguments):
    steps: list[Step] = Field(max_length=12)


ArtifactId = Annotated[
    str, Field(pattern=r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
]


class ArtifactSave(Arguments):
    artifact_id: ArtifactId | None = None
    expected_version: int | None = Field(default=None, ge=1)
    kind: Literal["brief", "script", "storyboard"]
    title: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=20000)

    @model_validator(mode="after")
    def require_version(self):
        if bool(self.artifact_id) != bool(self.expected_version):
            raise ValueError("修改时必须同时提供 artifactId 和 expectedVersion")
        return self


class ArtifactRead(Arguments):
    artifact_id: ArtifactId
    version: int | None = Field(default=None, ge=1)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=4000, ge=1, le=8000)


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    schema: type[BaseModel] | dict
    effect: Literal["read", "write"]
    execute: Callable[[dict, dict, str], object] | None = None
    async_execute: Callable[[dict], Awaitable[object]] | None = None
    identity: str | None = None


class ToolRegistry:
    def __init__(self):
        self._definitions: dict[str, ToolDefinition] = {}

    def register(self, definition: ToolDefinition) -> "ToolRegistry":
        if definition.name in self._definitions:
            raise ValueError(f"Duplicate tool: {definition.name}")
        if definition.async_execute and definition.effect != "read":
            raise ValueError("External tools must be explicitly read-only")
        self._definitions[definition.name] = definition
        return self

    def specs(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": definition.name,
                    "description": definition.description,
                    "parameters": copy.deepcopy(definition.schema)
                    if isinstance(definition.schema, dict)
                    else definition.schema.model_json_schema(by_alias=True),
                },
            }
            for definition in self._definitions.values()
        ]

    def read_only(self) -> "ToolRegistry":
        registry = ToolRegistry()
        for definition in self._definitions.values():
            if definition.effect == "read":
                registry.register(definition)
        return registry

    @property
    def identities(self) -> dict[str, str]:
        return {d.name: d.identity for d in self._definitions.values() if d.identity}

    def inventory(self) -> list[dict]:
        return [
            {
                "name": d.name,
                "description": d.description,
                "effect": d.effect,
                "source": "mcp" if d.async_execute else "builtin",
            }
            for d in self._definitions.values()
        ]

    async def aexecute(
        self, name: str, args: object, *, store: FileStore, project_id: str, operation_key: str
    ) -> dict:
        definition = self._definitions.get(name)
        if definition is None or definition.async_execute is None:
            return self.execute(name, args, store=store, project_id=project_id, operation_key=operation_key)
        if not isinstance(args, dict) or not isinstance(definition.schema, dict):
            return failure("INVALID_ARGUMENTS", "MCP 工具参数必须是 JSON 对象。")
        if next(Draft202012Validator(definition.schema).iter_errors(args), None):
            return failure("INVALID_ARGUMENTS", "MCP 工具参数不符合已发现的 Schema。")
        payload = copy.deepcopy(args)
        previous = store.operation_result(operation_key, name, payload)
        if previous is not None:
            return previous
        try:
            result = await definition.async_execute(payload)
        except Exception as error:
            safe = public_error(error)

            def fail(_):
                raise safe

            return store.operation(operation_key, name, payload, fail)
        return store.operation(operation_key, name, payload, lambda _: result)

    def execute(
        self, name: str, args: object, *, store: FileStore, project_id: str, operation_key: str
    ) -> dict:
        definition = self._definitions.get(name)
        if definition is None:
            return failure("UNKNOWN_TOOL", "该工具未注册。")
        if definition.async_execute:
            return failure("ASYNC_TOOL", "外部工具必须通过异步执行入口调用。")
        try:
            parsed = definition.schema.model_validate(args)
        except ValidationError as error:
            # Never include rejected input values in model-visible validation errors.
            fields = [".".join(str(p) for p in issue["loc"]) or "arguments" for issue in error.errors()]
            return failure("INVALID_ARGUMENTS", "参数格式不符合 Schema：" + ", ".join(fields)[:1000])
        payload = parsed.model_dump(by_alias=True, exclude_none=True)
        try:
            return store.operation(
                operation_key, name, payload, lambda draft: definition.execute(payload, draft, project_id)
            )
        except Exception as error:
            safe = public_error(error)
            return failure(safe.code, str(safe))


def _project_read(args: dict, state: dict, project_id: str) -> dict:
    artifacts = [a for a in state["artifacts"].values() if a["projectId"] == project_id]
    return {
        **state["projects"][project_id],
        "memoryConflicts": project_memory_conflicts(state, state["projects"][project_id]),
        "artifactCount": len(artifacts),
        "artifacts": [
            {
                "id": a["id"],
                "kind": a["kind"],
                **{k: v for k, v in a["versions"][-1].items() if k != "content"},
            }
            for a in artifacts[-20:]
        ],
    }


def _project_update(args: dict, state: dict, project_id: str) -> dict:
    project = state["projects"][project_id]
    if project["revision"] != args["expectedRevision"]:
        raise AppError("REVISION_CONFLICT", "项目版本已变化，请重新读取后再修改。")
    candidate = {**project, **{k: v for k, v in args.items() if k != "expectedRevision"}}
    check_project_memory(state, candidate)
    project.update(candidate)
    project["revision"] += 1
    return project


def _plan_update(args: dict, state: dict, project_id: str) -> dict:
    state["projects"][project_id]["plan"] = args["steps"]
    state["projects"][project_id]["revision"] += 1
    return args


def _artifact_save(args: dict, state: dict, project_id: str) -> dict:
    artifact_id = args.get("artifactId") or str(uuid4())
    existing = state["artifacts"].get(artifact_id)
    if args.get("artifactId") and (not existing or existing["projectId"] != project_id):
        raise AppError("NOT_FOUND", "当前项目中没有该产物。")
    if existing and (
        existing["versions"][-1]["version"] != args.get("expectedVersion") or existing["kind"] != args["kind"]
    ):
        raise AppError("VERSION_CONFLICT", "产物版本或类型不匹配，请重新读取。")
    project = state["projects"][project_id]
    check_project_memory(state, project)
    limits = project.get("contentLimits", {})
    checked = content_check(args["content"], limits.get(args["kind"], limits.get("all")))
    artifact = existing or {"id": artifact_id, "projectId": project_id, "kind": args["kind"], "versions": []}
    version = len(artifact["versions"]) + 1
    artifact["versions"].append(
        {
            "version": version,
            "title": args["title"],
            "content": args["content"],
            "createdAt": now(),
            "contentCheck": checked,
        }
    )
    state["artifacts"][artifact_id] = artifact
    return {
        "artifactId": artifact_id,
        "version": version,
        "title": args["title"],
        "persisted": True,
        "contentCheck": checked,
    }


def _artifact_read(args: dict, state: dict, project_id: str) -> dict:
    artifact = state["artifacts"].get(args["artifactId"])
    if not artifact or artifact["projectId"] != project_id:
        raise AppError("NOT_FOUND", "当前项目中没有该产物。")
    selected = (
        next((v for v in artifact["versions"] if v["version"] == args["version"]), None)
        if args.get("version")
        else artifact["versions"][-1]
    )
    if selected is None:
        raise AppError("NOT_FOUND", "没有该版本。")
    start, end = args["offset"], args["offset"] + args["limit"]
    return {
        "artifactId": artifact["id"],
        **selected,
        "content": selected["content"][start:end],
        "totalCharacters": len(selected["content"]),
        "nextOffset": end if end < len(selected["content"]) else None,
    }


def create_project_tools() -> ToolRegistry:
    definitions = [
        ToolDefinition(
            "project_read",
            "读取当前项目、revision、memoryConflicts 和最近 20 个产物的元信息；"
            "写入前先读取并修复记忆冲突。contentLimits 是后端强制的正文上限。",
            Arguments,
            "read",
            _project_read,
        ),
        ToolDefinition(
            "project_update",
            "更新用户确认的事实，使用当前 expectedRevision；goal 只写目的，audience/style 分别保存受众/风格。"
            "改变受众或风格时同步清理 goal/constraints 中的旧描述，冲突会整次拒绝；未传字段保持原值。"
            "contentLimits 由用户的字数要求设置，工具不能修改。",
            ProjectUpdate,
            "write",
            _project_update,
        ),
        ToolDefinition(
            "plan_update",
            "保存复杂任务的简短执行清单，会增加项目 revision；简单问题无需调用。"
            "同一批需要更新事实和计划时先调用 project_update，再调用 plan_update；后续更新使用最新 revision。",
            PlanUpdate,
            "write",
            _plan_update,
        ),
        ToolDefinition(
            "artifact_save",
            "保存方案、脚本或文本分镜；修改时同时传 artifactId 和 expectedVersion，保留原版。"
            "正文按非空白 Unicode 字符计数（含标点、英文、数字、Markdown 标记，不含 title），"
            "遵守项目 contentLimits，超限返回 CONTENT_LENGTH 且不保存；成功返回实际 contentCheck。",
            ArtifactSave,
            "write",
            _artifact_save,
        ),
        ToolDefinition(
            "artifact_read",
            "按 ID 读取当前项目的产物，可指定历史 version 和 offset/limit 分段读取长内容。",
            ArtifactRead,
            "read",
            _artifact_read,
        ),
    ]
    registry = ToolRegistry()
    for definition in definitions:
        registry.register(definition)
    return registry
