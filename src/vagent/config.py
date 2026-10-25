import os
import re
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import uuid4

import yaml
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError, model_validator

from vagent.errors import AppError
from vagent.video.contracts import WAN_MODEL, WAN_REGION, Money, WorkspaceId


def _integer_setting(value):
    """The settings form submits numeric inputs as strings; parse only integers."""
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return value


def _seconds_setting(value):
    """Submit interval/cooldown arrive as strings from the settings form or YAML."""
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return value
    return value


IntegerSetting = Annotated[int, BeforeValidator(_integer_setting)]
SecondsSetting = Annotated[float, BeforeValidator(_seconds_setting)]

# Public configuration name -> (runtime attribute, environment name, default).
VIDEO_SETTINGS = {
    "videoMode": ("video_mode", "VAGENT_VIDEO_MODE", "off"),
    "videoApiKey": ("video_api_key", "VAGENT_DASHSCOPE_KEY", None),
    "videoWorkspaceId": ("video_workspace_id", "VAGENT_DASHSCOPE_WORKSPACE_ID", None),
    "videoProvider": ("video_provider", "VAGENT_VIDEO_PROVIDER", "wan"),
    "videoModel": ("video_model", "VAGENT_VIDEO_MODEL", WAN_MODEL),
    "videoRegion": ("video_region", "VAGENT_VIDEO_REGION", WAN_REGION),
    "videoMaxJobCost": ("video_max_job_cost", "VAGENT_VIDEO_MAX_JOB_COST", "3.00"),
    "videoMaxNewJobsPerRun": (
        "video_max_new_jobs_per_run",
        "VAGENT_VIDEO_MAX_NEW_JOBS_PER_RUN",
        4,
    ),
    "videoMaxRunCost": ("video_max_run_cost", "VAGENT_VIDEO_MAX_RUN_COST", "12.00"),
    "klingApiKey": ("kling_api_key", "VAGENT_KLING_API_KEY", None),
    "falApiKey": ("fal_api_key", "FAL_KEY", None),
    "klingMaxJobCost": ("kling_max_job_cost", "VAGENT_KLING_MAX_JOB_COST", "0.60"),
    "klingMaxRunCost": ("kling_max_run_cost", "VAGENT_KLING_MAX_RUN_COST", "2.40"),
    "klingMaxJobUnits": ("kling_max_job_units", "VAGENT_KLING_MAX_JOB_UNITS", 4),
    "klingMaxRunUnits": ("kling_max_run_units", "VAGENT_KLING_MAX_RUN_UNITS", 16),
    "falMaxJobCost": ("fal_max_job_cost", "VAGENT_FAL_MAX_JOB_COST", "0.50"),
    "falMaxRunCost": ("fal_max_run_cost", "VAGENT_FAL_MAX_RUN_COST", "2.00"),
    # M8 E2 submit throttling (§5.4): local settings may only extend these values.
    "videoSubmitIntervalSeconds": (
        "video_submit_interval_seconds",
        "VAGENT_VIDEO_SUBMIT_INTERVAL_SECONDS",
        2.0,
    ),
    "videoRateLimitCooldownSeconds": (
        "video_rate_limit_cooldown_seconds",
        "VAGENT_VIDEO_RATE_LIMIT_COOLDOWN_SECONDS",
        60.0,
    ),
}
_SECRET_VIDEO_SETTINGS = frozenset({"videoApiKey", "klingApiKey", "falApiKey"})
_SETTINGS = {"apiKey": ("api_key", None, None), "model": ("model", None, "deepseek-flash"), **VIDEO_SETTINGS}


@dataclass(frozen=True)
class Config:
    home: Path
    api_key: str | None = field(repr=False)
    model: str = "deepseek-flash"
    context_bytes: int = 65536
    skills_root: Path | None = None
    redis_url: str | None = field(default=None, repr=False)
    cache_ttl: int = 3600
    mcp_config: Path | None = None
    mcp_local: bool = False
    sources: dict[str, str] = field(default_factory=dict)
    video_mode: Literal["off", "mock", "live"] = "off"
    video_api_key: str | None = field(default=None, repr=False)
    video_workspace_id: str | None = None
    video_provider: str = "wan"
    video_model: str = WAN_MODEL
    video_region: str = WAN_REGION
    video_max_job_cost: str = "3.00"
    video_max_new_jobs_per_run: int = 4
    video_max_run_cost: str = "12.00"
    kling_api_key: str | None = field(default=None, repr=False)
    fal_api_key: str | None = field(default=None, repr=False)
    kling_max_job_cost: str = "0.60"
    kling_max_run_cost: str = "2.40"
    kling_max_job_units: int = 4
    kling_max_run_units: int = 16
    fal_max_job_cost: str = "0.50"
    fal_max_run_cost: str = "2.00"
    video_submit_interval_seconds: float = 2.0
    video_rate_limit_cooldown_seconds: float = 60.0

    def __post_init__(self):
        if self.video_mode not in {"off", "mock", "live"}:
            raise AppError("INVALID_VIDEO_MODE", "VAGENT_VIDEO_MODE 只支持 off、mock 或 live，修改后需重启。")
        try:
            for attr, _, default in VIDEO_SETTINGS.values():
                if default is not None and getattr(self, attr) is None:
                    raise ValueError("Missing required setting")
            values = LocalSettings.model_validate(
                {name: getattr(self, attr) for name, (attr, _, _) in VIDEO_SETTINGS.items()}
            )
            for name, (attr, _, _) in VIDEO_SETTINGS.items():
                object.__setattr__(self, attr, getattr(values, name))
        except ValueError:
            raise AppError(
                "VIDEO_CONFIG_INVALID", "视频配置无效，请检查模型、地域、业务空间和十进制金额上限。"
            ) from None


class LocalSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    apiKey: str | None = Field(default=None, pattern=r"^[\x21-\x7e]{1,512}$", repr=False)
    model: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,99}$")
    videoMode: Literal["off", "mock", "live"] | None = None
    videoApiKey: str | None = Field(default=None, pattern=r"^[\x21-\x7e]{1,512}$", repr=False)
    videoWorkspaceId: WorkspaceId | None = None
    videoProvider: Literal["wan", "kling", "fal"] | None = None
    videoModel: Literal["wan2.7-t2v-2026-06-12"] | None = None
    videoRegion: Literal["cn-beijing"] | None = None
    videoMaxJobCost: Money | None = None
    videoMaxNewJobsPerRun: IntegerSetting | None = Field(default=None, ge=1, le=4)
    videoMaxRunCost: Money | None = None
    klingApiKey: str | None = Field(default=None, pattern=r"^[\x21-\x7e]{1,512}$", repr=False)
    falApiKey: str | None = Field(default=None, pattern=r"^[\x21-\x7e]{1,512}$", repr=False)
    klingMaxJobCost: Money | None = None
    klingMaxRunCost: Money | None = None
    klingMaxJobUnits: IntegerSetting | None = Field(default=None, ge=0, le=4)
    klingMaxRunUnits: IntegerSetting | None = Field(default=None, ge=0, le=16)
    falMaxJobCost: Money | None = None
    falMaxRunCost: Money | None = None
    videoSubmitIntervalSeconds: SecondsSetting | None = Field(default=None, ge=2.0, le=60.0)
    videoRateLimitCooldownSeconds: SecondsSetting | None = Field(default=None, ge=60.0, le=900.0)


class ConfigUpdate(LocalSettings):
    # K2: an empty workspace ID removes it and returns to the shared host.
    videoWorkspaceId: WorkspaceId | Literal[""] | None = None
    clearApiKey: bool = False
    clearVideoApiKey: bool = False
    clearKlingApiKey: bool = False
    clearFalApiKey: bool = False

    @model_validator(mode="after")
    def check_update(self):
        clears = {"clearApiKey", "clearVideoApiKey", "clearKlingApiKey", "clearFalApiKey"}
        if any(getattr(self, key) is None for key in self.model_fields_set if key not in clears):
            raise ValueError("省略保持原值；移除密钥使用对应的 clear 字段")
        for secret, clear in (
            ("apiKey", "clearApiKey"),
            ("videoApiKey", "clearVideoApiKey"),
            ("klingApiKey", "clearKlingApiKey"),
            ("falApiKey", "clearFalApiKey"),
        ):
            if getattr(self, clear) and getattr(self, secret) is not None:
                raise ValueError("不能同时设置和移除密钥")
        return self


def read_local_settings(home: Path) -> LocalSettings:
    try:
        path = home / "config.yml"
        if not path.exists():
            return LocalSettings()
        if path.stat().st_size > 16384:
            raise ValueError
        values = yaml.safe_load(path.read_text(encoding="utf-8"))
        return LocalSettings.model_validate({} if values is None else values)
    except (OSError, ValueError, yaml.YAMLError):
        raise AppError("INVALID_CONFIG", "本地 config.yml 无法读取或格式不正确，已保留原文件。") from None


def config_sources(config: Config) -> dict[str, str]:
    return {
        "apiKey": config.sources.get("apiKey", "provided" if config.api_key else "unset"),
        "model": config.sources.get("model", "provided"),
        **{
            name: config.sources.get(
                name,
                "unset"
                if getattr(config, attr) is None
                else "default"
                if getattr(config, attr) == default
                else "provided",
            )
            for name, (attr, _, default) in VIDEO_SETTINGS.items()
        },
    }


def update_local_settings(config: Config, update: ConfigUpdate) -> Config:
    sources = config_sources(config)
    values = read_local_settings(config.home).model_dump(exclude_none=True)
    clears = {
        "apiKey": update.clearApiKey,
        "videoApiKey": update.clearVideoApiKey,
        "klingApiKey": update.clearKlingApiKey,
        "falApiKey": update.clearFalApiKey,
    }
    for name, (attr, _, _) in _SETTINGS.items():
        supplied = name in update.model_fields_set
        clear = clears.get(name, False)
        if not supplied and not clear:
            continue
        if sources[name] in {"environment", "provided"}:
            if name in clears or getattr(update, name) != getattr(config, attr):
                raise AppError("CONFIG_OVERRIDE", "该字段由启动配置或环境变量提供，请先移除该配置再修改。")
            continue
        if clear or (name == "videoWorkspaceId" and supplied and update.videoWorkspaceId == ""):
            values.pop(name, None)
        elif supplied:
            values[name] = getattr(update, name)
    try:
        validated = LocalSettings.model_validate(values)
    except ValidationError:
        raise AppError("INVALID_CONFIG", "配置字段不符合要求。") from None
    temporary = config.home / f"config-{uuid4()}.tmp"
    try:
        config.home.mkdir(parents=True, exist_ok=True)
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            os.chmod(temporary, 0o600)
            yaml.safe_dump(validated.model_dump(exclude_none=True), handle, allow_unicode=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, config.home / "config.yml")
    except OSError:
        raise AppError("CONFIG_WRITE", "配置未保存，请检查本地数据目录权限。") from None
    finally:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
    effective, following_sources = {}, {}
    for name, (attr, _, default) in _SETTINGS.items():
        if sources[name] in {"environment", "provided"}:
            effective[attr], following_sources[name] = getattr(config, attr), sources[name]
        else:
            local_value = getattr(validated, name)
            effective[attr] = local_value if local_value is not None else default
            following_sources[name] = (
                "local" if local_value is not None else "unset" if default is None else "default"
            )
    return replace(config, **effective, sources=following_sources)


def _environment_value(value, local_value, default):
    """Environment strings for integer settings are parsed once, before validation."""
    if value:
        if isinstance(default, int):
            try:
                return int(value)
            except ValueError:
                raise AppError("VIDEO_CONFIG_INVALID", "视频配置中的数量上限必须是整数。") from None
        return value
    return local_value if local_value is not None else default


def load_config(env: Mapping[str, str] | None = None) -> Config:
    env = os.environ if env is None else env
    home = Path(env.get("VAGENT_HOME") or Path.home() / ".vagent").expanduser().resolve()
    local = read_local_settings(home)
    environment_key = env.get("VAGENT_DEEPSEEK_KEY") or env.get("DEEPSEEK_API_KEY")
    environment_model = env.get("VAGENT_DEEPSEEK_MODEL")
    try:
        budget = int(env.get("VAGENT_CONTEXT_BYTES") or "65536")
    except ValueError:
        raise AppError("INVALID_CONTEXT_BUDGET", "上下文预算必须是至少 1024 字节的整数。") from None
    if budget < 1024:
        raise AppError("INVALID_CONTEXT_BUDGET", "上下文预算必须是至少 1024 字节的整数。")
    redis_url = env.get("VAGENT_REDIS_URL") or None
    try:
        ttl = int(env.get("VAGENT_CACHE_TTL") or "3600")
        if not 1 <= ttl <= 604800:
            raise ValueError
        if redis_url:
            parsed = urlsplit(redis_url)
            if parsed.scheme not in {"redis", "rediss"} or not parsed.hostname:
                raise ValueError
            _ = parsed.port
    except ValueError:
        raise AppError(
            "INVALID_CACHE_CONFIG", "Redis URL 必须使用 redis/rediss；缓存 TTL 必须为 1～604800 秒。"
        ) from None
    return Config(
        home=home,
        api_key=environment_key or local.apiKey,
        model=environment_model or local.model or "deepseek-flash",
        context_bytes=budget,
        redis_url=redis_url,
        cache_ttl=ttl,
        skills_root=Path(env["VAGENT_SKILLS_DIR"]).absolute() if env.get("VAGENT_SKILLS_DIR") else None,
        mcp_config=Path(env["VAGENT_MCP_CONFIG"]).resolve() if env.get("VAGENT_MCP_CONFIG") else None,
        mcp_local=env.get("VAGENT_MCP_LOCAL", "0") == "1",
        **{
            attr: _environment_value(env.get(variable), getattr(local, name), default)
            for name, (attr, variable, default) in VIDEO_SETTINGS.items()
        },
        sources={
            "apiKey": "environment" if environment_key else "local" if local.apiKey else "unset",
            "model": "environment" if environment_model else "local" if local.model else "default",
            **{
                name: "environment"
                if env.get(variable)
                else "local"
                if getattr(local, name) is not None
                else "unset"
                if default is None
                else "default"
                for name, (_, variable, default) in VIDEO_SETTINGS.items()
            },
        },
    )


def require_key(key: str | None) -> str:
    if not key or not key.strip():
        raise AppError("MISSING_KEY", "请在页面设置或 DEEPSEEK_API_KEY 中配置密钥；无 Key 可运行 demo。")
    return key.strip()


def assert_id(value: str) -> None:
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", value) or value in {"__proto__", "constructor", "prototype"}:
        raise AppError("INVALID_ID", "ID 只能包含 1～64 位字母、数字、下划线或连字符，且不能使用保留名称。")
