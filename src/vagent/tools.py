"""Tool allowlist, shared JSON schemas, and project-scoped execution."""

import copy
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Annotated, Literal
from uuid import uuid4

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from pydantic.alias_generators import to_camel

from vagent.errors import AppError, failure, public_error
from vagent.quality import check_project_memory, content_check, project_memory_conflicts
from vagent.storage import FileStore, now
from vagent.waiting import DeferredToolResult, ToolExecutionContext


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, alias_generator=to_camel)


class ProjectUpdate(Arguments):
    expected_revision: int = Field(ge=0)
    goal: str | None = Field(
        default=None,
        max_length=2000,
        description="仅写创作目的或核心信息，例如“提升品牌认知和到店意愿”。"
        "不要包含 audience 或 style 的描述；“提升某人群的品牌认知”中的人群限定也必须移除。",
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
    steps: list[Step] = Field(
        max_length=12,
        description="简短的实际交付清单；不把读取 Skill、维护清单或最终回复列为子任务。",
    )


ArtifactId = Annotated[
    str, Field(pattern=r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
]


class ArtifactSave(Arguments):
    artifact_id: ArtifactId | None = None
    expected_version: int | None = Field(default=None, ge=1)
    kind: Literal["brief", "script", "storyboard"]
    title: str = Field(min_length=1, max_length=120)
    content: str = Field(
        min_length=1,
        max_length=20000,
        description="完整正文。遵守项目 contentLimits[kind]，非空白字符含标点、英文、数字和 Markdown。"
        "只有上限、没有最低字数要求时，初稿按上限约七成组织并保留必需信息，以工具实际计数为准。",
    )

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
    # Context handlers own their journal/transaction and may return a deferred
    # marker. Legacy handlers still return raw data for FileStore.operation.
    context_execute: Callable[[dict, FileStore, ToolExecutionContext], dict | DeferredToolResult] | None = (
        None
    )
    feature: str | None = None


@dataclass(frozen=True)
class ToolFeature:
    name: str
    configuration: dict
    instructions: str
    bypass_answer_cache: bool = False


class ToolRegistry:
    def __init__(self):
        self._definitions: dict[str, ToolDefinition] = {}
        self._features: dict[str, ToolFeature] = {}
        self.wait_resolvers: dict[str, Callable] = {}
        self._resolver_features: dict[str, str | None] = {}
        self._execution_variants: dict[int, tuple[list[ToolFeature], dict[str, str]]] = {}

    def register_wait_resolver(self, kind: str, resolve: Callable, *, feature=None) -> "ToolRegistry":
        if kind in self.wait_resolvers:
            raise ValueError(f"Duplicate wait resolver: {kind}")
        self.wait_resolvers[kind] = resolve
        self._resolver_features[kind] = feature
        return self

    def register_execution_variant(self, version: int, *, features, descriptions) -> None:
        """Keep historical prompts/specs without teaching the Runner domain rules."""
        old_features, old_descriptions = self._execution_variants.get(version, ([], {}))
        self._execution_variants[version] = (
            [*old_features, *copy.deepcopy(features)],
            {**old_descriptions, **descriptions},
        )

    def for_execution_version(self, version: int) -> "ToolRegistry":
        registry = self._filtered(lambda _: True)
        features, descriptions = self._execution_variants.get(version, ([], {}))
        for feature in features:
            if feature.name in registry._features:
                registry._features[feature.name] = copy.deepcopy(feature)
        for name, description in descriptions.items():
            if name in registry._definitions:
                registry._definitions[name] = replace(registry._definitions[name], description=description)
        return registry

    def register(self, definition: ToolDefinition) -> "ToolRegistry":
        if definition.name in self._definitions:
            raise ValueError(f"Duplicate tool: {definition.name}")
        if definition.async_execute and definition.effect != "read":
            raise ValueError("External tools must be explicitly read-only")
        if (
            sum(
                handler is not None
                for handler in (definition.execute, definition.async_execute, definition.context_execute)
            )
            != 1
        ):
            raise ValueError("A tool must have exactly one execution handler")
        if definition.feature and definition.feature not in self._features:
            raise ValueError("Register the tool feature before its tools")
        self._definitions[definition.name] = definition
        return self

    def register_feature(self, feature: ToolFeature) -> "ToolRegistry":
        if feature.name in self._features:
            raise ValueError(f"Duplicate tool feature: {feature.name}")
        self._features[feature.name] = copy.deepcopy(feature)
        return self

    def _active_features(self) -> list[ToolFeature]:
        names = {definition.feature for definition in self._definitions.values()}
        return [feature for name, feature in sorted(self._features.items()) if name in names]

    @property
    def features(self) -> dict[str, dict]:
        return {feature.name: copy.deepcopy(feature.configuration) for feature in self._active_features()}

    @property
    def instructions(self) -> str:
        return "\n".join(feature.instructions for feature in self._active_features())

    @property
    def bypass_answer_cache(self) -> bool:
        return any(feature.bypass_answer_cache for feature in self._active_features())

    def requires_context(self, name: str) -> bool:
        definition = self._definitions.get(name)
        return definition is not None and definition.context_execute is not None

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
        return self._filtered(lambda definition: definition.effect == "read")

    def without_feature(self, name: str) -> "ToolRegistry":
        return self._filtered(lambda definition: definition.feature != name)

    def _filtered(self, include: Callable[[ToolDefinition], bool]) -> "ToolRegistry":
        registry = ToolRegistry()
        registry._features = copy.deepcopy(self._features)
        registry._execution_variants = copy.deepcopy(self._execution_variants)
        for definition in self._definitions.values():
            if include(definition):
                registry.register(definition)
        active = {definition.feature for definition in registry._definitions.values()}
        for kind, resolver in self.wait_resolvers.items():
            feature = self._resolver_features[kind]
            if feature is None or feature in active:
                registry.register_wait_resolver(kind, resolver, feature=feature)
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
        self,
        name: str,
        args: object,
        *,
        store: FileStore,
        project_id: str,
        operation_key: str,
        context: ToolExecutionContext | None = None,
    ) -> dict | DeferredToolResult:
        definition = self._definitions.get(name)
        if definition is None or definition.async_execute is None:
            return self.execute(
                name, args, store=store, project_id=project_id, operation_key=operation_key, context=context
            )
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
        self,
        name: str,
        args: object,
        *,
        store: FileStore,
        project_id: str,
        operation_key: str,
        context: ToolExecutionContext | None = None,
    ) -> dict | DeferredToolResult:
        definition = self._definitions.get(name)
        if definition is None:
            return failure("UNKNOWN_TOOL", "该工具未注册。")
        if definition.async_execute:
            return failure("ASYNC_TOOL", "外部工具必须通过异步执行入口调用。")
        if definition.context_execute and not isinstance(args, dict):
            return failure("INVALID_ARGUMENTS", "工具参数必须是 JSON 对象。")
        if definition.context_execute and next(
            Draft202012Validator(definition.schema.model_json_schema(by_alias=True)).iter_errors(args), None
        ):
            return failure("INVALID_ARGUMENTS", "工具参数不符合已公布的 JSON Schema。")
        try:
            parsed = definition.schema.model_validate(args)
        except ValidationError as error:
            # Never include rejected input values in model-visible validation errors.
            fields = [".".join(str(p) for p in issue["loc"]) or "arguments" for issue in error.errors()]
            return failure("INVALID_ARGUMENTS", "参数格式不符合 Schema：" + ", ".join(fields)[:1000])
        payload = parsed.model_dump(by_alias=True, exclude_none=True)
        try:
            if definition.context_execute:
                with store.locked():
                    snapshot = store.snapshot()
                    self._check_context(context, snapshot, project_id, operation_key)
                    if definition.effect == "write" and snapshot["runs"][context.run_id].get("readOnly"):
                        return failure("READ_ONLY", "只读 Run 不能调用写工具。")
                    # Keep the original JSON encoding for per-call replay checks;
                    # semantic request normalization belongs to the domain service.
                    return definition.context_execute(copy.deepcopy(args), store, context)
            return store.operation(
                operation_key, name, payload, lambda draft: definition.execute(payload, draft, project_id)
            )
        except Exception as error:
            safe = public_error(error)
            return failure(safe.code, str(safe))

    @staticmethod
    def _check_context(context, state: dict, project_id: str, operation_key: str) -> None:
        if not isinstance(context, ToolExecutionContext):
            raise AppError("TOOL_CONTEXT_INVALID", "工具缺少服务端执行上下文。")
        run = state["runs"].get(context.run_id)
        if (
            context.project_id != project_id
            or context.project_id != context.session_id
            or context.operation_key != operation_key
            or context.project_id not in state["projects"]
            or context.session_id not in state["sessions"]
            or run is None
            or run["sessionId"] != context.session_id
        ):
            raise AppError("TOOL_CONTEXT_INVALID", "工具的项目、会话、Run 或调用标识不匹配。")


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
