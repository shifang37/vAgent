"""Scoped video tools. Registration and waiting never call the provider."""

from dataclasses import replace
from datetime import datetime, timedelta
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import Field, field_validator, model_validator

from vagent.contracts import Identifier
from vagent.errors import AppError, failure
from vagent.storage import FileStore
from vagent.tools import Arguments, ToolDefinition, ToolFeature, ToolRegistry
from vagent.video.adaptation import plan_episode
from vagent.video.audio import AudioGenerateArgs
from vagent.video.audio_service import episode_audio_view
from vagent.video.contracts import (
    ArtifactRef,
    EpisodeGenerationArgs,
    Job,
    LiveJob,
    ShotGenerationArgs,
    ShotRedoToolArgs,
    VideoCapabilities,
    VideoIntentV2,
    VideoIntentV6,
    VideoRequest,
    VideoSpec,
    parse_job,
    wan_capabilities,
)
from vagent.video.episodes import (
    SCENE_MAX_SHOTS,
    TimeOfDay,
    read_bible,
    read_episode,
    record_consistency_review,
    save_bible,
    save_episode,
)
from vagent.video.jobs import JobService
from vagent.video.renders import FilmRenderArgs, RenderReadArgs
from vagent.video.seasons import read_season, save_season
from vagent.video.storyboards import Framing, ShotMode, read_storyboard, save_storyboard
from vagent.video.subtitles import save_subtitles, subtitle_for
from vagent.video.tts import tts_capabilities
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
# and keeps the 5/5 and 4/4 frozen variants so recorded Runs restore. M5 E1 appends
# the five adaptation tools and raises both to 7/7, freezing 6/6 as history. M5 E2
# appends the consistency review tool and raises both to 8/8, freezing 7/7. M5 E3
# appends episode generation and per-shot redo and raises both to 9/9, freezing 8/8.
# M6 E1 appends subtitle_save and raises both to 10/10, freezing 9/9. M6 E2
# appends audio_generate and raises both to 11/11, freezing 10/10. M6 E3 appends
# film_render/render_read and raises both to 13/13, freezing 11/11. M7 E1 appends
# season_save/season_read and raises both to 15/15, freezing 13/13.
VIDEO_TOOLS_VERSION = 15
VIDEO_RULES_VERSION = 15
LIVE_VIDEO_TOOLS_VERSION = 15
LIVE_VIDEO_RULES_VERSION = 15
WAIT_TIMEOUT_SECONDS = 600
# One episode may hold up to 20 shots and 20 Jobs, so one collection wait covers a
# whole episode; E3 raised the member limit from 16. Stored waits with fewer members
# stay valid.
WAIT_SET_LIMIT = 20
# Every tool this file adds after the frozen pre-M2 versions 1/2/3.
M2_TOOL_NAMES = ("storyboard_read", "storyboard_save", "shot_generate", "asset_read")
M2_STORYBOARD_TOOL_NAMES = ("storyboard_read", "storyboard_save")
# E1 appends the film tools; the frozen 4/4 variants must hide them again.
M3_TOOL_NAMES = ("film_assemble", "film_read")
# M5 E1 appends the adaptation tools; every pre-M5 variant must hide them.
M5_TOOL_NAMES = ("episode_plan", "episode_save", "episode_read", "bible_save", "bible_read")
# M5 E2 appends the human consistency review; pre-E2 variants hide it.
M5_E2_TOOL_NAMES = ("consistency_review",)
# M5 E3 appends the episode batch and the model-turn redo; pre-E3 variants hide them.
M5_E3_TOOL_NAMES = ("episode_generate", "shot_redo")
# M6 registers four post-production tools across E1..E3; E1 enables subtitle_save.
M6_TOOL_NAMES = ("subtitle_save", "audio_generate", "film_render", "render_read")
M6_E1_TOOL_NAMES = ("subtitle_save",)
M6_E2_TOOL_NAMES = ("audio_generate",)
M6_E3_TOOL_NAMES = ("film_render", "render_read")
# M7 appends the season tools in E1; batches and timelines stay later stages.
M7_TOOL_NAMES = ("season_save", "season_read")
M7_E1_TOOL_NAMES = ("season_save", "season_read")
PRE_M3_TOOL_NAMES = (*M2_TOOL_NAMES, *M3_TOOL_NAMES)
PRE_M5_TOOL_NAMES = (*PRE_M3_TOOL_NAMES, *M5_TOOL_NAMES)
PRE_E2_TOOL_NAMES = (*PRE_M5_TOOL_NAMES, *M5_E2_TOOL_NAMES, *M5_E3_TOOL_NAMES)
PRE_E1_M6_TOOL_NAMES = (*PRE_E2_TOOL_NAMES, *M6_E1_TOOL_NAMES)
PRE_E2_M6_TOOL_NAMES = (*PRE_E1_M6_TOOL_NAMES, *M6_E2_TOOL_NAMES)
PRE_E3_M6_TOOL_NAMES = (*PRE_E2_M6_TOOL_NAMES, *M6_E3_TOOL_NAMES)
PRE_M7_E1_TOOL_NAMES = (*PRE_E3_M6_TOOL_NAMES, *M7_E1_TOOL_NAMES)

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

ADAPTATION_RULES = """剧本/小说改编先离线切分：episode_plan 传入原文与可选标题，返回章节、场景、对白、角色提案、镜头草案与逐镜头估算。
episode_plan 只产出草案，不写存储、不登记任务、不产生费用；同一原文与 textSplitContract 必须得到同样的草案，不要手改草案结构后声称等价。
bible_save/bible_read 维护角色与风格圣经：角色只引用素材库中已导入的 assetId，缺参考图整批拒绝，不得编造素材 ID 或路径。
episode_save 必须显式给出 scenes（来自草案），按每集 10–20 镜头、60–90 秒、每场≤6 镜头的冻结预算登记；超限整批拒绝，不部分保存。
episode_save 不创建分镜、不登记视频任务、不提交供应商；episode_read 读取当前分集、场景、对白与可再生的镜头草案。"""

CONSISTENCY_RULES = """角色一致性用角色圣经与参考图：storyboard_save 可用 sceneId 把分镜绑定到该分集的一个场景（只能绑定一次）；每个镜头可用 characterIds 引用角色，i2v 给 1 个、flf2v 按首帧/尾帧顺序给 2 个，服务端取每个角色最新版本的 referenceAssetIds 并冻结到镜头。
同一镜头不能同时给 characterIds 与 referenceAssetIds；角色缺参考图、参考图不在本项目素材库或不是图像时整批拒绝（CHARACTER_ASSET_MISSING），不降级为 t2v、不借用其他项目素材。
绑定场记后，镜头请求的提示词会先拼上该集冻结的风格模板（style-prompt-zh-v1：风格名/题材/色调/镜头语言）与出场角色名，再拼镜头描述；同一分集与同一角色版本得到逐字节相同的提示词。
episode_read 返回逐角色的跨镜头引用证据（镜头的角色版本与参考素材）与已记录评分；只有用户明确给出 1–5 分时才调用 consistency_review 记录，不得自行编造评分，也不要把评分说成生成质量已达标。评分只追加记录，不改变镜头、任务、预算或费用。"""

VIDEO_RULES_V7 = VIDEO_RULES_V6 + "\n" + ADAPTATION_RULES
LIVE_VIDEO_RULES_V7 = LIVE_VIDEO_RULES_V6 + "\n" + ADAPTATION_RULES
VIDEO_RULES_V8 = VIDEO_RULES_V7 + "\n" + CONSISTENCY_RULES
LIVE_VIDEO_RULES_V8 = LIVE_VIDEO_RULES_V7 + "\n" + CONSISTENCY_RULES

EPISODE_GENERATION_RULES = """分集生成用 episode_generate：一次为整集所有已绑定分镜的镜头登记任务，必须显式 confirm，返回逐镜头 jobId 与估算、集级已用与剩余额度。
集级额度按分集冻结的 shotBudget 记账并跨调用累计：每集最多 jobCap 个新任务（默认 20）、episodeCapAmount（默认 60.00 CNY），逐镜头按 0.60 CNY/秒估算；已登记、重做、unknown 或失败的任务都占额度，不因报错或失败释放。集级额度只按 CNY 记账，不换汇：Kling/fal 的 USD 报价不进入集级批量登记。
任一场景未绑定分镜、分镜镜头数或时长与冻结场记不一致、逐镜头单价与集预算不一致，或超过 jobCap / episodeCapAmount 时整批拒绝并逐条列出超限镜头与桶；不部分登记，不自动拆分调用或降低规格绕过上限。
episode_generate 只登记，不提交、不等待。登记后用 await_job 一次传入本集全部 jobIds 做集合等待；等待按镜头返回成功与失败，失败镜头不会隐藏已交付镜头，也不会自动重提。
重做只由用户显式发起，用 shot_redo 并带上读到的分镜 revision：只追加目标镜头的新 ShotVersion 与新 Job，其他镜头的版本、任务与媒体保持不动；属于某个分集的重做同样占用集级额度。
episode_read 的 generation 给出逐镜头状态（unregistered/pending/delivered/failed）与集级余量；只按它、job_get 与 await_job 的事实报告，不声称未交付镜头可播放，也不把估算说成已扣费。"""

VIDEO_RULES_V9 = VIDEO_RULES_V8 + "\n" + EPISODE_GENERATION_RULES
LIVE_VIDEO_RULES_V9 = LIVE_VIDEO_RULES_V8 + "\n" + EPISODE_GENERATION_RULES

SUBTITLE_RULES = """字幕用 subtitle_save：传入 scope=storyboard 与 storyboardId（或 scope=episode 与 episodeId），省略 cues 时服务端按冻结的场记台词与成片时间线确定性对齐（subtitle-align-zh-v1），返回新的字幕版本与 SRT/ASS 受控地址。
对齐只使用该场已冻结的 SceneVersion.dialogue 与成片/分镜时间线，不重读原文、不重新猜台词；每条 cue 绑定所属镜头与场，单条 ≤48 字符、最多两行，每条轨 ≤200 条，每场 ≤40 条，cue 不得重叠、倒序或越出镜头边界。帧对齐结果是精确的微秒有理数，不因封装偏移累积漂移。
人工修正时显式传 cues（startMs/endMs 为整数毫秒）：服务端只校验与排序，不自动改写时间；不合法的重叠或越界整次拒绝并返回 SUBTITLE_CUE_CONFLICT / SUBTITLE_OVERFLOW。相同 cues 重放同一版本；修正追加新版本，历史版本与已渲染成片不删除、不降级。
字幕保存与渲染全程本地：不注册视频 Job、不调用供应商、不产生费用；没有绑定场记或成片时不得编造字幕，缺少字幕轨时渲染会返回 SUBTITLE_TRACK_MISSING。"""

VIDEO_RULES_V10 = VIDEO_RULES_V9 + "\n" + SUBTITLE_RULES
LIVE_VIDEO_RULES_V10 = LIVE_VIDEO_RULES_V9 + "\n" + SUBTITLE_RULES

AUDIO_RULES = """配音用 audio_generate：传入 episodeId、可选的台词范围（sceneId + paragraph，必要时带 text）与 speaker→voiceId 映射，必须显式 confirm；服务端只使用该集冻结的 SceneVersion.dialogue，不重读原文、不自行改写台词。
音色表来自 tts-providers-v1：episode_read 的 audio.provider.voices 给出当前模式可用的 voiceId 与价格；mock 模式只用本地确定性音色（零费用、零网络），live 模式的 CosyVoice 仍标注「文档支持，未做真实样本」。同一集同一 speaker 第一次生成时冻结 provider/model/voiceId/speed，之后必须一致，否则整批拒绝（VOICE_CONFLICT）。
额度按集从 Store 重算并跨调用累计：单条 ≤500 字符、每集 ≤2000 字符、每集 ≤40 次请求；价格已核实时单条 ≤0.10 CNY、每集 ≤0.40 CNY。任一超限整批拒绝、不部分提交、不自动拆批或换音色绕过；相同台词与相同音色重放复用原音频资产，不重复计费。
audio_generate 只生成配音资产与本地音频媒体，不登记视频 Job、不提交视频供应商、不改变镜头/成片；视频与 TTS 费用分桶，不换汇、不互相抵扣。没有台词、缺少音色映射或音色未核实时不得编造配音。"""
VIDEO_RULES_V11 = VIDEO_RULES_V10 + "\n" + AUDIO_RULES
LIVE_VIDEO_RULES_V11 = LIVE_VIDEO_RULES_V10 + "\n" + AUDIO_RULES

RENDER_RULES = """成片渲染用 film_render：scope=storyboard 需要 storyboardId，scope=episode 需要 episodeId；可指定 trackId/trackVersion、profile（720p-h264-aac-v1 或 1080p-h264-aac-v1，1080P 仅参数化上采样）、subtitleMode（soft/burn/both）、transitions 与 titleCards，必须显式 confirm。
服务端只使用已交付镜头与冻结的字幕轨：转场 afterShotId 必须是时间线中的镜头边界，fade/crossfade 时长 0.1–1.0 秒且不超过相邻任一镜头时长的三分之一；标题卡最多 4 条，startMs/durationMs 不得重叠或越出成片。跨场转场只在 scope=episode 的整集成片允许。
转场会重叠并缩短实际成片；字幕轨仍冻结在源时间线，渲染时会按同一重叠把 cue 夹到有效镜头窗口并写入旁挂 SRT。输出时长为有效时间线、与输入逐段一致，任何结构、时长、字幕轨或响度失败都返回 RENDER_* / LOUDNESS_OUT_OF_RANGE，不提交成片版本。
渲染全程本地 FFmpeg：不调用视频或 TTS 供应商、不登记 Job、不产生费用；同一指纹重放原成片，单场失败只标记该场且不删除已交付的逐场成片。render_read 用 renderId、episodeId 或 storyboardId 读取逐场进度、测量与失败阶段。"""
VIDEO_RULES_V13 = VIDEO_RULES_V11 + "\n" + RENDER_RULES
LIVE_VIDEO_RULES_V13 = LIVE_VIDEO_RULES_V11 + "\n" + RENDER_RULES

SEASON_RULES = """多集用季来组织：season_save 保存季标题与 1–4 个有序 episodeIds（本项目已保存的分集 ID），新建可省略 seasonId 与 expectedRevision，修改必须带上读到的 revision 与 seasonId。
一次调用只在季当前版本上追加新版本：改名、重排、增删集都会产生新的 SeasonVersion，旧版本逐字节保留；相同标题与相同顺序重放同一版本，不追加空版本。一个项目最多 8 个季，一集至多属于一个季的当前版本；换季要先在原季的新版本中移除，再加入目标季。
季只保存成员关系与标题，不复制角色/风格，不拥有成片、场记、预算或 Job；season_save 不登记任务、不调用模型、不产生费用。
season_read 只读返回季、成员集摘要（版本、场数、镜头数、时长）与全部季列表；省略 seasonId 时读取本项目最新创建的季。E1 没有批次与时间线，season_read 的 batch 字段恒为 null。"""
VIDEO_RULES_V15 = VIDEO_RULES_V13 + "\n" + SEASON_RULES
LIVE_VIDEO_RULES_V15 = LIVE_VIDEO_RULES_V13 + "\n" + SEASON_RULES

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
    # E2: ordered character IDs; the server resolves one exact reference image each.
    character_ids: list[CanonicalId] = Field(default_factory=list, max_length=2)

    @field_validator("camera_movement")
    @classmethod
    def blank_movement(cls, value):
        if value is None:
            return None
        return value.strip() or None

    @model_validator(mode="after")
    def references_match_mode(self):
        frames = {"t2v": 0, "i2v": 1, "flf2v": 2}[self.mode]
        if self.character_ids:
            if self.reference_asset_ids:
                raise ValueError("同一镜头不能同时给出 characterIds 与 referenceAssetIds")
            if self.mode == "t2v" or len(self.character_ids) != frames:
                raise ValueError("镜头角色数必须与该模式的首尾帧数量一致")
            if len(set(self.character_ids)) != len(self.character_ids):
                raise ValueError("同一镜头不能重复引用同一个角色")
            return self
        if len(self.reference_asset_ids) != frames:
            raise ValueError("镜头模式要求的参考图数量不匹配")
        if len(set(self.reference_asset_ids)) != len(self.reference_asset_ids):
            raise ValueError("同一镜头的参考图不能重复")
        return self


class StoryboardSave(Arguments):
    storyboard_id: CanonicalId | None = None
    expected_version: int | None = Field(default=None, ge=1)
    # E2: bind the scene's single storyboard while saving it.
    scene_id: CanonicalId | None = None
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


class SubtitleCueArgs(Arguments):
    start_ms: int = Field(ge=0, le=90_000, description="起始时间，整数毫秒，精确到毫秒。")
    end_ms: int = Field(gt=0, le=90_000, description="结束时间，整数毫秒；必须晚于 startMs。")
    text: str = Field(min_length=1, max_length=48)
    speaker: str | None = Field(default=None, min_length=1, max_length=40)
    shot_id: CanonicalId | None = None
    scene_id: CanonicalId | None = None

    @model_validator(mode="after")
    def ordered_window(self):
        if self.end_ms <= self.start_ms:
            raise ValueError("endMs must be later than startMs")
        return self


class SubtitleSave(Arguments):
    scope: Literal["storyboard", "episode"]
    storyboard_id: CanonicalId | None = None
    episode_id: CanonicalId | None = None
    track_id: CanonicalId | None = None
    expected_revision: int | None = Field(default=None, ge=0)
    cues: list[SubtitleCueArgs] | None = Field(
        default=None,
        max_length=200,
        description="人工修正时显式给出；省略则按冻结台词与时间线确定性对齐。空数组表示清空。",
    )

    @model_validator(mode="after")
    def coherent_target(self):
        target = self.storyboard_id if self.scope == "storyboard" else self.episode_id
        other = self.episode_id if self.scope == "storyboard" else self.storyboard_id
        if target is None or other is not None:
            raise ValueError("The scope needs exactly its own target id")
        return self


class SeasonSaveArgs(Arguments):
    season_id: CanonicalId | None = Field(default=None, description="修改已有季时提供；新建时省略。")
    title: str = Field(min_length=1, max_length=120, description="季标题。")
    episode_ids: list[CanonicalId] = Field(
        min_length=1,
        max_length=4,
        description="1–4 个本项目分集 ID，顺序即季内顺序；换季要先在原季移除。",
    )
    expected_revision: int | None = Field(
        default=None, ge=0, description="修改时必须提供 season_read 返回的 revision。"
    )


class SeasonReadArgs(Arguments):
    season_id: CanonicalId | None = Field(default=None, description="省略时读取本项目最新创建的季。")


class EpisodePlanArgs(Arguments):
    text: str = Field(
        min_length=1,
        max_length=20000,
        description="纯文本剧本或小说章节正文（txt/markdown）。切分是确定性的，同一原文得到同样的草案。",
    )
    title: str | None = Field(default=None, max_length=120)


class SceneDialogueArgs(Arguments):
    text: str = Field(min_length=1, max_length=300)
    speaker: str | None = Field(default=None, min_length=1, max_length=40)
    paragraph: int = Field(default=0, ge=0, le=100)


class SceneSpec(Arguments):
    id: CanonicalId | None = None
    heading: str = Field(min_length=1, max_length=120)
    location: str | None = Field(default=None, min_length=1, max_length=120)
    time_of_day: TimeOfDay = "unknown"
    synopsis: str = Field(min_length=1, max_length=1000)
    character_ids: list[CanonicalId] = Field(default_factory=list, max_length=16)
    dialogue: list[SceneDialogueArgs] = Field(default_factory=list, max_length=40)
    shot_count: int = Field(ge=1, le=SCENE_MAX_SHOTS)
    duration_seconds: int = Field(ge=2, le=90)


class EpisodeSave(Arguments):
    episode_id: CanonicalId | None = None
    expected_revision: int | None = Field(default=None, ge=0)
    title: str = Field(min_length=1, max_length=120)
    style_id: CanonicalId | None = None
    source_refs: list[ArtifactRef] | None = Field(default=None, max_length=16)
    target_duration_seconds: int | None = Field(default=None, ge=60, le=90)
    scenes: list[SceneSpec] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def require_revision(self):
        if bool(self.episode_id) != bool(self.expected_revision is not None):
            raise ValueError("修改时必须同时提供 episodeId 和 expectedRevision")
        return self


class EpisodeRead(Arguments):
    episode_id: CanonicalId | None = None


class VoiceArgs(Arguments):
    description: str = Field(min_length=1, max_length=200)
    timbre: str | None = Field(default=None, min_length=1, max_length=60)
    pace: Literal["slow", "normal", "fast"] = "normal"


class CharacterLinkArgs(Arguments):
    character_id: CanonicalId
    relation: str = Field(min_length=1, max_length=60)


class BibleSave(Arguments):
    kind: Literal["character", "style"]
    id: CanonicalId | None = None
    expected_revision: int | None = Field(default=None, ge=0)
    name: str = Field(min_length=1, max_length=60)
    # Character fields.
    aliases: list[str] = Field(default_factory=list, max_length=8)
    appearance: str | None = Field(default=None, min_length=1, max_length=600)
    wardrobe: str | None = Field(default=None, min_length=1, max_length=300)
    voice: VoiceArgs | None = None
    relationships: list[CharacterLinkArgs] = Field(default_factory=list, max_length=16)
    reference_asset_ids: list[CanonicalId] = Field(default_factory=list, max_length=4)
    # Style fields.
    genre: list[str] = Field(default_factory=list, max_length=8)
    palette: str | None = Field(default=None, min_length=1, max_length=200)
    camera_language: str | None = Field(default=None, min_length=1, max_length=300)

    @model_validator(mode="after")
    def kind_specific_fields(self):
        if bool(self.id) != bool(self.expected_revision is not None):
            raise ValueError("修改时必须同时提供 id 和 expectedRevision")
        if self.kind == "character":
            if self.appearance is None:
                raise ValueError("角色必须提供 appearance")
            if self.palette is not None or self.camera_language is not None or self.genre:
                raise ValueError("角色不能携带风格字段")
        else:
            if self.palette is None or self.camera_language is None:
                raise ValueError("风格必须提供 palette 与 cameraLanguage")
            if (
                self.appearance
                or self.wardrobe
                or self.voice
                or self.aliases
                or self.relationships
                or self.reference_asset_ids
            ):
                raise ValueError("风格不能携带角色字段")
        return self


class BibleRead(Arguments):
    kind: Literal["character", "style"]
    id: CanonicalId | None = None


class ConsistencyReviewArgs(Arguments):
    """One human 1-5 consistency score, bound to the evidence the Store recomputes."""

    episode_id: CanonicalId
    character_id: CanonicalId
    score: int = Field(ge=1, le=5, description="人工一致性评分：1 最差、5 最好；只按用户明确给出的分数记录。")
    note: str | None = Field(default=None, min_length=1, max_length=500)
    reviewer: str = Field(default="user", min_length=1, max_length=60)


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
                "tts": tts_capabilities(mode),
            },
            instructions=LIVE_VIDEO_RULES_V15 if live else VIDEO_RULES_V15,
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

    def episode_generate(args: dict, store: FileStore, context: ToolExecutionContext):
        check_store(store)
        return service.episode_generate(args, context=context)

    def shot_redo(args: dict, store: FileStore, context: ToolExecutionContext):
        check_store(store)
        return service.redo_shot_tool(args, context=context)

    def read_storyboard_with_subtitle(args, state, project_id):
        data = read_storyboard(args, state, project_id)
        storyboard_id = data.get("storyboardId")
        data["subtitle"] = (
            subtitle_for(state, project_id, storyboard_id=storyboard_id, include_digests=False)
            if storyboard_id
            else None
        )
        return data

    def read_episode_with_subtitle(args, state, project_id):
        data = read_episode(args, state, project_id)
        episode_id = data.get("episodeId")
        data["subtitle"] = (
            subtitle_for(state, project_id, episode_id=episode_id, include_digests=False)
            if episode_id
            else None
        )
        data["audio"] = episode_audio_view(state, project_id, episode_id, mode=mode) if episode_id else None
        data["render"] = (
            service.renders.episode_summary(state, project_id, episode_id) if episode_id else None
        )
        return data

    def save_subtitle(args, state, project_id):
        return save_subtitles(args, state, project_id)

    async def generate_audio(args, store, context):
        check_store(store)
        return await service.audio.generate(args, context=context)

    async def render_film(args, store, context):
        check_store(store)
        return await service.renders.render(
            args,
            project_id=context.project_id,
            operation_key=context.operation_key,
            args=args,
        )

    def read_render(args, state, project_id):
        return service.renders.read(args, project_id)

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
            "episode_generate",
            "为已保存分集登记整批视频任务：一次覆盖该集所有已绑定分镜的全部镜头，必须显式确认；"
            "返回逐镜头 jobId 与估算、集级已用与剩余额度。集级额度按冻结 shotBudget 跨调用累计"
            "（jobCap 个任务 / episodeCapAmount 与逐镜头单价），任一镜头越界即整批拒绝、不部分登记；"
            "同一镜头相同请求复用原任务。只登记，不提交、不等待。",
            EpisodeGenerationArgs,
            "write",
            context_execute=episode_generate,
            feature="video",
        ),
        ToolDefinition(
            "shot_redo",
            "只由用户显式发起的镜头重做：为指定分镜版本的目标镜头追加一个新任务，其他镜头、任务与媒体不动；"
            "必须带上读到的分镜 revision，同一调用重放原结果。分镜属于某个分集时同样占用集级额度。只登记，不提交。",
            ShotRedoToolArgs,
            "write",
            context_execute=shot_redo,
            feature="video",
        ),
        ToolDefinition(
            "storyboard_read",
            "读取当前项目的结构化分镜：当前版本的有序镜头（稳定 shotId、景别、时长、模式、参考图）与各镜头版本摘要，"
            "以及该分镜的字幕轨与逐条 cue；省略 storyboardId 时读取最新分镜。只读，不创建视频任务。",
            StoryboardRead,
            "read",
            read_storyboard_with_subtitle,
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
            "可用 sceneId 把分镜绑定到分集的一个场景（只能绑定一次）；镜头可用 characterIds 引用角色，"
            "i2v 给 1 个、flf2v 按首帧/尾帧顺序给 2 个，服务端取该角色最新参考图并冻结；"
            "角色缺参考图或素材不在本项目时整批拒绝。只保存分镜结构，不创建视频任务。",
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
        ToolDefinition(
            "episode_plan",
            "把纯文本剧本/小说章节离线切分为分集草案：章节、场景、对白、角色提案、镜头草案与逐镜头估算。"
            "确定性（textSplitContract=text-split-zh-v1），不调用模型、不写存储、不登记任务、不产生费用。",
            EpisodePlanArgs,
            "read",
            lambda args, _state, _project_id: plan_episode(args["text"], title=args.get("title")),
            feature="video",
        ),
        ToolDefinition(
            "episode_save",
            "保存或修改分集：scenes 来自 episode_plan 的草案，一次原子提交场景与分集。"
            "每集 10–20 镜头、60–90 秒、每场≤6 镜头；超限整批拒绝。修改时提供 episodeId 与 expectedRevision。"
            "只保存分集结构与台词，不创建分镜、不登记视频任务、不产生费用。",
            EpisodeSave,
            "write",
            save_episode,
            feature="video",
        ),
        ToolDefinition(
            "episode_read",
            "读取当前项目的分集、场景、台词与可再生的镜头草案（含逐镜头估算），以及该集冻结的风格模板、"
            "逐角色的跨镜头引用证据（镜头、角色版本、参考素材是否一致）与已记录的人工一致性评分；"
            "省略 episodeId 时读取本项目最近保存的分集。只读，不修改、不登记任务。",
            EpisodeRead,
            "read",
            read_episode_with_subtitle,
            feature="video",
        ),
        ToolDefinition(
            "subtitle_save",
            "保存字幕：scope=storyboard 需 storyboardId，scope=episode 需 episodeId。省略 cues 时按该场已冻结的"
            "SceneVersion.dialogue 与成片/分镜时间线确定性对齐（subtitle-align-zh-v1），返回新的字幕版本与 SRT/ASS 地址；"
            "显式给出 cues（startMs/endMs 整数毫秒）则按人工修正校验，重叠、倒序、越界或超过 48 字符/200 条整次拒绝。"
            "相同 cues 重放同一版本；修正追加新版本，历史版本保留。全程本地，不调用供应商、不登记视频任务。",
            SubtitleSave,
            "write",
            save_subtitle,
            feature="video",
        ),
        ToolDefinition(
            "audio_generate",
            "为分集台词生成配音：必须显式确认，按 speaker→voiceId 映射调用当前模式的 TTS（mock 为本地确定性音色，"
            "live 为 CosyVoice）；只使用该集冻结的台词，单条 ≤500 字符、每集 ≤2000 字符/40 次请求，价格已核实时"
            "单条 ≤0.10 CNY、每集 ≤0.40 CNY，任一超限整批拒绝。同一集同一 speaker 首次生成即冻结音色，"
            "不同音色返回 VOICE_CONFLICT；相同台词与音色重放复用原音频资产。只生成本地音频资产与媒体，"
            "不登记视频 Job、不提交视频供应商。",
            AudioGenerateArgs,
            "write",
            None,
            async_write_execute=generate_audio,
            feature="video",
        ),
        ToolDefinition(
            "film_render",
            "把已交付镜头、冻结字幕轨与可选配音/配乐渲染为一条成片：scope=storyboard 或 episode，"
            "可指定 720P/1080P profile、soft/burn/both 字幕、cut/fade/crossfade 转场与标题卡，必须显式 confirm。"
            "转场会重叠并缩短实际时间线；字幕轨必须与源镜头时间线逐段相等，渲染按同一重叠夹取 cue。"
            "同一指纹返回原成片；任一编码、时长或响度校验失败返回 RENDER_*，不提交成片版本。"
            "全程本地 FFmpeg，不登记视频 Job、不调用供应商、不产生费用。",
            FilmRenderArgs,
            "write",
            None,
            async_write_execute=render_film,
            feature="video",
        ),
        ToolDefinition(
            "render_read",
            "只读渲染进度：用 renderId、episodeId、storyboardId 或 filmId 读取逐场状态、失败阶段、"
            "预估耗时与成片测量；不重新渲染、不修改文件、不调用供应商。",
            RenderReadArgs,
            "read",
            read_render,
            feature="video",
        ),
        ToolDefinition(
            "season_save",
            "新建或修改季：保存季标题与 1–4 个有序 episodeIds（本项目已保存的分集 ID），顺序即季内顺序。"
            "新建可省略 seasonId 与 expectedRevision，修改必须同时提供并匹配 season_read 返回的 revision。"
            "每次有效修改追加新的 SeasonVersion，旧版本逐字节保留；相同标题与相同顺序重放同一版本。"
            "一个项目最多 8 个季，一集至多属于一个季的当前版本；换季要先在原季移除再加入目标季。"
            "只保存季成员关系与标题，不复制角色/风格，不拥有成片、预算或 Job；不登记任务、不调用模型、不产生费用。",
            SeasonSaveArgs,
            "write",
            save_season,
            feature="video",
        ),
        ToolDefinition(
            "season_read",
            "读取季及其成员集摘要（版本、场数、镜头数、目标时长）与全部季列表；省略 seasonId 时读取本项目最新创建的季。"
            "只读，不登记任务、不提交供应商；E1 没有批次与时间线，batch 字段恒为 null。",
            SeasonReadArgs,
            "read",
            read_season,
            feature="video",
        ),
        ToolDefinition(
            "consistency_review",
            "记录用户给出的人工角色一致性评分（1–5）：服务端在同一事务内重新计算该角色在这个分集已绑定分镜中的"
            "全部镜头、角色版本与参考素材，把证据与分数一起追加到分集；没有任何引用该角色的镜头时整次拒绝"
            "（CONSISTENCY_EVIDENCE_MISSING）。只有用户明确给出分数时才调用；不改变镜头、任务、预算或费用，"
            "历史评分不可改写。",
            ConsistencyReviewArgs,
            "write",
            record_consistency_review,
            feature="video",
        ),
        ToolDefinition(
            "bible_save",
            "新建或修改角色/风格圣经：kind=character 需要 name 与 appearance，可引用素材库 referenceAssetIds；"
            "kind=style 需要 palette 与 cameraLanguage。修改时提供 id 与 expectedRevision；版本只增不改。"
            "只保存元数据，不导入素材、不调用模型、不登记任务。",
            BibleSave,
            "write",
            save_bible,
            feature="video",
        ),
        ToolDefinition(
            "bible_read",
            "读取当前项目的角色或风格圣经（kind 必填）：返回名称、外观、服装、声音描述、关系、参考素材 ID 与版本；"
            "省略 id 时列出该项目的全部条目。只读，不返回本地路径。",
            BibleRead,
            "read",
            read_bible,
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
            exclude=PRE_M7_E1_TOOL_NAMES,
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
            exclude=(
                *M3_TOOL_NAMES,
                *M5_TOOL_NAMES,
                *M5_E2_TOOL_NAMES,
                *M5_E3_TOOL_NAMES,
                *M6_E1_TOOL_NAMES,
                *M6_E2_TOOL_NAMES,
                *M6_E3_TOOL_NAMES,
                *M7_E1_TOOL_NAMES,
            ),
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
            exclude=(
                *M5_TOOL_NAMES,
                *M5_E2_TOOL_NAMES,
                *M5_E3_TOOL_NAMES,
                *M6_E1_TOOL_NAMES,
                *M6_E2_TOOL_NAMES,
                *M6_E3_TOOL_NAMES,
                *M7_E1_TOOL_NAMES,
            ),
            definitions={"video_generate": frozen_generate},
        )
        registry.register_tools_variant(
            6,
            features=[
                ToolFeature(
                    name="video",
                    configuration={
                        "mode": "live",
                        "toolsVersion": 6,
                        "rulesVersion": 6,
                        "capabilities": capabilities,
                    },
                    instructions=LIVE_VIDEO_RULES_V6,
                    bypass_answer_cache=True,
                )
            ],
            exclude=(
                *M5_TOOL_NAMES,
                *M5_E2_TOOL_NAMES,
                *M5_E3_TOOL_NAMES,
                *M6_E1_TOOL_NAMES,
                *M6_E2_TOOL_NAMES,
                *M6_E3_TOOL_NAMES,
                *M7_E1_TOOL_NAMES,
            ),
        )
        # 7/7 introduced the episode/bible tools; its video_generate schema is the
        # same six-argument intent as 6/6, so no override is needed.
        registry.register_tools_variant(
            7,
            features=[
                ToolFeature(
                    name="video",
                    configuration={
                        "mode": "live",
                        "toolsVersion": 7,
                        "rulesVersion": 7,
                        "capabilities": capabilities,
                    },
                    instructions=LIVE_VIDEO_RULES_V7,
                    bypass_answer_cache=True,
                )
            ],
            exclude=(
                *M5_E2_TOOL_NAMES,
                *M5_E3_TOOL_NAMES,
                *M6_E1_TOOL_NAMES,
                *M6_E2_TOOL_NAMES,
                *M6_E3_TOOL_NAMES,
                *M7_E1_TOOL_NAMES,
            ),
        )
        # 8/8 added the consistency review; E3 freezes it without the new writers.
        registry.register_tools_variant(
            8,
            features=[
                ToolFeature(
                    name="video",
                    configuration={
                        "mode": "live",
                        "toolsVersion": 8,
                        "rulesVersion": 8,
                        "capabilities": capabilities,
                    },
                    instructions=LIVE_VIDEO_RULES_V8,
                    bypass_answer_cache=True,
                )
            ],
            exclude=(
                *M5_E3_TOOL_NAMES,
                *M6_E1_TOOL_NAMES,
                *M6_E2_TOOL_NAMES,
                *M6_E3_TOOL_NAMES,
                *M7_E1_TOOL_NAMES,
            ),
        )
        # 9/9 froze the M5 E3 episode surface; M6 E1 hides its new post-production tool.
        registry.register_tools_variant(
            9,
            features=[
                ToolFeature(
                    name="video",
                    configuration={
                        "mode": "live",
                        "toolsVersion": 9,
                        "rulesVersion": 9,
                        "capabilities": capabilities,
                    },
                    instructions=LIVE_VIDEO_RULES_V9,
                    bypass_answer_cache=True,
                )
            ],
            exclude=(*M6_E1_TOOL_NAMES, *M6_E2_TOOL_NAMES, *M6_E3_TOOL_NAMES, *M7_E1_TOOL_NAMES),
        )
        # 10/10 froze the M6 E1 subtitle surface; M6 E2 hides only its new TTS writer.
        registry.register_tools_variant(
            10,
            features=[
                ToolFeature(
                    name="video",
                    configuration={
                        "mode": "live",
                        "toolsVersion": 10,
                        "rulesVersion": 10,
                        "capabilities": capabilities,
                    },
                    instructions=LIVE_VIDEO_RULES_V10,
                    bypass_answer_cache=True,
                )
            ],
            exclude=(*M6_E2_TOOL_NAMES, *M6_E3_TOOL_NAMES, *M7_E1_TOOL_NAMES),
        )
        # 11/11 froze the M6 E2 audio surface; M6 E3 hides only its two render tools.
        registry.register_tools_variant(
            11,
            features=[
                ToolFeature(
                    name="video",
                    configuration={
                        "mode": "live",
                        "toolsVersion": 11,
                        "rulesVersion": 11,
                        "capabilities": capabilities,
                    },
                    instructions=LIVE_VIDEO_RULES_V11,
                    bypass_answer_cache=True,
                )
            ],
            exclude=(*M6_E3_TOOL_NAMES, *M7_E1_TOOL_NAMES),
        )
        # 13/13 froze the M6 E3 render surface; M7 E1 hides only its two season tools.
        registry.register_tools_variant(
            13,
            features=[
                ToolFeature(
                    name="video",
                    configuration={
                        "mode": "live",
                        "toolsVersion": 13,
                        "rulesVersion": 13,
                        "capabilities": capabilities,
                        "tts": tts_capabilities(mode),
                    },
                    instructions=LIVE_VIDEO_RULES_V13,
                    bypass_answer_cache=True,
                )
            ],
            exclude=M7_E1_TOOL_NAMES,
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
            exclude=PRE_M7_E1_TOOL_NAMES,
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
            exclude=PRE_M7_E1_TOOL_NAMES,
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
            exclude=(
                *M3_TOOL_NAMES,
                *M5_TOOL_NAMES,
                *M5_E2_TOOL_NAMES,
                *M5_E3_TOOL_NAMES,
                *M6_E1_TOOL_NAMES,
                *M6_E2_TOOL_NAMES,
                *M6_E3_TOOL_NAMES,
                *M7_E1_TOOL_NAMES,
            ),
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
            exclude=(
                *M5_TOOL_NAMES,
                *M5_E2_TOOL_NAMES,
                *M5_E3_TOOL_NAMES,
                *M6_E1_TOOL_NAMES,
                *M6_E2_TOOL_NAMES,
                *M6_E3_TOOL_NAMES,
                *M7_E1_TOOL_NAMES,
            ),
        )
        registry.register_tools_variant(
            6,
            features=[
                ToolFeature(
                    name="video",
                    configuration={
                        "mode": "mock",
                        "toolsVersion": 6,
                        "rulesVersion": 6,
                        "capabilities": capabilities,
                    },
                    instructions=VIDEO_RULES_V6,
                    bypass_answer_cache=True,
                )
            ],
            exclude=(
                *M5_TOOL_NAMES,
                *M5_E2_TOOL_NAMES,
                *M5_E3_TOOL_NAMES,
                *M6_E1_TOOL_NAMES,
                *M6_E2_TOOL_NAMES,
                *M6_E3_TOOL_NAMES,
                *M7_E1_TOOL_NAMES,
            ),
        )
        registry.register_tools_variant(
            7,
            features=[
                ToolFeature(
                    name="video",
                    configuration={
                        "mode": "mock",
                        "toolsVersion": 7,
                        "rulesVersion": 7,
                        "capabilities": capabilities,
                    },
                    instructions=VIDEO_RULES_V7,
                    bypass_answer_cache=True,
                )
            ],
            exclude=(
                *M5_E2_TOOL_NAMES,
                *M5_E3_TOOL_NAMES,
                *M6_E1_TOOL_NAMES,
                *M6_E2_TOOL_NAMES,
                *M6_E3_TOOL_NAMES,
                *M7_E1_TOOL_NAMES,
            ),
        )
        registry.register_tools_variant(
            8,
            features=[
                ToolFeature(
                    name="video",
                    configuration={
                        "mode": "mock",
                        "toolsVersion": 8,
                        "rulesVersion": 8,
                        "capabilities": capabilities,
                    },
                    instructions=VIDEO_RULES_V8,
                    bypass_answer_cache=True,
                )
            ],
            exclude=(
                *M5_E3_TOOL_NAMES,
                *M6_E1_TOOL_NAMES,
                *M6_E2_TOOL_NAMES,
                *M6_E3_TOOL_NAMES,
                *M7_E1_TOOL_NAMES,
            ),
        )
        # 9/9 froze the M5 E3 episode surface; M6 E1 hides its new post-production tool.
        registry.register_tools_variant(
            9,
            features=[
                ToolFeature(
                    name="video",
                    configuration={
                        "mode": "mock",
                        "toolsVersion": 9,
                        "rulesVersion": 9,
                        "capabilities": capabilities,
                    },
                    instructions=VIDEO_RULES_V9,
                    bypass_answer_cache=True,
                )
            ],
            exclude=(*M6_E1_TOOL_NAMES, *M6_E2_TOOL_NAMES, *M6_E3_TOOL_NAMES, *M7_E1_TOOL_NAMES),
        )
        # 10/10 froze the M6 E1 subtitle surface; M6 E2 hides only its new TTS writer.
        registry.register_tools_variant(
            10,
            features=[
                ToolFeature(
                    name="video",
                    configuration={
                        "mode": "mock",
                        "toolsVersion": 10,
                        "rulesVersion": 10,
                        "capabilities": capabilities,
                    },
                    instructions=VIDEO_RULES_V10,
                    bypass_answer_cache=True,
                )
            ],
            exclude=(*M6_E2_TOOL_NAMES, *M6_E3_TOOL_NAMES, *M7_E1_TOOL_NAMES),
        )
        # 11/11 froze the M6 E2 audio surface; M6 E3 hides only its two render tools.
        registry.register_tools_variant(
            11,
            features=[
                ToolFeature(
                    name="video",
                    configuration={
                        "mode": "mock",
                        "toolsVersion": 11,
                        "rulesVersion": 11,
                        "capabilities": capabilities,
                    },
                    instructions=VIDEO_RULES_V11,
                    bypass_answer_cache=True,
                )
            ],
            exclude=(*M6_E3_TOOL_NAMES, *M7_E1_TOOL_NAMES),
        )
        # 13/13 froze the M6 E3 render surface; M7 E1 hides only its two season tools.
        registry.register_tools_variant(
            13,
            features=[
                ToolFeature(
                    name="video",
                    configuration={
                        "mode": "mock",
                        "toolsVersion": 13,
                        "rulesVersion": 13,
                        "capabilities": capabilities,
                        "tts": tts_capabilities(mode),
                    },
                    instructions=VIDEO_RULES_V13,
                    bypass_answer_cache=True,
                )
            ],
            exclude=M7_E1_TOOL_NAMES,
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
