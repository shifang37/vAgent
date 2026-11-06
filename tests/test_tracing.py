"""H5 tracing: offline collector, parent/child linkage, redaction, default-off, resume."""

import json
from pathlib import Path

import pytest
from conftest import ScriptedModel, tool_call
from langchain_core.messages import AIMessage

from vagent.config import load_config
from vagent.errors import AppError
from vagent.runner import AgentRunner
from vagent.tools import create_project_tools
from vagent.tracing import (
    InMemoryExporter,
    JsonlExporter,
    Span,
    Tracer,
    redact_attributes,
    redact_value,
)

GOAL = "面向年轻人的咖啡店，提升品牌认知和到店意愿"


def runner(store, callback, tracing=None, **kwargs):
    return AgentRunner(
        store=store,
        model=ScriptedModel(callback),
        tools=create_project_tools(),
        tracing=tracing,
        **kwargs,
    )


def tracer_for(exporter, **kwargs):
    return Tracer(exporter, **kwargs)


def two_step(*_):
    """One tool call, then a final answer."""

    def callback(messages, step):
        if step == 0:
            return tool_call("project_update", {"expectedRevision": 0, "goal": GOAL})
        return AIMessage(
            content="好。", usage_metadata={"input_tokens": 7, "output_tokens": 3, "total_tokens": 10}
        )

    return callback


# --- default off ------------------------------------------------------------------


async def test_tracing_off_writes_nothing_and_keeps_events_identical(store):
    factory = Tracer(JsonlExporter(store.home / "traces"))
    assert factory.sample_percent == 100
    events_off = []
    off = runner(store, two_step(), on_event=events_off.append)
    result = await off.run("coffee", "做个咖啡店方案", request_id="trace-off")
    assert result["status"] == "completed"
    assert not (store.home / "traces").exists()

    # The same script with tracing on yields the same product events and signature:
    # tracing observes, it never participates.
    exporter = InMemoryExporter()
    events_on = []
    on = runner(store, two_step(), tracing=tracer_for(exporter), on_event=events_on.append)
    assert on.context_signature() == off.context_signature()
    await on.run("coffee", "做个咖啡店方案", request_id="trace-on")
    assert exporter.spans
    assert [event["type"] for event in events_off] == [event["type"] for event in events_on]


# --- span tree --------------------------------------------------------------------


async def test_spans_share_one_trace_and_nest_under_the_run(store):
    exporter = InMemoryExporter()
    traced = runner(store, two_step(), tracing=tracer_for(exporter))
    result = await traced.run("coffee", "做个咖啡店方案", request_id="trace-tree")
    assert result["status"] == "completed"

    trace_id = Tracer.trace_id_for(result["id"])
    assert {item["traceId"] for item in exporter.spans} == {trace_id}
    runs = exporter.by_name("run", trace_id)
    assert len(runs) == 1 and runs[0]["parentSpanId"] is None and runs[0]["status"] == "ok"
    for name in ("context.build", "model.call", "tool.call"):
        assert exporter.by_name(name, trace_id), name
    root = runs[0]["spanId"]
    # Every non-root span is attached to the run, directly or through a nested span.
    parents = {item["spanId"]: item["parentSpanId"] for item in exporter.spans}
    for item in exporter.spans:
        if item["name"] == "run":
            continue
        ancestor, depth = item["parentSpanId"], 0
        while ancestor is not None and depth < 8:
            if ancestor == root:
                break
            ancestor, depth = parents.get(ancestor), depth + 1
        assert ancestor == root, item["name"]
    tool_span = exporter.by_name("tool.call", trace_id)[0]
    assert tool_span["attributes"]["tool"] == "project_update"
    assert tool_span["attributes"]["ok"] is True
    assert tool_span["status"] == "ok"


async def test_a_real_run_writes_a_jsonl_sidecar(store):
    exporter = JsonlExporter(store.home / "traces")
    traced = runner(store, two_step(), tracing=Tracer(exporter))
    result = await traced.run("coffee", "做个咖啡店方案", request_id="trace-jsonl")
    assert result["status"] == "completed"

    trace_id = Tracer.trace_id_for(result["id"])
    spans = exporter.spans(trace_id)
    assert spans and all(item["traceId"] == trace_id for item in spans)
    assert [item["name"] for item in spans].count("run") == 1
    assert spans[0]["startTimeUnixNano"] and spans[0]["endTimeUnixNano"]
    # Duration is monotonic: a sub-millisecond context build must not read as zero.
    assert all(item["durationNano"] > 0 for item in spans if item["name"] == "context.build")
    first_line = exporter.path(trace_id).read_text(encoding="utf-8").splitlines()[0]
    assert json.loads(first_line)["traceId"] == trace_id


async def test_model_span_usage_matches_the_usage_events(store):
    exporter = InMemoryExporter()
    events = []
    traced = runner(store, two_step(), tracing=tracer_for(exporter))
    traced.on_event = events.append
    await traced.run("coffee", "做个咖啡店方案", request_id="trace-usage")

    usage_events = [event["call"] for event in events if event["type"] == "model.usage"]
    calls = exporter.by_name("model.call")
    assert len(calls) == len(usage_events) == 2
    assert [call["attributes"]["inputTokens"] for call in calls] == [
        usage["inputTokens"] for usage in usage_events
    ]
    assert [call["attributes"]["outputTokens"] for call in calls] == [
        usage["outputTokens"] for usage in usage_events
    ]
    # The tool-calling step reports no usage; the final answer reports 7/3 tokens.
    assert calls[-1]["attributes"]["inputTokens"] == 7
    assert calls[-1]["attributes"]["outputTokens"] == 3
    assert sum(call["attributes"]["outputTokens"] or 0 for call in calls) == 3


async def test_failed_run_records_an_error_span(store):
    def explode(*_):
        raise RuntimeError("provider down")

    exporter = InMemoryExporter()
    traced = runner(store, explode, tracing=tracer_for(exporter, content=True))
    secret_prompt = "做个咖啡店方案 Authorization: Bearer sk-must-not-leak-0123456789"
    result = await traced.run("coffee", secret_prompt, request_id="trace-fail")
    assert result["status"] == "failed"
    run = exporter.by_name("run")[0]
    assert run["status"] == "error" and run["attributes"]["runStatus"] == "failed"
    dumped = json.dumps(exporter.spans, ensure_ascii=False)
    assert "sk-must-not-leak" not in dumped
    assert run["attributes"]["prompt"].startswith("做个咖啡店方案")


# --- redaction --------------------------------------------------------------------


def test_secret_shaped_values_and_keys_are_redacted():
    attributes = {
        "prompt": "Authorization: Bearer sk-live-abcdefgh12345678",
        "api_key": "plain-value-dropped-by-key",
        "nested": {"token": "x", "note": "sk-short-but-toolong-0000111122223333"},
        "hash": "0123456789abcdef0123456789abcdef",
        "count": 3,
        "flag": True,
    }
    redacted = redact_attributes(attributes)
    text = json.dumps(redacted, ensure_ascii=False)
    assert "sk-live-abcdefgh12345678" not in text
    assert "plain-value-dropped-by-key" not in text
    assert "0123456789abcdef0123456789abcdef" not in text
    assert redacted["api_key"] == "[redacted]"
    assert redacted["nested"]["token"] == "[redacted]"
    assert redacted["count"] == 3 and redacted["flag"] is True
    # A long prose value is truncated (a long unbroken alphanumeric run is treated as a
    # blob and redacted outright, which is why the fixture uses spaces).
    truncated = redact_value("长文案 " * 300)
    assert truncated.endswith("<1200 chars>") and len(truncated) < 600


async def test_content_capture_is_opt_in(store):
    exporter = InMemoryExporter()
    traced = runner(store, two_step(), tracing=tracer_for(exporter))
    await traced.run("coffee", "做个咖啡店方案", request_id="trace-structure-only")
    run = exporter.by_name("run")[0]
    assert "prompt" not in run["attributes"]
    assert all("prompt" not in call["attributes"] for call in exporter.by_name("model.call"))

    exporter = InMemoryExporter()
    traced = runner(store, two_step(), tracing=tracer_for(exporter, content=True))
    await traced.run("coffee", "做个咖啡店方案", request_id="trace-with-content")
    run = exporter.by_name("run")[0]
    assert run["attributes"]["prompt"] == "做个咖啡店方案"
    assert exporter.by_name("tool.call")[0]["attributes"]["arguments"]["goal"] == GOAL


# --- exporter ---------------------------------------------------------------------


def test_jsonl_exporter_writes_one_file_per_trace(tmp_path):
    exporter = JsonlExporter(tmp_path / "traces")
    trace = Tracer(exporter).for_run("run-1")
    with trace.span("run", attributes={"runId": "run-1"}) as root:
        with trace.span("model.call", attributes={"step": 1}):
            pass
    path = exporter.path(trace.trace_id)
    assert path.name == f"{trace.trace_id}.jsonl"
    spans = exporter.spans(trace.trace_id)
    assert [item["name"] for item in spans] == ["model.call", "run"]
    assert spans[0]["parentSpanId"] == root.span_id
    assert exporter.previous_root(trace.trace_id) == root.span_id


def test_corrupt_trace_file_degrades_to_no_previous_root(tmp_path):
    exporter = JsonlExporter(tmp_path / "traces")
    exporter.directory.mkdir(parents=True)
    exporter.path("deadbeef").write_text("{ not json\n", encoding="utf-8")
    assert exporter.previous_root("deadbeef") is None
    assert exporter.spans("deadbeef") == []


def test_sampling_is_deterministic_and_can_disable_tracing():
    exporter = InMemoryExporter()
    assert Tracer(exporter, sample_percent=0).for_run("run-1") is None
    assert Tracer(exporter, sample_percent=100).for_run("run-1") is not None
    flaky = Tracer(exporter, sample_percent=50)
    trace_id = Tracer.trace_id_for("run-1")
    assert all((flaky.for_run("run-1") is not None) == flaky.sampled(trace_id) for _ in range(3))
    with pytest.raises(ValueError):
        Tracer(exporter, sample_percent=101)


async def test_an_export_failure_never_breaks_the_run(store):
    class Broken:
        def export(self, span):
            raise OSError("disk full")

        def previous_root(self, trace_id):
            return None

    traced = runner(store, two_step(), tracing=Tracer(Broken()))
    result = await traced.run("coffee", "做个咖啡店方案", request_id="trace-broken")
    assert result["status"] == "completed"


# --- resume continuity ------------------------------------------------------------


async def test_resume_continues_the_same_trace(store):
    exporter = InMemoryExporter()
    tracer = tracer_for(exporter)

    def read_then_fail(messages, step):
        if step == 0:
            return tool_call("project_update", {"expectedRevision": 0, "audience": "夜班护士"})
        raise RuntimeError("provider down")

    first = await runner(store, read_then_fail, tracing=tracer).run("coffee", "做个方案")
    assert first["status"] == "failed" and first["resumable"]

    def finish(*_):
        return AIMessage(content="完成")

    final = await runner(store, finish, tracing=tracer).resume(first["id"])
    assert final["status"] == "completed"

    trace_id = Tracer.trace_id_for(first["id"])
    assert {item["traceId"] for item in exporter.spans} == {trace_id}
    runs = exporter.by_name("run", trace_id)
    assert len(runs) == 2
    assert runs[0]["attributes"]["resume"] is False
    assert runs[1]["attributes"]["resume"] is True
    # The resumed execution hangs off the previous root: one connected trace, not two.
    assert runs[1]["parentSpanId"] == runs[0]["spanId"]
    assert runs[0]["attributes"]["runStatus"] == "failed"
    assert runs[1]["attributes"]["runStatus"] == "completed"


# --- config -----------------------------------------------------------------------


def test_config_tracing_defaults_off_and_parses_the_switch():
    base = load_config({"VAGENT_HOME": "tmp/h5-knob"})
    assert (base.tracing_enabled, base.trace_sample, base.trace_content) == (False, 100, False)
    switched = load_config(
        {
            "VAGENT_HOME": "tmp/h5-knob",
            "VAGENT_TRACING": "on",
            "VAGENT_TRACE_SAMPLE": "25",
            "VAGENT_TRACE_CONTENT": "on",
        }
    )
    assert (switched.tracing_enabled, switched.trace_sample, switched.trace_content) == (
        True,
        25,
        True,
    )


def test_config_rejects_an_invalid_sample_percent():
    for value in ("-1", "101", "half"):
        with pytest.raises(AppError) as error:
            load_config({"VAGENT_HOME": "tmp/h5-knob", "VAGENT_TRACE_SAMPLE": value})
        assert error.value.code == "INVALID_TRACE_SAMPLE"


async def test_application_wires_tracing_only_when_enabled(tmp_path):
    from dataclasses import replace

    from vagent.application import ApplicationService
    from vagent.config import Config

    config = Config(home=tmp_path, api_key=None)
    async with ApplicationService.open(config, model=ScriptedModel(two_step())) as service:
        assert service.runner(model=ScriptedModel(two_step())).tracing is None

    enabled = replace(config, tracing_enabled=True, trace_sample=25, trace_content=True)
    async with ApplicationService.open(enabled, model=ScriptedModel(two_step())) as service:
        tracing = service.runner(model=ScriptedModel(two_step())).tracing
        assert isinstance(tracing, Tracer)
        assert (tracing.sample_percent, tracing.content) == (25, True)
        assert tracing.exporter.directory == tmp_path / "traces"


# --- module discipline ------------------------------------------------------------


def test_tracing_module_stays_offline_and_dependency_free():
    text = (Path(__file__).resolve().parents[1] / "src" / "vagent" / "tracing.py").read_text(encoding="utf-8")
    for forbidden in ("httpx", "requests", "socket", "opentelemetry", "random"):
        assert forbidden not in text


def test_span_set_treats_status_and_error_code_as_fields():
    trace = Tracer(InMemoryExporter()).for_run("run-1")
    record = trace.record_span("probe", start_ns=10, end_ns=20, status="error", error_code="BOOM")
    assert record["status"] == "error" and record["errorCode"] == "BOOM"
    with trace.span("probe") as item:
        item.set(status="error", errorCode="BOOM", note="kept")
    assert isinstance(item, Span)
    assert item.attributes["note"] == "kept"
