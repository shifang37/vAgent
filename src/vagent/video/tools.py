"""Scoped video tools. Registration and waiting never call the provider."""

from dataclasses import replace
from datetime import datetime, timedelta
from typing import Annotated
from uuid import uuid4

from pydantic import Field, field_validator, model_validator

from vagent.contracts import Identifier
from vagent.errors import AppError, failure
from vagent.storage import FileStore
from vagent.tools import Arguments, ToolDefinition, ToolFeature, ToolRegistry
from vagent.video.contracts import (
    ArtifactRef,
    Job,
    LiveJob,
    ShotGenerationArgs,
    VideoCapabilities,
    VideoIntentV2,
    VideoIntentV6,
    VideoRequest,
    VideoSpec,
    parse_job,
    wan_capabilities,
)
from vagent.video.jobs import JobService
from vagent.video.storyboards import Framing, ShotMode, read_storyboard, save_storyboard
from vagent.video.views import job_snapshot
from vagent.waiting import (
    ExternalResourceRef,
    ToolExecutionContext,
    ToolResultError,
    WaitBinding,
    WaitBindingV3,
    parse_wait,
)

# Mock moves to the M2 toolset (storyboard tools); live keeps its frozen M1-C
# 3/3 identity until the M2 Wan protocol lands in D2. E1 raised both to 5/5 by
# appending the film tools; E2 raises both to 6/6 for the generic provider request
# and keeps the 5/5 and 4/4 frozen variants so recorded Runs restore.
VIDEO_TOOLS_VERSION = 6
VIDEO_RULES_VERSION = 6
LIVE_VIDEO_TOOLS_VERSION = 6
LIVE_VIDEO_RULES_VERSION = 6
WAIT_TIMEOUT_SECONDS = 600
WAIT_SET_LIMIT = 16
# Every tool this file adds after the frozen pre-M2 versions 1/2/3.
M2_TOOL_NAMES = ("storyboard_read", "storyboard_save", "shot_generate", "asset_read")
M2_STORYBOARD_TOOL_NAMES = ("storyboard_read", "storyboard_save")
# E1 appends the film tools; the frozen 4/4 variants must hide them again.
M3_TOOL_NAMES = ("film_assemble", "film_read")
PRE_M3_TOOL_NAMES = (*M2_TOOL_NAMES, *M3_TOOL_NAMES)

LEGACY_VIDEO_RULES = """当前启用 mock 视频模式，只能登记模拟任务；所有结果均为 simulated=true、mediaAvailable=false。
需要生成时先调用 video_capabilities，根据返回的模型、能力版本和完整规格组合构造 video_generate。
sourceRefs 可省略；引用文本产物时先读取并使用工具确认的 artifactId 和确切 version。
video_generate 只返回本地 jobId 与登记状态；每个 Run 最多新建一个 Job，不能自动降低规格、拆分或重提。
已登记不等于已完成。当前入口不会自动推进 Job，也不能挂起等待未完成任务；先报告 jobId 与真实状态。
job_get 可读取本地状态，await_job 可取得已有终态；不要循环查询。提交不确定或查询暂停时明确报告原因。
成功也只是模拟结果描述，没有可播放或下载的媒体；不能声称已生成真实视频。"""

VIDEO_RULES_V2 = LEGACY_VIDEO_RULES.replace(
    "已登记不等于已完成。当前入口不会自动推进 Job，也不能挂起等待未完成任务；先报告 jobId 与真实状态。\n"
    "job_get 可读取本地状态，await_job 可取得已有终态；不要循环查询。提交不确定或查询暂停时明确报告原因。",
    "已登记不等于已完成。可以先报告 jobId 与真实状态；需要等待结果时调用 await_job，Run 会持久挂起。\n"
    "Job 由独立 Worker 推进；等待不会请求模型或占用活动时间，结果就绪后续接原调用。\n"
    "job_get 读取本地状态；不要循环查询。等待到期、提交不确定或查询暂停时明确报告原因。",
)

VIDEO_RULES_V4 = """当前启用 mock 视频模式，只能登记模拟任务；所有结果均为 simulated=true、mediaAvailable=false。
多镜头需求先用 storyboard_save 保存结构化分镜：按顺序给出每个镜头的 description、framing、durationSeconds 与 mode；
镜头 id 由服务端生成并跨版本稳定，修改时带 storyboardId 与 expectedVersion，产生新版本并保留旧版本。
storyboard_read 读取当前分镜与各镜头版本摘要。保存分镜不创建视频任务。
asset_read 列出素材库中已导入的参考图（assetId、格式、尺寸、来源项目）；引用素材只用 assetId，不展示或索取本地路径。
shot_generate 为选定镜头批量登记模拟任务，必须显式确认并回显逐镜头估算；同一分镜版本同一镜头的相同请求复用原任务。
重做只由用户显式发起且只影响目标镜头；其他镜头的版本、任务与媒体必须保持不动。
需要生成时先调用 video_capabilities，根据返回的模型、能力版本和完整规格组合构造 video_generate。
sourceRefs 可省略；引用文本产物时先读取并使用工具确认的 artifactId 和确切 version。
video_generate 只返回本地 jobId 与登记状态；每个 Run 最多新建一个 Job，不能自动降低规格、拆分或重提。
已登记不等于已完成。可以先报告 jobId 与真实状态；需要等待结果时调用 await_job，Run 会持久挂起。
Job 由独立 Worker 推进；等待不会请求模型或占用活动时间，结果就绪后续接原调用。
job_get 读取本地状态；不要循环查询。等待到期、提交不确定或查询暂停时明确报告原因。
成功也只是模拟结果描述，没有可播放或下载的媒体；不能声称已生成真实视频。"""


def legacy_mock_capabilities() -> list[dict]:
    """Exact pre-M2 mock capability JSON, pinned so restores keep frozen toolFeatures."""
    return [
        VideoCapabilities(
            provider="mock",
            model="mock-t2v",
            capabilities_version="v1",
            specs=(
                VideoSpec(duration_seconds=5, resolution="720p", aspect_ratio="16:9"),
                VideoSpec(duration_seconds=10, resolution="1080p", aspect_ratio="9:16"),
            ),
        ).model_dump(mode="json", by_alias=True)
    ]


LIVE_VIDEO_RULES_V3 = """当前启用 live 视频模式，可登记真实万相单镜头文生视频任务。
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

AWAIT_DESCRIPTION_M1C = (
    "读取当前项目 Job 的终态；未结束时保存持久等待并返回通用延迟标记。"
    "执行器挂起并在结果就绪后续接原调用；不要用它轮询。停止等待不会取消 Job。"
)

AWAIT_DESCRIPTION_M2 = (
    "读取一组当前项目视频任务的终态：全部到达可交付终态后返回逐镜头结果与失败摘要；"
    "未结束时保存一次持久等待，执行器挂起并在集合结果就绪后续接原调用。"
    "等待到期返回已完成部分，可显式重新等待；不要用它轮询。停止等待不会取消 Job。"
)

LIVE_VIDEO_RULES_V4 = """当前启用 live 视频模式，可为分镜的每个镜头登记真实万相视频任务。
多镜头需求先用 storyboard_save 保存结构化分镜：按顺序给出每个镜头的 description、framing、durationSeconds 与 mode；
镜头 id 由服务端生成并跨版本稳定，修改时带 storyboardId 与 expectedVersion，产生新版本并保留旧版本。storyboard_read 读取当前分镜与各镜头版本摘要。
asset_read 列出素材库中已导入的参考图（assetId、格式、尺寸、来源项目）；i2v/首尾帧的 referenceAssetIds 只能引用其返回的 assetId，不展示或索取本地路径。
生成前调用 video_capabilities，按返回的每个模式条目选择精确模型、能力版本和完整规格组合；图生视频/首尾帧只引用素材库中已导入的参考图，不猜测或替换模型、时长与画幅。
shot_generate 必须显式确认，一次登记全部选中镜头；结果固定回显逐镜头与合计估算、已用与剩余额度，服务端强制每 Job 金额、每 Run 数量与累计金额三重上限，超出即整批拒绝且不部分执行。
一次调用只登记用户确认的镜头；不得自动降规格、拆分多次调用绕过上限或重复提交同一镜头；同一分镜版本同一镜头的相同请求复用原任务。
重做只由用户显式发起且只影响目标镜头：其他镜头的版本、任务与媒体必须保持不动。
需要等待时调用 await_job 并传入全部 jobIds，Run 持久挂起并在集合结果就绪后续接原调用；等待不请求模型，不占活动时间。job_get 只读本地事实，不循环查询。
云端成功进入 downloading，只表示已有上游结果；完整本地媒体交付后才是 succeeded。只有 mediaAvailable=true 且有 mediaRefs 时才能报告媒体可用，不能编造下载地址。
费用 estimate 是后端报价，actual 未取得账单时始终未知；不能把估算或供应商 usage 当作实际扣费。
缺配置、提交不确定、查询暂停、下载失败或等待超时，应报告原 jobId 和具体原因；修复沿用原任务，不能再次生成。停止 Agent 不代表云端取消。
不要索取、读取或展示视频 Key、签名 URL 或素材私有路径。"""

FILM_RULES = """所有镜头都交付本地 MP4 后，可用 film_assemble 生成本地拼接成片：它按分镜顺序做流复制，不调用供应商、不登记生成任务、不产生费用。
拼接需要分镜的 expectedRevision（取自 storyboard_read 的 revision）与 storyboardId；接口会核对每个镜头最新版本是否已交付、编码配置是否可拼接，任一不满足则整体拒绝并列出镜头。
同一组输入重复拼接返回原版本，不新增版本或文件；镜头重做后不会自动重拼，只有用户显式要求时才再次拼接，旧版本与文件保留。
film_read 只读返回当前成片、历史版本与受控媒体 URL；状态以工具返回为准，不要声称未交付的成片可播放或可下载。"""

VIDEO_RULES_V5 = VIDEO_RULES_V4 + "\n" + FILM_RULES
LIVE_VIDEO_RULES_V5 = LIVE_VIDEO_RULES_V4 + "\n" + FILM_RULES

MULTI_PROVIDER_RULES = """真实视频供应商由能力表逐条列出：wan（万相）、kling（Kling 新加坡直连）、fal（fal 队列中转）。
每个供应商只使用自己的 Key；未配置 Key 的供应商不可用，但不阻塞其他供应商、模拟任务与文本创作。
kling 与 fal 本期只开放文生视频：5 秒、720p、16:9；kling 含原生音频，fal standard 档显式静音。不要请求图生、首尾帧、时长或画幅变体。
video_generate 只登记本地 Job；每个 Run 最多新建 4 个真实任务，全 Run 共享这一上限。各供应商按原币种与资源单位分桶限额，不换汇、不把某供应商的余额挪给另一个。
estimate 与资源单位来自冻结能力表；actual 在取得账单前始终未知。不同供应商的镜头能否拼接要按实际编码检查，不能按模型名或 720p 标签保证。"""

VIDEO_RULES_V6 = VIDEO_RULES_V5
LIVE_VIDEO_RULES_V6 = LIVE_VIDEO_RULES_V5 + "\n" + MULTI_PROVIDER_RULES

OLD_LIVE_GENERATE_DESCRIPTION = (
    "登记一个真实视频 Job，返回 jobId 与登记状态；后台仅提交一次，云端成功后还需本地媒体交付。"
    "按能力表选择完整规格；来源须属于当前项目并指定 version。每个 Run 最多新建一个 Job。"
)


def legacy_live_capabilities() -> list[dict]:
    """Exact M1-C live capability JSON, pinned so restored 3/3 Runs keep their signature."""
    return [wan_capabilities().model_dump(mode="json", by_alias=True)]


class JobLookup(Arguments):
    job_id: Identifier


class JobSetLookup(Arguments):
    job_ids: list[Identifier] = Field(min_length=1, max_length=WAIT_SET_LIMIT)


CanonicalId = Annotated[
    str, Field(pattern=r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
]


class StoryboardShotSpec(Arguments):
    id: CanonicalId | None = None
    description: str = Field(min_length=1, max_length=2000)
    framing: Framing
    camera_movement: str | None = Field(default=None, max_length=200)
    duration_seconds: int = Field(default=5, ge=2, le=15)
    mode: ShotMode = "t2v"
    reference_asset_ids: list[CanonicalId] = Field(default_factory=list, max_length=2)

    @field_validator("camera_movement")
    @classmethod
    def blank_movement(cls, value):
        if value is None:
            return None
        return value.strip() or None

    @model_validator(mode="after")
    def references_match_mode(self):
        if len(self.reference_asset_ids) != {"t2v": 0, "i2v": 1, "flf2v": 2}[self.mode]:
            raise ValueError("镜头模式要求的参考图数量不匹配")
        if len(set(self.reference_asset_ids)) != len(self.reference_asset_ids):
            raise ValueError("同一镜头的参考图不能重复")
        return self


class StoryboardSave(Arguments):
    storyboard_id: CanonicalId | None = None
    expected_version: int | None = Field(default=None, ge=1)
    title: str = Field(min_length=1, max_length=120)
    shots: list[StoryboardShotSpec] = Field(min_length=1, max_length=64)
    source_refs: list[ArtifactRef] | None = Field(default=None, max_length=16)

    @model_validator(mode="after")
    def require_version(self):
        if bool(self.storyboard_id) != bool(self.expected_version):
            raise ValueError("修改时必须同时提供 storyboardId 和 expectedVersion")
        return self


class StoryboardRead(Arguments):
    storyboard_id: CanonicalId | None = None


class FilmAssemble(Arguments):
    storyboard_id: CanonicalId
    expected_revision: int = Field(ge=0)


class FilmRead(Arguments):
    storyboard_id: CanonicalId | None = None


class AssetLookup(Arguments):
    """Read-only asset discovery: one asset by ID, or the bounded newest listing."""

    asset_id: CanonicalId | None = None
    project_id: Identifier | None = None


def _asset_listing(args: dict, state: dict, project_id: str) -> dict:
    """Local metadata only: no filesystem path, private URL or content bytes."""
    from vagent.video.assets import ASSET_LIST_LIMIT, asset_public_metadata
    from vagent.video.storyboards import Asset

    assets = [Asset.model_validate(raw) for raw in state["assets"].values()]
    if args.get("assetId"):
        asset = next((item for item in assets if item.id == args["assetId"]), None)
        if asset is None:
            raise AppError("ASSET_NOT_FOUND", "素材库中没有这个素材。")
        return {"asset": asset_public_metadata(asset), "callerProject": project_id}
    scope = args.get("projectId")
    selected = [item for item in assets if scope is None or item.project_id == scope]
    selected.sort(key=lambda item: (item.created_at, item.id), reverse=True)
    return {
        "assets": [asset_public_metadata(item) for item in selected[:ASSET_LIST_LIMIT]],
        "total": len(selected),
        "truncated": len(selected) > ASSET_LIST_LIMIT,
        "callerProject": project_id,
    }


def completed_job_data(job: Job) -> dict:
    """Raw success data or a bounded error, for immediate waits and the B3 resolver."""
    if job.status == "succeeded":
        if isinstance(job, LiveJob) and job.media_availability.status != "available":
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
    if isinstance(job, LiveJob) and job.runtime_block is not None:
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
        if isinstance(job, LiveJob) and job.status == "succeeded":
            job = (service or JobService(store, [])).media.refresh(job)
        return {"ok": True, "data": completed_job_data(job)}
    except AppError as error:
        if error.code == "JOB_NOT_READY":
            return None
        return failure(error.code, str(error))


def job_settled(job: Job) -> bool:
    """A Job that will not change without a new user action is deliverable evidence."""
    if (
        isinstance(job, LiveJob)
        and job.status == "succeeded"
        and job.media_availability.status != "available"
        and job.download.repair
        and job.download.phase in {"pending", "writing", "prepared"}
    ):
        return False
    return (
        job.status in {"succeeded", "failed", "unknown", "download_failed"}
        or job.query_state == "paused"
        or (isinstance(job, LiveJob) and job.runtime_block is not None)
    )


def jobset_entries(items) -> list[dict]:
    """Per-shot outcomes for one set wait; failures never hide other deliveries."""
    entries = []
    for job_id, job in items:
        if job is None:
            entries.append(
                {
                    "jobId": job_id,
                    "ok": False,
                    "status": None,
                    "mediaAvailable": False,
                    "error": {"code": "JOB_NOT_FOUND", "message": "没有这个视频任务。"},
                }
            )
            continue
        try:
            data = completed_job_data(job)
        except AppError as error:
            entries.append(
                {
                    "jobId": job_id,
                    "ok": False,
                    "status": job.status,
                    "mediaAvailable": False,
                    "error": {"code": error.code, "message": str(error)},
                }
            )
            continue
        entries.append(
            {
                "jobId": job_id,
                "ok": True,
                "status": job.status,
                "mediaAvailable": bool(data.get("mediaAvailable")),
                "result": data,
            }
        )
    return entries


def jobset_summary(entries, *, expired: bool = False) -> dict:
    done = [item for item in entries if item["ok"]]
    failed = [item for item in entries if not item["ok"]]
    summary = f"共 {len(entries)} 个镜头任务：{len(done)} 个可交付，{len(failed)} 个未完成或失败。"
    if expired:
        summary += "本次等待已到期；任务继续独立跟踪，可显式重新等待。"
    return {
        "jobIds": [item["jobId"] for item in entries],
        "jobs": entries,
        "succeeded": len(done),
        "failed": len(failed),
        "summary": summary,
    }


def resolve_jobset_wait(store: FileStore, binding, *, service=None) -> dict | None:
    """Resolve a set wait once every member is settled, or with the partial result at its deadline."""
    service = service or JobService(store, [])
    state = store.snapshot()
    items = []
    for job_id in binding.job_ids:
        raw = state["jobs"].get(job_id)
        if raw is None or raw["context"]["projectId"] != binding.context.project_id:
            items.append((job_id, None))
            continue
        job = parse_job(raw)
        if isinstance(job, LiveJob) and job.status == "succeeded":
            job = service.media.refresh(job)
        items.append((job_id, job))
    settled = all(job is not None and job_settled(job) for _, job in items)
    expired = service.timestamp() >= binding.deadline_at
    if not settled and not expired:
        return None
    return {"ok": True, "data": jobset_summary(jobset_entries(items), expired=expired)}


def register_video_tools(registry: ToolRegistry, service: JobService, *, mode="mock") -> ToolRegistry:
    capabilities = [item.model_dump(mode="json", by_alias=True) for item in service.capabilities(mode)]
    if mode not in {"mock", "live"} or not capabilities:
        raise AppError("VIDEO_PROVIDER_UNAVAILABLE", "缺少原模式的视频适配器和能力表。")
    live = mode == "live"
    # The frozen 4/4 and 5/5 live signatures saw only the Wan entries; the E2
    # multi-provider table is appended in 6/6 and must not leak into an old one.
    historical = [item for item in capabilities if item["provider"] == "wan"] if live else capabilities
    registry.register_feature(
        ToolFeature(
            name="video",
            configuration={
                "mode": mode,
                "toolsVersion": LIVE_VIDEO_TOOLS_VERSION if live else VIDEO_TOOLS_VERSION,
                "rulesVersion": LIVE_VIDEO_RULES_VERSION if live else VIDEO_RULES_VERSION,
                "capabilities": capabilities,
            },
            instructions=LIVE_VIDEO_RULES_V6 if live else VIDEO_RULES_V6,
            bypass_answer_cache=True,
        )
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
                **(
                    {
                        "configuration": service.configuration_status(),
                        "providers": service.provider_statuses(),
                    }
                    if live
                    else {}
                ),
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

    films = service.films

    def read_film(args, store, context):
        check_store(store)
        state = store.snapshot()
        storyboard_id = args.get("storyboardId")
        if storyboard_id is None:
            candidates = [
                item for item in state["storyboards"].values() if item["projectId"] == context.project_id
            ]
            if not candidates:
                return {"storyboardId": None, "film": None}
            storyboard_id = max(candidates, key=lambda item: (item["createdAt"], item["id"]))["id"]
        return films.view(context.project_id, storyboard_id)

    async def assemble_film(args, store, context):
        check_store(store)
        return await films.assemble(
            project_id=context.project_id,
            storyboard_id=args["storyboardId"],
            expected_revision=args["expectedRevision"],
            operation_key=context.operation_key,
            args=args,
        )

    def await_single(job_id: str, args: dict, store: FileStore, context: ToolExecutionContext):
        """The frozen M1-C single-Job wait; one element of the set shape is this path."""
        key = context.operation_key
        fingerprint = store.operation_fingerprint("await_job", args)
        # The registry holds the Store lock across this handler. Lookup, final
        # Operation or preparing binding therefore observe one local Job revision.
        for raw in store.snapshot()["waits"].values():
            binding = parse_wait(raw)
            if binding.context.operation_key != key:
                continue
            if (
                binding.context != context
                or binding.operation_fingerprint != fingerprint
                or binding.resource != ExternalResourceRef(kind="job", id=job_id)
            ):
                return failure("OPERATION_CONFLICT", "同一调用 ID 不能用于不同操作。")
            # Never short-circuit an existing interrupt position, even after the
            # Job/Operation completes. B3 will resolve it from its ResumeToken.
            return binding.deferred()
        previous = store.operation_result(key, "await_job", args)
        if previous is not None:
            return previous
        try:
            job = service.media.refresh(service.get(job_id, project_id=context.project_id))
        except AppError as error:

            def reject(_, error=error):
                raise error

            return store.operation(key, "await_job", args, reject)
        repairing = (
            isinstance(job, LiveJob)
            and job.status == "succeeded"
            and job.media_availability.status != "available"
            and job.download.repair
            and job.download.phase in {"pending", "writing", "prepared"}
        )
        if not repairing and (
            job.status in {"succeeded", "failed", "unknown", "download_failed"}
            or job.query_state == "paused"
            or (isinstance(job, LiveJob) and job.runtime_block is not None)
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

    def await_set(job_ids, args: dict, store: FileStore, context: ToolExecutionContext):
        """Register one persistent wait over several Jobs of this project."""
        key = context.operation_key
        fingerprint = store.operation_fingerprint("await_job", args)
        for raw in store.snapshot()["waits"].values():
            binding = parse_wait(raw)
            if binding.context.operation_key != key:
                continue
            if (
                binding.context != context
                or binding.operation_fingerprint != fingerprint
                or binding.resource.kind != "jobset"
                or list(getattr(binding, "job_ids", ())) != job_ids
            ):
                return failure("OPERATION_CONFLICT", "同一调用 ID 不能用于不同操作。")
            return binding.deferred()
        previous = store.operation_result(key, "await_job", args)
        if previous is not None:
            return previous
        try:
            jobs = [
                service.media.refresh(service.get(job_id, project_id=context.project_id))
                for job_id in job_ids
            ]
        except AppError as error:

            def reject(_, error=error):
                raise error

            return store.operation(key, "await_job", args, reject)
        if all(job_settled(job) for job in jobs):
            return store.operation(
                key, "await_job", args, lambda _: jobset_summary(jobset_entries(zip(job_ids, jobs)))
            )
        timestamp = service.timestamp()
        binding = WaitBindingV3(
            id=str(uuid4()),
            context=context,
            resource=ExternalResourceRef(kind="jobset", id=str(uuid4())),
            job_ids=tuple(job_ids),
            operation_fingerprint=fingerprint,
            started_at=timestamp,
            deadline_at=(
                datetime.fromisoformat(timestamp) + timedelta(seconds=WAIT_TIMEOUT_SECONDS)
            ).isoformat(),
            timeout_error=ToolResultError(
                code="JOB_WAIT_TIMEOUT",
                message=f"这 {len(job_ids)} 个镜头任务的本次等待已到期；Job 将继续独立跟踪。",
            ),
        )
        store.transaction(
            lambda draft: draft["waits"].__setitem__(
                binding.id, binding.model_dump(mode="json", by_alias=True)
            )
        )
        return binding.deferred()

    def await_job(args: dict, store: FileStore, context: ToolExecutionContext):
        check_store(store)
        job_ids = args.get("jobIds")
        if job_ids is None and "jobId" in args:
            # The pre-M2 call shape, still accepted for Runs recorded before D2.
            job_ids = [args["jobId"]]
        if not isinstance(job_ids, list) or not job_ids:
            return failure("INVALID_ARGUMENTS", "需要至少一个 jobId。")
        if len(job_ids) == 1:
            return await_single(job_ids[0], args, store, context)
        return await_set(sorted(set(job_ids)), args, store, context)

    def shot_generate(args: dict, store: FileStore, context: ToolExecutionContext):
        check_store(store)
        return service.shot_generate(args, context=context)

    definitions = [
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
                "按能力表选择供应商与完整规格；kling/fal 本期只开放文生视频；来源须属于当前项目并指定 version。"
                "每个 Run 最多新建 4 个真实任务，各供应商额度单独计。"
            )
            if live
            else (
                "登记一个模拟视频 Job，返回 jobId 与登记状态；不提交上游、不交付真实媒体。"
                "按能力表选择完整规格；来源须属于当前项目并指定 version。每个 Run 最多新建一个 Job。"
            ),
            VideoIntentV6 if live else VideoRequest,
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
            AWAIT_DESCRIPTION_M2,
            JobSetLookup,
            "read",
            context_execute=await_job,
            feature="video",
        ),
        ToolDefinition(
            "shot_generate",
            "为已保存分镜的指定镜头批量登记视频任务：必须显式确认，一次登记全部选中镜头，"
            "返回逐镜头与合计估算、已用与剩余额度；任一镜头校验失败则整批不登记，"
            "同一分镜版本同一镜头的相同请求复用原任务。只登记，不提交、不等待。",
            ShotGenerationArgs,
            "write",
            context_execute=shot_generate,
            feature="video",
        ),
        ToolDefinition(
            "storyboard_read",
            "读取当前项目的结构化分镜：当前版本的有序镜头（稳定 shotId、景别、时长、模式、参考图）与各镜头版本摘要；"
            "省略 storyboardId 时读取最新分镜。只读，不创建视频任务。",
            StoryboardRead,
            "read",
            read_storyboard,
            feature="video",
        ),
        ToolDefinition(
            "asset_read",
            "读取素材库中已导入参考图的元数据（assetId、格式、尺寸、大小、来源项目）；"
            "省略 assetId 时按导入时间倒序列出最近素材，可用 projectId 过滤。"
            "只读，不返回本地路径或私有 URL，也不导入、删除或改写素材。",
            AssetLookup,
            "read",
            _asset_listing,
            feature="video",
        ),
        ToolDefinition(
            "storyboard_save",
            "新建或修改结构化分镜：shots 按给定顺序编号，镜头 id 由服务端生成并跨版本稳定；"
            "修改时同时提供 storyboardId 与 expectedVersion，产生新版本并保留旧版本；显式传入既有镜头 id 保持其身份。"
            "i2v 镜头需 1 张、flf2v 需 2 张素材库参考图。只保存分镜结构，不创建视频任务。",
            StoryboardSave,
            "write",
            save_storyboard,
            feature="video",
        ),
        ToolDefinition(
            "film_read",
            "读取当前分镜的本地拼接成片：返回 filmId、当前版本、历史版本摘要、逐输入镜头与时间线偏移，"
            "以及受控媒体 URL。省略 storyboardId 时读取本项目最新分镜。只读，不拼接、不创建任务。",
            FilmRead,
            "read",
            context_execute=read_film,
            feature="video",
        ),
        ToolDefinition(
            "film_assemble",
            "把当前分镜版本里每个镜头的最新已交付 MP4 按顺序本地拼接成单一成片：使用包内 FFmpeg 流复制，"
            "不调用供应商、不登记生成任务、不产生费用。必须提供分镜 revision 作为 expectedRevision；"
            "任一镜头最新版本未交付或编码不可拼接则整体拒绝。同一组输入重复拼接返回原版本；"
            "镜头重做后不会自动重拼。拼接在 Store 锁外执行，可被停止。",
            FilmAssemble,
            "write",
            None,
            async_write_execute=assemble_film,
            feature="video",
        ),
    ]
    await_definition = next(item for item in definitions if item.name == "await_job")
    generate_definition = next(item for item in definitions if item.name == "video_generate")
    for definition in definitions:
        registry.register(definition)

    # The six public live arguments arrived with 6/6; older signatures keep the
    # frozen intent schema and description they were recorded with.
    frozen_generate = replace(
        generate_definition, schema=VideoIntentV2, description=OLD_LIVE_GENERATE_DESCRIPTION
    )

    def frozen_await(description: str) -> ToolDefinition:
        return replace(await_definition, schema=JobLookup, description=description)

    if live:
        registry.register_execution_variant(
            2,
            features=[
                ToolFeature(
                    name="video",
                    configuration={
                        "mode": "live",
                        "toolsVersion": 3,
                        "rulesVersion": 3,
                        "capabilities": legacy_live_capabilities(),
                    },
                    instructions=LIVE_VIDEO_RULES_V3,
                    bypass_answer_cache=True,
                )
            ],
            descriptions={},
            exclude=PRE_M3_TOOL_NAMES,
            definitions={
                "await_job": frozen_await(AWAIT_DESCRIPTION_M1C),
                "video_generate": frozen_generate,
            },
        )
        registry.register_tools_variant(
            4,
            features=[
                ToolFeature(
                    name="video",
                    configuration={
                        "mode": "live",
                        "toolsVersion": 4,
                        "rulesVersion": 4,
                        "capabilities": historical,
                    },
                    instructions=LIVE_VIDEO_RULES_V4,
                    bypass_answer_cache=True,
                )
            ],
            exclude=M3_TOOL_NAMES,
            definitions={"video_generate": frozen_generate},
        )
        registry.register_tools_variant(
            5,
            features=[
                ToolFeature(
                    name="video",
                    configuration={
                        "mode": "live",
                        "toolsVersion": 5,
                        "rulesVersion": 5,
                        "capabilities": historical,
                    },
                    instructions=LIVE_VIDEO_RULES_V5,
                    bypass_answer_cache=True,
                )
            ],
            definitions={"video_generate": frozen_generate},
        )
    else:
        legacy_capabilities = legacy_mock_capabilities()
        registry.register_execution_variant(
            1,
            features=[
                ToolFeature(
                    name="video",
                    configuration={
                        "mode": "mock",
                        "toolsVersion": 1,
                        "rulesVersion": 1,
                        "capabilities": legacy_capabilities,
                    },
                    instructions=LEGACY_VIDEO_RULES,
                    bypass_answer_cache=True,
                )
            ],
            descriptions={},
            exclude=PRE_M3_TOOL_NAMES,
            definitions={"await_job": frozen_await(LEGACY_AWAIT_DESCRIPTION)},
        )
        registry.register_execution_variant(
            2,
            features=[
                ToolFeature(
                    name="video",
                    configuration={
                        "mode": "mock",
                        "toolsVersion": 2,
                        "rulesVersion": 2,
                        "capabilities": legacy_capabilities,
                    },
                    instructions=VIDEO_RULES_V2,
                    bypass_answer_cache=True,
                )
            ],
            descriptions={},
            exclude=PRE_M3_TOOL_NAMES,
            definitions={"await_job": frozen_await(AWAIT_DESCRIPTION_M1C)},
        )
        registry.register_tools_variant(
            4,
            features=[
                ToolFeature(
                    name="video",
                    configuration={
                        "mode": "mock",
                        "toolsVersion": 4,
                        "rulesVersion": 4,
                        "capabilities": historical,
                    },
                    instructions=VIDEO_RULES_V4,
                    bypass_answer_cache=True,
                )
            ],
            exclude=M3_TOOL_NAMES,
        )
        registry.register_tools_variant(
            5,
            features=[
                ToolFeature(
                    name="video",
                    configuration={
                        "mode": "mock",
                        "toolsVersion": 5,
                        "rulesVersion": 5,
                        "capabilities": historical,
                    },
                    instructions=VIDEO_RULES_V5,
                    bypass_answer_cache=True,
                )
            ],
        )
    registry.register_wait_resolver(
        "job", lambda binding: resolve_job_wait(service.store, binding, service=service), feature="video"
    )
    registry.register_wait_resolver(
        "jobset",
        lambda binding: resolve_jobset_wait(service.store, binding, service=service),
        feature="video",
    )
    return registry
