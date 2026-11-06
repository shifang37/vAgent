"""Offline growth measurement for ``state.json`` (FX-20).

Synthesizes 10/20/40 terminal Runs in a throwaway home directory and reports the file
size plus the median cost of one empty transaction (which rewrites the whole file). The
numbers are a reproducible before/after for the event cap, the no-write recovery pass
and ``vagent gc``; nothing here calls a model or a provider.

Usage:
    python scripts/measure_state.py
    python scripts/measure_state.py --runs 10,20,40 --repeats 5
"""

from __future__ import annotations

import argparse
import json
import statistics
import tempfile
import time
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, messages_to_dict

from vagent.storage import FileStore, RunRecord, StoredMessage

BODY = "分镜与镜头说明：" + "暖色晨光下的咖啡店，镜头缓慢推进，人物自然交谈。" * 8


def _message(kind: str, index: int) -> StoredMessage:
    text = f"[{index}] {BODY}"
    message = HumanMessage(content=text) if kind == "human" else AIMessage(content=text)
    return StoredMessage.model_validate(messages_to_dict([message])[0])


def _synthetic_run(index: int, createdAt: str) -> dict:
    session_id = f"project-{index:02d}"
    messages = [_message("human" if step % 2 == 0 else "ai", step) for step in range(24)]
    events = [
        {"type": "tool.completed", "name": "project_read", "callId": f"call-{event}", "at": createdAt}
        for event in range(60)
    ]
    record = RunRecord(
        id=f"run_{index:04d}",
        session_id=session_id,
        request_id=f"request-{index:04d}",
        prompt="写一个 15 秒的咖啡店广告分镜。",
        model="deepseek-flash",
        status="completed",
        messages=messages,
        model_steps=3,
        tool_calls=2,
        input_tokens=12000,
        output_tokens=800,
        answer="已完成分镜并保存产物。",
        created_at=createdAt,
        updated_at=createdAt,
        events=events,
    )
    return record.model_dump(by_alias=True)


def measure(counts: list[int], *, repeats: int = 5) -> list[dict]:
    rows = []
    with tempfile.TemporaryDirectory(prefix="vagent-measure-") as directory:
        home = Path(directory)
        with FileStore.open(home) as store:
            for count in counts:

                def add(draft: dict, count: int = count) -> None:
                    while len(draft["runs"]) < count:
                        index = len(draft["runs"])
                        run = _synthetic_run(index, "2026-01-01T00:00:00+00:00")
                        draft["runs"][run["id"]] = run
                        session = draft["sessions"].setdefault(
                            run["sessionId"], {"id": run["sessionId"], "messages": []}
                        )
                        session["messages"] = run["messages"]
                        draft["projects"].setdefault(
                            run["sessionId"],
                            {
                                "id": run["sessionId"],
                                "revision": 0,
                                "goal": "",
                                "audience": "",
                                "style": "",
                                "constraints": [],
                                "plan": [],
                            },
                        )

                store.transaction(add)
                sizes = [(home / "state.json").stat().st_size]
                timings = []
                for _ in range(repeats):
                    started = time.perf_counter()
                    store.transaction(lambda draft: None)
                    timings.append((time.perf_counter() - started) * 1000)
                rows.append(
                    {
                        "runs": count,
                        "bytes": sizes[0],
                        "emptyTransactionMs": round(statistics.median(timings), 2),
                    }
                )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", default="10,20,40", help="逗号分隔的 Run 数量")
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    counts = [int(item) for item in args.runs.split(",") if item.strip()]
    rows = measure(counts, repeats=max(1, args.repeats))
    print("| runs | state.json bytes | empty transaction ms |")
    print("|---:|---:|---:|")
    for row in rows:
        print(f"| {row['runs']} | {row['bytes']} | {row['emptyTransactionMs']} |")
    print(json.dumps(rows, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
