import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

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


def load_config(env: Mapping[str, str] | None = None) -> Config:
    env = os.environ if env is None else env
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
        home=Path(env.get("VAGENT_HOME") or Path.home() / ".vagent").expanduser().resolve(),
        api_key=env.get("VAGENT_DEEPSEEK_KEY") or env.get("DEEPSEEK_API_KEY"),
        model=env.get("VAGENT_DEEPSEEK_MODEL") or "deepseek-flash",
        context_bytes=budget,
        redis_url=redis_url,
        cache_ttl=ttl,
        skills_root=Path(env["VAGENT_SKILLS_DIR"]).absolute() if env.get("VAGENT_SKILLS_DIR") else None,
    )


def require_key(key: str | None) -> str:
    if not key or not key.strip():
        raise AppError("MISSING_KEY", "请配置 DEEPSEEK_API_KEY；无 Key 可运行 demo 查看模拟工具流程。")
    return key.strip()


def assert_id(value: str) -> None:
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", value) or value in {"__proto__", "constructor", "prototype"}:
        raise AppError("INVALID_ID", "ID 只能包含 1～64 位字母、数字、下划线或连字符，且不能使用保留名称。")
