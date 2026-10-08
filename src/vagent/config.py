import os
import re
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from vagent.errors import AppError


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


class LocalSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    apiKey: str | None = Field(default=None, pattern=r"^[\x21-\x7e]{1,512}$", repr=False)
    model: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,99}$")


class ConfigUpdate(LocalSettings):
    clearApiKey: bool = False

    @model_validator(mode="after")
    def check_update(self):
        if any(getattr(self, key) is None for key in self.model_fields_set if key != "clearApiKey"):
            raise ValueError("省略保持原值；移除密钥使用 clearApiKey")
        if self.clearApiKey and self.apiKey is not None:
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
    }


def update_local_settings(config: Config, update: ConfigUpdate) -> Config:
    sources = config_sources(config)
    if (update.apiKey is not None or update.clearApiKey) and sources["apiKey"] in {"environment", "provided"}:
        raise AppError("CONFIG_OVERRIDE", "密钥由启动配置或环境变量提供，请先移除该配置再在页面修改。")
    if (
        update.model is not None
        and sources["model"] in {"environment", "provided"}
        and update.model != config.model
    ):
        raise AppError("CONFIG_OVERRIDE", "模型由启动配置或环境变量提供，请先移除该配置再在页面修改。")
    values = read_local_settings(config.home).model_dump(exclude_none=True)
    if update.apiKey is not None:
        values["apiKey"] = update.apiKey
    if update.clearApiKey:
        values.pop("apiKey", None)
    if update.model is not None and sources["model"] not in {"environment", "provided"}:
        values["model"] = update.model
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
    key = config.api_key if sources["apiKey"] in {"environment", "provided"} else validated.apiKey
    model = (
        config.model
        if sources["model"] in {"environment", "provided"}
        else validated.model or "deepseek-flash"
    )
    return replace(
        config,
        api_key=key,
        model=model,
        sources={
            "apiKey": sources["apiKey"]
            if sources["apiKey"] in {"environment", "provided"}
            else "local"
            if key
            else "unset",
            "model": sources["model"]
            if sources["model"] in {"environment", "provided"}
            else "local"
            if validated.model
            else "default",
        },
    )


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
        sources={
            "apiKey": "environment" if environment_key else "local" if local.apiKey else "unset",
            "model": "environment" if environment_model else "local" if local.model else "default",
        },
    )


def require_key(key: str | None) -> str:
    if not key or not key.strip():
        raise AppError("MISSING_KEY", "请在页面设置或 DEEPSEEK_API_KEY 中配置密钥；无 Key 可运行 demo。")
    return key.strip()


def assert_id(value: str) -> None:
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", value) or value in {"__proto__", "constructor", "prototype"}:
        raise AppError("INVALID_ID", "ID 只能包含 1～64 位字母、数字、下划线或连字符，且不能使用保留名称。")
