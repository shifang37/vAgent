"""H5 tracing: a dependency-free span model over the Run lifecycle.

Tracing is observation, never control flow. Every span is written after the work it
describes has already committed its real effect, export failures are swallowed, and
with tracing off nothing is constructed, nothing is written and no behavior changes.

The model deliberately mirrors the OpenTelemetry field names (``traceId``, ``spanId``,
``parentSpanId``, ``startTimeUnixNano``, ``attributes``, ``status``) so the export can be
posted to an OTLP collector later without reshaping, while the transport here stays a
local JSONL sidecar: one line per finished span, one file per trace.

Trace identity is derived from the Run id, so a Run keeps **one** trace across an
explicit resume: the resumed execution's root span is parented to the previous root,
which keeps the tree connected instead of splitting one task into two traces.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import time
import uuid
from contextvars import ContextVar
from pathlib import Path

TRACE_DIRECTORY = "traces"
MAX_ATTRIBUTE_CHARS = 512
REDACTED = "[redacted]"

# A span must never become a credential exfiltration path: attribute names that look like
# secrets are dropped wholesale, values that look like keys are replaced in place.
SECRET_KEY = re.compile(r"api[-_]?key|authorization|token|secret|password|cookie|credential", re.IGNORECASE)
SECRET_VALUE_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\b(?:bearer|basic)\s+[A-Za-z0-9._\-+/=]{8,}", re.IGNORECASE),
    re.compile(r"\b[A-Fa-f0-9]{32,}\b"),
    re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b"),
)

_CURRENT: ContextVar[Span | None] = ContextVar("vagent_current_span", default=None)


def redact_value(value):
    """Make an attribute value JSON-safe, secret-free and bounded in length."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        for pattern in SECRET_VALUE_PATTERNS:
            value = pattern.sub(REDACTED, value)
        if len(value) > MAX_ATTRIBUTE_CHARS:
            return f"{value[:MAX_ATTRIBUTE_CHARS]}…<{len(value)} chars>"
        return value
    if isinstance(value, dict):
        return {
            str(key): REDACTED if SECRET_KEY.search(str(key)) else redact_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_value(item) for item in value]
    return redact_value(str(value))


def redact_attributes(attributes: dict | None) -> dict:
    # The key check applies at the top level too: ``{"api_key": "..."}`` must not survive
    # just because the nested branch is what usually handles names.
    return {
        str(key): REDACTED if SECRET_KEY.search(str(key)) else redact_value(value)
        for key, value in (attributes or {}).items()
    }


class Span:
    """One unit of work, written on exit; ``set`` merges attributes post-hoc."""

    def __init__(
        self,
        trace: Trace,
        name: str,
        *,
        kind: str,
        parent_span_id: str | None,
        attributes,
        start_ns: int,
        start_mono: int,
    ):
        self._trace = trace
        self.name = name
        self.kind = kind
        self.parent_span_id = parent_span_id
        self.attributes = redact_attributes(attributes)
        self.start_ns = start_ns
        self.start_mono = start_mono
        self.span_id = trace.span_id()
        self.status = "ok"
        self.error_code: str | None = None
        self.ended = False
        self._token = None

    def set(self, **attributes) -> Span:
        """Merge attributes; ``status`` / ``errorCode`` are span fields, not attributes."""
        for key, value in attributes.items():
            if key == "status":
                self.status = str(value)
            elif key == "errorCode":
                self.error_code = None if value is None else str(value)
            else:
                self.attributes[key] = redact_value(value)
        return self

    def __enter__(self) -> Span:
        self._token = _CURRENT.set(self)
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        if self._token is not None:
            _CURRENT.reset(self._token)
            self._token = None
        if exc_type is not None and self.status == "ok":
            self.status = "error"
            self.error_code = self.error_code or getattr(exc_type, "__name__", None)
        self.end()
        return False

    def end(self) -> None:
        if self.ended:
            return
        self.ended = True
        # Duration comes from the monotonic counter: Windows' wall clock can be coarse
        # enough that two samples of a sub-millisecond context build are identical, which
        # would report every fast span as "0 ns".
        end_ns = self._trace.clock()
        elapsed = self._trace.mono() - self.start_mono
        self._trace.export(
            {
                "traceId": self._trace.trace_id,
                "spanId": self.span_id,
                "parentSpanId": self.parent_span_id,
                "name": self.name,
                "kind": self.kind,
                "startTimeUnixNano": self.start_ns,
                "endTimeUnixNano": max(end_ns, self.start_ns),
                "durationNano": max(0, elapsed),
                "status": self.status,
                "errorCode": self.error_code,
                "attributes": self.attributes,
            }
        )


class NullSpan:
    """The tracing-off placeholder, so call sites need no branch on ``None``."""

    status = "ok"
    error_code = None

    def set(self, **_attributes) -> NullSpan:
        return self

    def __enter__(self) -> NullSpan:
        return self

    def __exit__(self, *_exc) -> bool:
        return False


class Trace:
    """One trace: a Run across its first execution and any explicit resume."""

    def __init__(
        self,
        exporter,
        trace_id: str,
        *,
        content: bool = False,
        clock=time.time_ns,
        mono=time.perf_counter_ns,
        id_factory=None,
        root_parent: str | None = None,
    ):
        self.exporter = exporter
        self.trace_id = trace_id
        self.content = content
        self.clock = clock
        self.mono = mono
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex[:16])
        self.root_parent = root_parent
        self.root_span_id: str | None = None

    def span_id(self) -> str:
        return self._id_factory()

    def _parent_for(self, name: str) -> str | None:
        """Explicit nesting when a context is available, root linkage otherwise."""
        current = _CURRENT.get()
        if isinstance(current, Span):
            return current.span_id
        if name == "run":
            return self.root_parent
        # A node may run in a task whose context predates the root span; attach it to
        # the root anyway so the trace stays one connected tree, never a forest.
        return self.root_span_id or self.root_parent

    def span(
        self,
        name: str,
        *,
        kind: str = "internal",
        attributes: dict | None = None,
        parent_span_id: str | None = None,
    ) -> Span:
        if parent_span_id is None:
            parent_span_id = self._parent_for(name)
        created = Span(
            self,
            name,
            kind=kind,
            parent_span_id=parent_span_id,
            attributes=attributes,
            start_ns=self.clock(),
            start_mono=self.mono(),
        )
        if name == "run" and self.root_span_id is None:
            self.root_span_id = created.span_id
        return created

    def record_span(
        self,
        name: str,
        *,
        start_ns: int,
        end_ns: int,
        kind: str = "internal",
        attributes: dict | None = None,
        parent_span_id: str | None = None,
        status: str = "ok",
        error_code: str | None = None,
    ) -> dict:
        """Write an already-finished span, e.g. a wait measured across a process gap."""
        if parent_span_id is None:
            parent_span_id = self._parent_for(name)
        span = {
            "traceId": self.trace_id,
            "spanId": self.span_id(),
            "parentSpanId": parent_span_id,
            "name": name,
            "kind": kind,
            "startTimeUnixNano": int(start_ns),
            "endTimeUnixNano": int(end_ns),
            "durationNano": max(0, int(end_ns) - int(start_ns)),
            "status": status,
            "errorCode": error_code,
            "attributes": redact_attributes(attributes),
        }
        self.export(span)
        return span

    def export(self, span: dict) -> None:
        # Observation must never fail the work it observes.
        with contextlib.suppress(Exception):
            self.exporter.export(span)


def span(tracer: Trace | None, name: str, *, kind: str = "internal", attributes=None, parent_span_id=None):
    """``with span(trace, "tool.call", attributes=...) as current:`` — a no-op when off."""
    if tracer is None:
        return NullSpan()
    return tracer.span(name, kind=kind, attributes=attributes, parent_span_id=parent_span_id)


class JsonlExporter:
    """Append-only JSONL sidecar: ``<home>/traces/<traceId>.jsonl``, one span per line."""

    def __init__(self, directory):
        self.directory = Path(directory)

    def path(self, trace_id: str) -> Path:
        return self.directory / f"{trace_id}.jsonl"

    def export(self, span: dict) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        line = json.dumps(span, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self.path(span["traceId"]).open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")

    def previous_root(self, trace_id: str) -> str | None:
        """The last ``run`` span already on disk, so a resume continues the same tree."""
        try:
            lines = self.path(trace_id).read_text(encoding="utf-8").splitlines()
        except OSError:
            return None
        root = None
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and record.get("name") == "run":
                root = record.get("spanId")
        return root

    def spans(self, trace_id: str) -> list[dict]:
        try:
            lines = self.path(trace_id).read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        spans = []
        for line in lines:
            try:
                spans.append(json.loads(line))
            except ValueError:
                continue
        return spans


class InMemoryExporter:
    """The offline collector used by tests and by callers that grade spans themselves."""

    def __init__(self):
        self.spans: list[dict] = []

    def export(self, span: dict) -> None:
        self.spans.append(span)

    def previous_root(self, trace_id: str) -> str | None:
        roots = [item for item in self.spans if item["traceId"] == trace_id and item["name"] == "run"]
        return roots[-1]["spanId"] if roots else None

    def names(self, trace_id: str | None = None) -> list[str]:
        return [item["name"] for item in self.spans if trace_id is None or item["traceId"] == trace_id]

    def by_name(self, name: str, trace_id: str | None = None) -> list[dict]:
        return [
            item
            for item in self.spans
            if item["name"] == name and (trace_id is None or item["traceId"] == trace_id)
        ]


class Tracer:
    """The per-process factory: decides sampling, then binds one trace to a Run."""

    def __init__(
        self,
        exporter,
        *,
        sample_percent: int = 100,
        content: bool = False,
        clock=time.time_ns,
        mono=time.perf_counter_ns,
        id_factory=None,
    ):
        if type(sample_percent) is not int or not 0 <= sample_percent <= 100:
            raise ValueError("trace sample percent must be an integer in 0..100")
        self.exporter = exporter
        self.sample_percent = sample_percent
        self.content = content
        self.clock = clock
        self.mono = mono
        self._id_factory = id_factory

    @staticmethod
    def trace_id_for(run_id: str) -> str:
        return hashlib.sha256(f"run:{run_id}".encode()).hexdigest()[:32]

    def sampled(self, trace_id: str) -> bool:
        """Deterministic per-trace sampling: one Run is traced wholly or not at all."""
        if self.sample_percent >= 100:
            return True
        if self.sample_percent <= 0:
            return False
        return int(trace_id[:8], 16) % 100 < self.sample_percent

    def for_run(self, run_id: str) -> Trace | None:
        trace_id = self.trace_id_for(run_id)
        if not self.sampled(trace_id):
            return None
        parent = None
        with contextlib.suppress(Exception):
            parent = self.exporter.previous_root(trace_id)
        return Trace(
            self.exporter,
            trace_id,
            content=self.content,
            clock=self.clock,
            mono=self.mono,
            id_factory=self._id_factory,
            root_parent=parent,
        )
