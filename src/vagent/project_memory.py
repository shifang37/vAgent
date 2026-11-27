"""The project memory ledger: bounded cross-session facts, decisions, progress and todos.

Design: ``docs/PROJECT_MEMORY_DESIGN.md``. The ledger is a separate top-level collection
(``memories``) rather than a field of ``Project``: project facts are replaced whole by
``project_update`` while memory entries are patched one by one under their own revision,
and only the ledger is rendered as its own system-prompt block.

Limits are enforced here rather than by the argument schema so that every limit violation
returns the single documented ``MEMORY_LIMIT`` code instead of a generic schema error. The
storage model re-asserts the same bounds as a hard invariant: a ledger that bypassed this
module still cannot be committed.
"""

from vagent.context import MEMORY_KIND_ORDER, compact_json
from vagent.errors import AppError
from vagent.storage import now

# D8: bounded by construction; over-limit writes are refused, never silently truncated.
MEMORY_MAX_ENTRIES = 64
MEMORY_MAX_ENTRY_CHARS = 400
MEMORY_MAX_BYTES = 8 * 1024

# D5: progress is code-owned. A model that could write it could claim a save that never
# happened, so both writing and removing a progress line are refused with this code.
CODE_OWNED_KIND = "progress"


def _require_project(state: dict, project_id: str) -> None:
    if project_id not in state["projects"]:
        raise AppError("PROJECT_NOT_FOUND", "没有这个项目。")


def _ordered(entries: list[dict]) -> list[dict]:
    """Deterministic order shared by every read and by the rendered block."""
    return sorted(
        entries,
        key=lambda entry: (MEMORY_KIND_ORDER.get(entry["kind"], len(MEMORY_KIND_ORDER)), entry["key"]),
    )


def ledger_bytes(entries: list[dict]) -> int:
    """The ledger's owned size: ``compact_json(entries)`` exactly as rendered."""
    return len(compact_json(_ordered(entries)).encode("utf-8"))


def memory_limits() -> dict:
    return {
        "maxEntries": MEMORY_MAX_ENTRIES,
        "maxEntryChars": MEMORY_MAX_ENTRY_CHARS,
        "maxBytes": MEMORY_MAX_BYTES,
    }


def read_memory(state: dict, project_id: str, kind: str | None = None) -> dict:
    """Read one project's ledger; a project with no ledger reads as an empty one."""
    _require_project(state, project_id)
    memory = state["memories"].get(project_id)
    entries = [] if memory is None else [dict(entry) for entry in memory["entries"]]
    # ``bytes`` always reports the whole ledger: a filtered view must not look smaller
    # than the ledger that a following write still has to fit into.
    size = ledger_bytes(entries)
    if kind is not None:
        entries = [entry for entry in entries if entry["kind"] == kind]
    return {
        "revision": 0 if memory is None else memory["revision"],
        "entries": _ordered(entries),
        "bytes": size,
        "limits": memory_limits(),
    }


def update_memory(
    state: dict,
    project_id: str,
    *,
    expected_revision: int,
    upsert: list[dict],
    remove: list[str],
) -> dict:
    """Patch the ledger under an optimistic lock; never exceed the documented bounds."""
    _require_project(state, project_id)
    memory = state["memories"].get(project_id)
    revision = 0 if memory is None else memory["revision"]
    if expected_revision != revision:
        raise AppError("MEMORY_REVISION_CONFLICT", "项目记忆版本已变化，请重新读取后再修改。")

    current = {} if memory is None else {entry["key"]: dict(entry) for entry in memory["entries"]}
    if any(entry["kind"] == CODE_OWNED_KIND for entry in upsert):
        raise AppError("MEMORY_KIND_FORBIDDEN", "进度条目由系统维护，不能由模型写入。")
    for key in remove:
        existing = current.get(key)
        if existing is not None and existing["kind"] == CODE_OWNED_KIND:
            raise AppError("MEMORY_KIND_FORBIDDEN", "进度条目由系统维护，不能由模型删除。")
        current.pop(key, None)
    for entry in upsert:
        text = entry["text"]
        if len(text) > MEMORY_MAX_ENTRY_CHARS:
            raise AppError(
                "MEMORY_LIMIT",
                f"单条记忆不能超过 {MEMORY_MAX_ENTRY_CHARS} 字符；当前 {len(text)} 字符，未写入。",
            )
        current[entry["key"]] = {"key": entry["key"], "kind": entry["kind"], "text": text}

    entries = _ordered(list(current.values()))
    size = ledger_bytes(entries)
    if len(entries) > MEMORY_MAX_ENTRIES or size > MEMORY_MAX_BYTES:
        raise AppError(
            "MEMORY_LIMIT",
            f"项目记忆最多 {MEMORY_MAX_ENTRIES} 条、{MEMORY_MAX_BYTES} 字节；"
            f"本次会得到 {len(entries)} 条、{size} 字节，未写入。请先删除过期条目。",
        )
    before = [] if memory is None else _ordered([dict(entry) for entry in memory["entries"]])
    if entries == before:
        # A write that changes nothing (including removing a key that never existed) is
        # not a new ledger version, and an absent ledger stays absent.
        return {"revision": revision, "entries": entries, "bytes": size}

    updated = {
        "id": project_id,
        "revision": revision + 1,
        "entries": entries,
        "updatedAt": now(),
    }
    state["memories"][project_id] = updated
    return {"revision": updated["revision"], "entries": entries, "bytes": size}
