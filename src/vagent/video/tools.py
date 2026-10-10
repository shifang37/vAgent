"""Scoped video tools. Registration and waiting never call the provider."""

from datetime import datetime, timedelta
from uuid import uuid4

from vagent.contracts import Identifier
from vagent.errors import AppError, failure
from vagent.storage import FileStore
from vagent.tools import Arguments, ToolDefinition, ToolFeature, ToolRegistry
from vagent.video.contracts import Job, JobV2, VideoIntentV2, VideoRequest, parse_job
from vagent.video.jobs import JobService
from vagent.video.views import job_snapshot
from vagent.waiting import ExternalResourceRef, ToolExecutionContext, ToolResultError, WaitBinding

VIDEO_TOOLS_VERSION = 2
VIDEO_RULES_VERSION = 2
LIVE_VIDEO_TOOLS_VERSION = 3
LIVE_VIDEO_RULES_VERSION = 3
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

LIVE_VIDEO_RULES = """当前启用 live 视频模式，可登记真实万相单镜头文生视频任务。
生成前调用 video_capabilities，按返回的精确模型、能力版本和完整规格组合构造 video_generate；不猜测或替换模型、地域、时长及画幅。
sourceRefs 可省略；引用文本产物须使用工具确认的 artifactId 和确切 version，保持原来源版本。
video_generate 只登记本地 Job，后台最多提交一次；每个 Run 最多新建一个 Job，不拆分、降低规格或新建替代任务。
需要等待时调用 await_job，Run 持久挂起并在结果就绪后续接原调用；等待不请求模型，不占活动时间。job_get 只读本地事实，不循环查询。
云端成功进入 downloading，只表示已有上游结果；完整本地媒体交付后才是 succeeded。只有 mediaAvailable=true 且有 mediaRefs 时才能报告媒体可用，不能编造下载地址。
费用 estimate 是后端报价，actual 未取得账单时始终未知；不能把估算或供应商 usage 当作实际扣费。
缺配置、提交不确定、查询暂停、下载失败或等待超时，应报告原 jobId 和具体原因；修复沿用原任务，不能再次生成。停止 Agent 不代表云端取消。
不要索取、读取或展示视频 Key、签名 URL 或供应商私有配置。"""

LEGACY_AWAIT_DESCRIPTION = (
    "读取当前项目 Job 的终态；未结束时保存持久等待并返回通用延迟标记。"
    "当前执行器尚不支持挂起，遇到未完成任务会结束本次 Run；不要用它轮询。"
)


class JobLookup(Arguments):
    job_id: Identifier


def completed_job_data(job: Job) -> dict:
    """Raw success data or a bounded error, for immediate waits and the B3 resolver."""
    if job.status == "succeeded":
        if isinstance(job, JobV2) and job.media_availability.status != "available":
            if job.download.repair and job.download.phase in {"pending", "writing", "prepared"}:
                raise AppError("JOB_NOT_READY", "原媒体正在修复。")
            raise AppError("JOB_MEDIA_UNAVAILABLE", f"任务 {job.id} 的本地媒体当前不可用。")
        return job_snapshot(job)
    if job.status == "download_failed":
        raise AppError(
            "JOB_DOWNLOAD_FAILED", f"任务 {job.id} 已生成，但本地下载失败（{job.error.code}）；保留原任务。"
        )
    if job.status == "failed":
        raise AppError("JOB_FAILED", f"任务 {job.id} 已失败（{job.error.stage}/{job.error.code}）。")
    if job.status == "unknown":
        raise AppError("JOB_SUBMISSION_UNKNOWN", f"任务 {job.id} 的提交结果不确定，不会自动重新提交。")
    if job.query_state == "paused":
        raise AppError("JOB_QUERY_PAUSED", f"任务 {job.id} 的查询已暂停；生成状态未变，需要恢复原任务查询。")
    if isinstance(job, JobV2) and job.runtime_block is not None:
        raise AppError(
            "VIDEO_PROVIDER_UNAVAILABLE",
            f"任务 {job.id} 的执行条件未满足（{job.runtime_block.code}），请补齐原任务配置。",
        )
    raise AppError("JOB_NOT_READY", "视频任务尚未结束。")


def resolve_job_wait(store: FileStore, binding: WaitBinding, *, service=None) -> dict | None:
    """Resolve local facts even when the original adapter/model is unavailable."""
    raw = store.snapshot()["jobs"].get(binding.resource.id)
    if raw is None or raw["context"]["projectId"] != binding.context.project_id:
        return failure("JOB_NOT_FOUND", "没有这个视频任务。")
    try:
        job = parse_job(raw)
        if isinstance(job, JobV2) and job.status == "succeeded":
            job = (service or JobService(store, [])).media.refresh(job)
        return {"ok": True, "data": completed_job_data(job)}
    except AppError as error:
        if error.code == "JOB_NOT_READY":
            return None
        return failure(error.code, str(error))


def register_video_tools(registry: ToolRegistry, service: JobService, *, mode="mock") -> ToolRegistry:
    capabilities = [item.model_dump(mode="json", by_alias=True) for item in service.capabilities(mode)]
    if mode not in {"mock", "live"} or not capabilities:
        raise AppError("VIDEO_PROVIDER_UNAVAILABLE", "缺少原模式的视频适配器和能力表。")
    live = mode == "live"
    registry.register_feature(
        ToolFeature(
            name="video",
            configuration={
                "mode": mode,
                "toolsVersion": LIVE_VIDEO_TOOLS_VERSION if live else VIDEO_TOOLS_VERSION,
                "rulesVersion": LIVE_VIDEO_RULES_VERSION if live else VIDEO_RULES_VERSION,
                "capabilities": capabilities,
            },
            instructions=LIVE_VIDEO_RULES if live else VIDEO_RULES,
            bypass_answer_cache=True,
        )
    )
    if not live:
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
        "job", lambda binding: resolve_job_wait(service.store, binding, service=service), feature="video"
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
            lambda _: {
                "mode": mode,
                "simulated": not live,
                "mediaAvailable": False,
                "models": capabilities,
                **({"configuration": service.configuration_status()} if live else {}),
            },
        )

    def generate(args, store, context):
        check_store(store)
        return service.generate(args, context=context)

    def get_job(args, store, context):
        check_store(store)
        previous = store.operation_result(context.operation_key, "job_get", args)
        if previous is not None:
            return previous
        # File verification can update availability, so it precedes the Operation
        # transaction. A nested Store transaction would overwrite the new revision.
        try:
            job = service.media.refresh(service.get(args["jobId"], project_id=context.project_id))
        except AppError as error:

            def reject(_, error=error):
                raise error

            return store.operation(context.operation_key, "job_get", args, reject)
        return store.operation(
            context.operation_key,
            "job_get",
            args,
            lambda _: job_snapshot(job),
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
            job = service.media.refresh(service.get(args["jobId"], project_id=context.project_id))
        except AppError as error:

            def reject(_, error=error):
                raise error

            return store.operation(key, "await_job", args, reject)
        repairing = (
            isinstance(job, JobV2)
            and job.status == "succeeded"
            and job.media_availability.status != "available"
            and job.download.repair
            and job.download.phase in {"pending", "writing", "prepared"}
        )
        if not repairing and (
            job.status in {"succeeded", "failed", "unknown", "download_failed"}
            or job.query_state == "paused"
            or (isinstance(job, JobV2) and job.runtime_block is not None)
        ):
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
            "读取当前真实视频模型、完整规格、固定参数、估价及配置状态；生成前先查询，不自行猜测。"
            if live
            else "读取当前模拟视频模型、能力版本和完整合法规格组合；生成前先查询，不自行猜测规格。",
            Arguments,
            "read",
            context_execute=read_capabilities,
            feature="video",
        ),
        ToolDefinition(
            "video_generate",
            (
                "登记一个真实视频 Job，返回 jobId 与登记状态；后台仅提交一次，云端成功后还需本地媒体交付。"
                "按能力表选择完整规格；来源须属于当前项目并指定 version。每个 Run 最多新建一个 Job。"
            )
            if live
            else (
                "登记一个模拟视频 Job，返回 jobId 与登记状态；不提交上游、不交付真实媒体。"
                "按能力表选择完整规格；来源须属于当前项目并指定 version。每个 Run 最多新建一个 Job。"
            ),
            VideoIntentV2 if live else VideoRequest,
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
