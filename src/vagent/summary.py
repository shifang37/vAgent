"""Context v3 history summarization: offline by default, deduplicated across resume.

A context-v3 Run may replace the turns it would otherwise drop with one compact
history summary. The summary cache is a sidecar file (``<home>/summaries.json``),
never part of ``state.json``: context v3 adds no schema and no frozen contract
model hash. The default summarizer is extractive and deterministic and calls no
model; a model-backed summarizer may be injected, and the sidecar makes it run at
most once per summarized span, so an explicit resume never pays for the same
digest twice.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Protocol

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    ToolMessage,
    messages_to_dict,
)

SUMMARY_VERSION = 1
SUMMARY_FILE = "summaries.json"
SUMMARY_PROMPT_VERSION = "history-summary-v1"
MAX_SUMMARY_BYTES = 4096
DEFAULT_TARGET_BYTES = 1024
MAX_ENTRIES = 512
LINE_LIMIT = 160


def summary_target_bytes(budget: int) -> int:
    """The bytes a digest may use for a given context budget (one shared rule)."""
    return min(MAX_SUMMARY_BYTES, max(256, budget // 16))


class Summarizer(Protocol):
    """A synchronous or awaitable ``summarize``; the caller resolves awaitability."""

    name: str
    fingerprint: str

    def summarize(self, messages: list[BaseMessage], *, target_bytes: int) -> str: ...


def clip(text: str, target_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= target_bytes:
        return text
    return encoded[:target_bytes].decode("utf-8", "ignore").rstrip()


def message_text(message: BaseMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    return "".join(block if isinstance(block, str) else str(block.get("text", "")) for block in content)


def one_line(text: str) -> str:
    return " ".join(text.split())[:LINE_LIMIT]


def tool_outcome(message: ToolMessage) -> str:
    if not isinstance(message.content, str):
        return ""
    try:
        payload = json.loads(message.content)
    except (ValueError, TypeError):
        return ""
    if not isinstance(payload, dict) or type(payload.get("ok")) is not bool:
        return ""
    if payload["ok"]:
        return "ok"
    error = payload.get("error")
    code = error.get("code") if isinstance(error, dict) else None
    return f"error {code}" if code else "error"


class ExtractiveSummarizer:
    """Deterministic offline digest that covers the whole dropped span, not just its head.

    Detailed lines (one per message) are used while they fit the target. When they do
    not, a compact one-line-per-turn digest is built first, so every dropped turn is
    represented; only if even that overflows are those lines stride-sampled. Losing a
    little detail everywhere beats silently forgetting the newest drops.
    """

    name = "extractive-v1"
    fingerprint = SUMMARY_PROMPT_VERSION
    INTENT_LIMIT = 60

    def summarize(self, messages: list[BaseMessage], *, target_bytes: int = DEFAULT_TARGET_BYTES) -> str:
        cap = min(target_bytes, MAX_SUMMARY_BYTES)
        detailed = self._detailed(messages)
        if len(detailed.encode("utf-8")) <= cap:
            return detailed
        return self._stride(self._compact(messages), cap)

    def _detailed(self, messages: list[BaseMessage]) -> str:
        lines: list[str] = []
        for message in messages:
            if isinstance(message, HumanMessage):
                text = one_line(message_text(message))
                if text:
                    lines.append(f"- 用户: {text}")
            elif isinstance(message, AIMessage):
                text = one_line(message_text(message))
                if text:
                    lines.append(f"- 助手: {text}")
                for call in message.tool_calls:
                    lines.append(f"- 工具调用: {call['name']}")
            elif isinstance(message, ToolMessage):
                outcome = tool_outcome(message)
                if outcome:
                    lines.append(f"- 工具结果: {message.name or ''} {outcome}")
        return "\n".join(lines)

    def _compact(self, messages: list[BaseMessage]) -> list[str]:
        lines: list[str] = []
        intent: str | None = None
        outcomes: list[str] = []
        for message in messages:
            if isinstance(message, HumanMessage):
                if intent is not None:
                    lines.append(self._turn_line(len(lines), intent, outcomes))
                intent = one_line(message_text(message))[: self.INTENT_LIMIT]
                outcomes = []
            elif isinstance(message, ToolMessage):
                outcome = tool_outcome(message)
                if outcome and outcome not in outcomes:
                    outcomes.append(outcome)
        if intent is not None:
            lines.append(self._turn_line(len(lines), intent, outcomes))
        return lines

    @staticmethod
    def _turn_line(index: int, intent: str, outcomes: list[str]) -> str:
        suffix = f" [{'、'.join(outcomes)}]" if outcomes else ""
        return f"- 第{index}轮: {intent}{suffix}"

    @staticmethod
    def _stride(lines: list[str], cap: int) -> str:
        for stride in range(1, len(lines) + 1):
            selected = lines[::stride]
            if len("\n".join(selected).encode("utf-8")) <= cap or stride == len(lines):
                return clip("\n".join(selected), cap)
        return ""


def span_key(messages: list[BaseMessage], *, config_fingerprint: str) -> str:
    """A content-addressed key: the same span and summarizer config always collide."""
    payload = {
        "promptVersion": SUMMARY_PROMPT_VERSION,
        "config": config_fingerprint,
        "span": messages_to_dict(messages),
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class SummaryStore:
    """``summaries-v1`` sidecar: a versioned ``key -> summary`` map, corruption tolerant."""

    def __init__(self, home, *, clock=None):
        self.path = Path(home) / SUMMARY_FILE
        if clock is None:
            from vagent.storage import now as clock
        self.clock = clock
        # ``(st_mtime_ns, st_size) -> entries``: resume and repeated steps share one parse.
        self._cache: tuple[tuple[int, int], dict] | None = None

    def _read(self) -> dict:
        try:
            stat = self.path.stat()
        except OSError:
            self._cache = None
            return {}
        stamp = (stat.st_mtime_ns, stat.st_size)
        if self._cache is not None and self._cache[0] == stamp:
            return self._cache[1]
        entries = self._load()
        self._cache = (stamp, entries)
        return entries

    def _load(self) -> dict:
        try:
            raw = self.path.read_bytes()
        except OSError:
            return {}
        try:
            payload = json.loads(raw)
        except ValueError:
            return {}
        if not isinstance(payload, dict) or payload.get("version") != SUMMARY_VERSION:
            return {}
        entries = payload.get("summaries")
        return entries if isinstance(entries, dict) else {}

    def _remember(self, entries: dict) -> None:
        try:
            stat = self.path.stat()
        except OSError:
            self._cache = None
            return
        self._cache = ((stat.st_mtime_ns, stat.st_size), entries)

    def get(self, key: str) -> str | None:
        entry = self._read().get(key)
        if isinstance(entry, dict) and isinstance(entry.get("text"), str) and entry["text"]:
            return entry["text"]
        return None

    def put(self, key: str, text: str, *, model: str, prompt_version: str = SUMMARY_PROMPT_VERSION) -> None:
        """One summary = one atomic replacement; an oversized cache evicts the oldest."""
        entries = dict(self._read())
        entries[key] = {
            "text": text,
            "model": model,
            "promptVersion": prompt_version,
            "bytes": len(text.encode("utf-8")),
            "createdAt": self.clock(),
        }

        def by_age(item: str) -> tuple[str, str]:
            return (str(entries[item].get("createdAt") or ""), item)

        if len(entries) > MAX_ENTRIES:
            for stale in sorted(entries, key=by_age)[: len(entries) - MAX_ENTRIES]:
                entries.pop(stale, None)
        ordered = {item: entries[item] for item in sorted(entries, key=by_age)}
        payload = json.dumps({"version": SUMMARY_VERSION, "summaries": ordered}, ensure_ascii=False)
        temporary = self.path.with_name(self.path.name + ".tmp")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, self.path)
        self._remember(ordered)
