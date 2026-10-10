import inspect
from datetime import UTC, datetime, timedelta

import pytest
from langchain_core.messages import AIMessage

from vagent.storage import FileStore, RunRecordV1
from vagent.video.jobs import JobService
from vagent.video.providers.mock import MockVideoAdapter
from vagent.waiting import ToolExecutionContext


@pytest.fixture
def store(tmp_path):
    with FileStore.open(tmp_path / "state") as instance:
        yield instance


def tool_call(name, args=None, call_id="call-1"):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args or {}, "id": call_id}])


class ScriptedModel:
    name = "scripted-test"

    def __init__(self, callback):
        self.callback = callback
        self.calls = 0
        self.inputs = []

    async def generate(self, messages, tools):
        step = self.calls
        self.calls += 1
        self.inputs.append(messages)
        result = self.callback(messages, step)
        return await result if inspect.isawaitable(result) else result


class VideoClock:
    def __init__(self):
        self.value = datetime(2030, 1, 1, tzinfo=UTC)

    def __call__(self):
        return self.value

    def advance(self, seconds=2):
        self.value += timedelta(seconds=seconds)

    def due(self, job):
        self.value = max(self.value, datetime.fromisoformat(job.next_poll_at))


def video_run(store, run_id="video-run", *, project_id="coffee", read_only=False):
    store.ensure_session(project_id)
    record = RunRecordV1(
        id=run_id,
        session_id=project_id,
        request_id=run_id,
        prompt="offline video fixture",
        model="no-model",
        status="completed",
        messages=[],
        model_steps=0,
        tool_calls=0,
        input_tokens=0,
        output_tokens=0,
        answer="",
        created_at="2030-01-01T00:00:00+00:00",
        updated_at="2030-01-01T00:00:00+00:00",
        read_only=read_only,
    ).model_dump(by_alias=True)

    def add(draft):
        draft["runs"][run_id] = record
        draft["sessions"][project_id]["latestRunId"] = run_id

    store.transaction(add)
    return ToolExecutionContext(
        project_id=project_id, session_id=project_id, run_id=run_id, model_step=1, tool_call_id="generate"
    )


def video_request(adapter, **changes):
    capabilities = adapter.capabilities()
    return {
        "provider": capabilities.provider,
        "model": capabilities.model,
        "capabilitiesVersion": capabilities.capabilities_version,
        "prompt": "雨夜咖啡店",
        "spec": capabilities.specs[0].model_dump(mode="json", by_alias=True),
        **changes,
    }


@pytest.fixture
def video_clock():
    return VideoClock()


@pytest.fixture
def video_service(store, video_clock):
    video_run(store)
    adapter = MockVideoAdapter(store, clock=video_clock)
    return JobService(store, [adapter], clock=video_clock)


@pytest.fixture
def job_runtime(monkeypatch, video_clock):
    """Application Worker tests advance persisted deadlines, never real poll intervals."""
    from types import SimpleNamespace

    import vagent.application as application
    from vagent.video.providers.mock import MockScenario
    from vagent.video.worker import JobWorker

    runtime = SimpleNamespace(clock=video_clock, scenario=MockScenario(), adapters=[])

    def adapter(store):
        provider = MockVideoAdapter(store, clock=video_clock, scenario=runtime.scenario)
        runtime.adapters.append(provider)
        return provider

    monkeypatch.setattr(application, "MockVideoAdapter", adapter)
    monkeypatch.setattr(
        application,
        "JobService",
        lambda store, adapters, **kwargs: JobService(store, adapters, clock=video_clock, **kwargs),
    )
    monkeypatch.setattr(
        application, "JobWorker", lambda service: JobWorker(service, idle_interval_seconds=0.005)
    )
    return runtime


@pytest.fixture
def media_runtime(monkeypatch, video_clock):
    from media_support import install_media_runtime

    return install_media_runtime(monkeypatch, video_clock)
