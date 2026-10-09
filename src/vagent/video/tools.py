"""Scoped video tools. Registration and waiting never call the provider."""

from datetime import datetime, timedelta
from uuid import uuid4

from vagent.contracts import Identifier
from vagent.errors import AppError, failure
from vagent.storage import FileStore
from vagent.tools import Arguments, ToolDefinition, ToolFeature, ToolRegistry
from vagent.video.contracts import Job, VideoRequest
from vagent.video.jobs import JobService
from vagent.video.views import job_snapshot
from vagent.waiting import ExternalResourceRef, ToolExecutionContext, ToolResultError, WaitBinding

VIDEO_TOOLS_VERSION = 2
VIDEO_RULES_VERSION = 2
WAIT_TIMEOUT_SECONDS = 600

LEGACY_VIDEO_RULES = """当前启用 mock 视频模式，只能登记模拟任务；所有结果均为 simulated=true、mediaAvailable=false。
需要生成时先调用 video_capabilities，根据返回的模型、能力版本和完整规格组合构造 video_generate。
sourceRefs 可省略；引用文本产物时先读取并使用工具确认的 artifactId 和确切 version。
video_generate 只返回本地 jobId 与登记状态；每个 Run 最多新建一个 Job，不能自动降低规格、拆分或重提。
已登记不等于已完成。当前入口不会自动推进 Job，也不能挂起等待未完成任务；先报告 jobId 与真实状态。
job_get 可读取本地状态，await_job 可取得已有终态；不要循环查询。提交不确定或查询暂停时明确报告原因。
成功也只是模拟结果描述，没有可播放或下载的媒体；不能声称已生成真实视频。"""

VIDEO_RULES = LEGACY_VIDEO_RULES.replace(
    "已登记不等于已完成。当前入口不会自动推进 Job，也不能挂起等待未完成任务；先报告 jobId 与真实状态。\n"
    "job_get 可读取本地状态，await_job 可取得已有终态；不要循环查询。提交不确定或查询暂停时明确报告原因。",
    "已登记不等于已完成。可以先报告 jobId 与真实状态；需要等待结果时调用 await_job，Run 会持久挂起。\n"
    "Job 由独立 Worker 推进；等待不会请求模型或占用活动时间，结果就绪后续接原调用。\n"
    "job_get 读取本地状态；不要循环查询。等待到期、提交不确定或查询暂停时明确报告原因。",
)

LEGACY_AWAIT_DESCRIPTION = (
    "读取当前项目 Job 的终态；未结束时保存持久等待并返回通用延迟标记。"
    "当前执行器尚不支持挂起，遇到未完成任务会结束本次 Run；不要用它轮询。"
)


class JobLookup(Arguments):
    job_id: Identifier


def completed_job_data(job: Job) -> dict:
    """Raw success data or a bounded error, for immediate waits and the B3 resolver."""
    if job.status == "succeeded":
        return job_snapshot(job)
    if job.status == "failed":
        raise AppError("JOB_FAILED", f"任务 {job.id} 已失败（{job.error.stage}/{job.error.code}）。")
    if job.status == "unknown":
        raise AppError("JOB_SUBMISSION_UNKNOWN", f"任务 {job.id} 的提交结果不确定，不会自动重新提交。")
    if job.query_state == "paused":
        raise AppError("JOB_QUERY_PAUSED", f"任务 {job.id} 的查询已暂停；生成状态未变，需要恢复原任务查询。")
    raise AppError("JOB_NOT_READY", "视频任务尚未结束。")


def resolve_job_wait(store: FileStore, binding: WaitBinding) -> dict | None:
    """Resolve local facts even when the original adapter/model is unavailable."""
    raw = store.snapshot()["jobs"].get(binding.resource.id)
    if raw is None or raw["context"]["projectId"] != binding.context.project_id:
        return failure("JOB_NOT_FOUND", "没有这个视频任务。")
    try:
        return {"ok": True, "data": completed_job_data(Job.model_validate(raw))}
    except AppError as error:
        if error.code == "JOB_NOT_READY":
            return None
        return failure(error.code, str(error))


def register_video_tools(registry: ToolRegistry, service: JobService) -> ToolRegistry:
    capabilities = [item.model_dump(mode="json", by_alias=True) for item in service.capabilities()]
    if not capabilities or any(item["mode"] != "mock" for item in capabilities):
        raise AppError("VIDEO_PROVIDER_UNAVAILABLE", "mock 模式需要已配置的模拟视频适配器。")
    registry.register_feature(
        ToolFeature(
            name="video",
            configuration={
                "mode": "mock",
                "toolsVersion": VIDEO_TOOLS_VERSION,
                "rulesVersion": VIDEO_RULES_VERSION,
                "capabilities": capabilities,
            },
            instructions=VIDEO_RULES,
            bypass_answer_cache=True,
        )
    )
    registry.register_execution_variant(
        1,
        features=[
            ToolFeature(
                name="video",
                configuration={
                    "mode": "mock",
                    "toolsVersion": 1,
                    "rulesVersion": 1,
                    "capabilities": capabilities,
                },
                instructions=LEGACY_VIDEO_RULES,
                bypass_answer_cache=True,
            )
        ],
        descriptions={"await_job": LEGACY_AWAIT_DESCRIPTION},
    )
    registry.register_wait_resolver(
        "job", lambda binding: resolve_job_wait(service.store, binding), feature="video"
    )

    def check_store(store: FileStore):
        if store is not service.store:
            raise AppError("TOOL_CONTEXT_INVALID", "视频工具与当前执行存储不匹配。")

    def read_capabilities(args, store, context):
        check_store(store)
        return store.operation(
            context.operation_key,
            "video_capabilities",
            args,
            lambda _: {"mode": "mock", "simulated": True, "mediaAvailable": False, "models": capabilities},
        )

    def generate(args, store, context):
        check_store(store)
        return service.generate(args, context=context)

    def get_job(args, store, context):
        check_store(store)
        return store.operation(
            context.operation_key,
            "job_get",
            args,
            lambda _: job_snapshot(service.get(args["jobId"], project_id=context.project_id)),
        )

    def await_job(args: dict, store: FileStore, context: ToolExecutionContext):
        check_store(store)
        key = context.operation_key
        fingerprint = store.operation_fingerprint("await_job", args)
        # The registry holds the Store lock across this handler. Lookup, final
        # Operation or preparing binding therefore observe one local Job revision.
        for raw in store.snapshot()["waits"].values():
            binding = WaitBinding.model_validate(raw)
            if binding.context.operation_key != key:
                continue
            if (
                binding.context != context
                or binding.operation_fingerprint != fingerprint
                or binding.resource != ExternalResourceRef(kind="job", id=args["jobId"])
            ):
                return failure("OPERATION_CONFLICT", "同一调用 ID 不能用于不同操作。")
            # Never short-circuit an existing interrupt position, even after the
            # Job/Operation completes. B3 will resolve it from its ResumeToken.
            return binding.deferred()
        previous = store.operation_result(key, "await_job", args)
        if previous is not None:
            return previous
        try:
            job = service.get(args["jobId"], project_id=context.project_id)
        except AppError as error:

            def reject(_, error=error):
                raise error

            return store.operation(key, "await_job", args, reject)
        if job.status in {"succeeded", "failed", "unknown"} or job.query_state == "paused":
            return store.operation(key, "await_job", args, lambda _: completed_job_data(job))
        timestamp = service.timestamp()
        binding = WaitBinding(
            id=str(uuid4()),
            context=context,
            resource=ExternalResourceRef(kind="job", id=job.id),
            operation_fingerprint=fingerprint,
            started_at=timestamp,
            deadline_at=(
                datetime.fromisoformat(timestamp) + timedelta(seconds=WAIT_TIMEOUT_SECONDS)
            ).isoformat(),
            timeout_error=ToolResultError(
                code="JOB_WAIT_TIMEOUT", message=f"任务 {job.id} 的本次等待已到期；Job 将继续独立跟踪。"
            ),
        )
        store.transaction(
            lambda draft: draft["waits"].__setitem__(
                binding.id, binding.model_dump(mode="json", by_alias=True)
            )
        )
        return binding.deferred()

    for definition in (
        ToolDefinition(
            "video_capabilities",
            "读取当前模拟视频模型、能力版本和完整合法规格组合；生成前先查询，不自行猜测规格。",
            Arguments,
            "read",
            context_execute=read_capabilities,
            feature="video",
        ),
        ToolDefinition(
            "video_generate",
            "登记一个模拟视频 Job，返回 jobId 与登记状态；不提交上游、不交付真实媒体。"
            "按能力表选择完整规格；来源须属于当前项目并指定 version。每个 Run 最多新建一个 Job。",
            VideoRequest,
            "write",
            context_execute=generate,
            feature="video",
        ),
        ToolDefinition(
            "job_get",
            "读取当前项目内的本地 Job 快照；不访问供应商、不提交或重试。新调用读取最新状态。",
            JobLookup,
            "read",
            context_execute=get_job,
            feature="video",
        ),
        ToolDefinition(
            "await_job",
            "读取当前项目 Job 的终态；未结束时保存持久等待并返回通用延迟标记。"
            "执行器挂起并在结果就绪后续接原调用；不要用它轮询。停止等待不会取消 Job。",
            JobLookup,
            "read",
            context_execute=await_job,
            feature="video",
        ),
    ):
        registry.register(definition)
    return registry
