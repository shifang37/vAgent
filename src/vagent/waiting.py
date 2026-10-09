"""Provider-neutral waiting contracts. Runtime coordination is introduced in B3."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, JsonValue, model_validator

from vagent.contracts import Contract, Fingerprint, Identifier, Name, UtcTimestamp
from vagent.errors import AppError

WAIT_EXECUTION_VERSION = 2  # Reserved for the new graph; the existing graph stays at v1.


class ToolExecutionContext(Contract):
    project_id: Identifier
    session_id: Identifier
    run_id: Identifier
    model_step: int = Field(ge=1)
    tool_call_id: str = Field(min_length=1, max_length=200, pattern=r"^\S+$")

    @property
    def operation_key(self) -> str:
        return f"{self.run_id}:{self.model_step}:{self.tool_call_id}"


class ExternalResourceRef(Contract):
    kind: Name
    id: Identifier


class ToolResultError(Contract):
    code: str = Field(min_length=1, max_length=64, pattern=r"^[A-Z][A-Z0-9_]*$")
    message: str = Field(min_length=1, max_length=1000)


class _ToolOutcome(Contract):
    @model_validator(mode="before")
    @classmethod
    def boolean_discriminator(cls, values):
        if isinstance(values, dict) and "ok" in values and type(values["ok"]) is not bool:
            raise ValueError("Tool outcome must use a JSON boolean")
        return values


class ToolSuccess(_ToolOutcome):
    ok: Literal[True] = True
    data: JsonValue


class ToolFailure(_ToolOutcome):
    ok: Literal[False] = False
    error: ToolResultError


ToolResult = Annotated[ToolSuccess | ToolFailure, Field(discriminator="ok")]


class ResumeToken(Contract):
    """A wake-up reference, never a caller-provided tool result."""

    wait_id: Identifier
    generation: int = Field(ge=1)


class DeferredToolResult(ResumeToken):
    kind: Literal["deferred"] = "deferred"
    resource: ExternalResourceRef


class WaitBinding(Contract):
    id: Identifier
    context: ToolExecutionContext
    resource: ExternalResourceRef
    # Optional for B0 probe records; production registrations save the original
    # tool name/arguments fingerprint before any final Operation exists.
    operation_fingerprint: Fingerprint | None = None
    revision: int = Field(default=0, ge=0)
    generation: int = Field(default=1, ge=1)
    status: Literal["preparing", "armed", "ready", "claimed", "delivered", "stopped"] = "preparing"
    started_at: UtcTimestamp
    deadline_at: UtcTimestamp
    auto_resume: bool = True
    checkpoint_id: str | None = Field(default=None, min_length=1, max_length=200)
    interrupt_id: str | None = Field(default=None, min_length=1, max_length=200)
    result: ToolResult | None = None
    claimed_model_steps: int | None = Field(default=None, ge=0)
    delivered_checkpoint_id: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def coherent_state(self):
        if datetime.fromisoformat(self.deadline_at) <= datetime.fromisoformat(self.started_at):
            raise ValueError("Wait deadline must follow its start")
        if bool(self.checkpoint_id) != bool(self.interrupt_id):
            raise ValueError("Checkpoint and interrupt identities must be saved together")
        if self.status not in {"preparing", "stopped"} and not self.checkpoint_id:
            raise ValueError("Only checkpointed waits can be armed or delivered")
        if self.status in {"preparing", "armed"} and self.result is not None:
            raise ValueError("A pending wait cannot already contain a final result")
        if self.status in {"ready", "claimed", "delivered"} and self.result is None:
            raise ValueError("A ready wait must contain a durable result")
        if self.status in {"claimed", "delivered"} and self.claimed_model_steps is None:
            raise ValueError("Claiming records the model-attempt boundary")
        if self.status in {"preparing", "armed", "ready"} and self.claimed_model_steps is not None:
            raise ValueError("An unclaimed wait cannot have a claim boundary")
        if (self.status == "delivered") != bool(self.delivered_checkpoint_id):
            raise ValueError("Delivery requires the resulting graph checkpoint")
        if self.status == "stopped" and self.auto_resume:
            raise ValueError("Stopped waits cannot resume automatically")
        return self

    def deferred(self) -> DeferredToolResult:
        return DeferredToolResult(wait_id=self.id, generation=self.generation, resource=self.resource)

    def resume_token(self) -> ResumeToken:
        return ResumeToken(wait_id=self.id, generation=self.generation)

    def confirmed_result(self, token: ResumeToken) -> ToolResult:
        if token != self.resume_token():
            raise AppError("STALE_WAIT", "等待标识或代次已变化，未交付工具结果。")
        if self.status not in {"ready", "claimed", "delivered"} or self.result is None:
            raise AppError("WAIT_NOT_READY", "等待尚无可交付结果，或已停止。")
        return self.result
