"""Durable execution accounting independent of replayable graph nodes."""

import time

from langchain_core.messages import messages_to_dict

from vagent.storage import FileStore, ModelCall, now


class RunJournal:
    def __init__(self, store: FileStore, run_id: str, *, active=True):
        self.store, self.run_id = store, run_id
        self.started = time.monotonic() if active else None
        self.previous_seconds = self.record.get("activeSeconds", 0)

    @property
    def record(self) -> dict:
        return self.store.snapshot()["runs"][self.run_id]

    @property
    def elapsed(self) -> float:
        return self.previous_seconds + (time.monotonic() - self.started if self.started is not None else 0)

    def pause(self) -> float:
        """Freeze active time before yielding the graph to an external resource."""
        self.previous_seconds = self.elapsed
        self.started = None
        return self.previous_seconds

    def restart(self) -> None:
        self.previous_seconds = self.record.get("activeSeconds", 0)
        self.started = time.monotonic()

    def update(self, **fields) -> dict:
        def save(draft):
            run = draft["runs"][self.run_id]
            run.update(fields, activeSeconds=self.elapsed, updatedAt=now())
            return run

        return self.store.transaction(save)

    def start_model_call(self, step: int) -> None:
        def save(draft):
            run = draft["runs"][self.run_id]
            if run.get("usageStartStep") is None:
                run["usageStartStep"] = step
            calls = run.setdefault("modelCalls", [])
            calls.append(ModelCall(step=step, status="started", started_at=now()).model_dump(by_alias=True))
            run.update(activeSeconds=self.elapsed, updatedAt=now())

        self.store.transaction(save)

    def finish_model_call(
        self,
        step: int,
        *,
        status: str,
        duration: float,
        usage: dict | None = None,
        error_code: str | None = None,
    ) -> dict | None:
        def save(draft):
            run = draft["runs"][self.run_id]
            call = next((c for c in run.get("modelCalls", []) if c["step"] == step), None)
            if call is None or call["status"] != "started":
                return call
            call.update(
                status=status,
                finishedAt=now(),
                durationSeconds=duration,
                errorCode=error_code,
                **(usage or {}),
            )
            # Commit the per-call record and totals together, once. These counters
            # are independent of graph checkpoints, including rejected responses.
            run["inputTokens"] += call.get("inputTokens") or 0
            run["outputTokens"] += call.get("outputTokens") or 0
            run.update(activeSeconds=self.elapsed, inFlightSeconds=0, updatedAt=now())
            return call

        return self.store.transaction(save)

    def stats(self, state: dict) -> dict:
        record = self.record
        return {
            **state,
            **{
                field: record[key]
                for field, key in (
                    ("model_steps", "modelSteps"),
                    ("tool_calls", "toolCalls"),
                    ("input_tokens", "inputTokens"),
                    ("output_tokens", "outputTokens"),
                    ("context_bytes", "contextBytes"),
                    ("dropped_messages", "droppedMessages"),
                )
            },
        }

    def publish(self, state: dict, *, final: bool = False, resumable: bool = False) -> None:
        def save(draft):
            run = draft["runs"][self.run_id]
            run.update(
                messages=messages_to_dict(state["messages"]),
                activeSeconds=self.elapsed,
                inFlightSeconds=0,
                updatedAt=now(),
            )
            if final:
                run.update(
                    status=state["status"],
                    answer=state["answer"],
                    errorCode=state["error_code"],
                    resumable=resumable,
                )
                if state["status"] == "completed":
                    draft["sessions"][run["sessionId"]]["messages"] = run["messages"]

        self.store.transaction(save)
