"""M2 D3 usage aggregation: counts, Decimal estimates, honest coverage, no writes."""

import httpx
from conftest import ScriptedModel
from langchain_core.messages import AIMessage
from shot_support import generation_args, m2_run, mock_tools, png_bytes, save_storyboard, shot
from wan_support import live_service, provider_response

from vagent.config import Config
from vagent.runner import AgentRunner
from vagent.tools import create_project_tools
from vagent.video.assets import AssetService
from vagent.video.jobs import JobService
from vagent.video.providers.mock import MockVideoAdapter
from vagent.video.usage import summarize_records, summarize_runs
from vagent.waiting import ToolExecutionContext


def context(run_id, call_id="call"):
    return ToolExecutionContext(
        project_id="coffee",
        session_id="coffee",
        run_id=run_id,
        model_step=1,
        tool_call_id=call_id,
    )


def reply(**usage):
    metadata = {"input_tokens": 100, "output_tokens": 5, "total_tokens": 105}
    if usage:
        metadata = usage
    return AIMessage(content="完成", usage_metadata=metadata)


def test_an_empty_scope_never_claims_complete_usage(store):
    usage = summarize_records(store.snapshot())
    assert usage["projectId"] is None and usage["jobs"]["total"] == 0
    assert usage["cost"]["estimateAmount"] == "0.00"
    assert usage["runs"] == summarize_runs([])
    assert usage["runs"]["tokenUsageComplete"] is False
    assert usage["runs"]["cacheUsageComplete"] is False
    assert usage["runs"]["cacheHitTokens"] is None and usage["runs"]["cacheHitRate"] is None
    assert usage["daily"] == []


def test_mock_registrations_report_counts_without_cost(store, video_clock):
    registry, service, _, _ = mock_tools(store, clock=video_clock)
    saved = save_storyboard(registry, store, [shot("镜头一"), shot("镜头二")])
    m2_run(store, "shot-run", mode="mock")
    assert service.shot_generate(generation_args(saved), context=context("shot-run"))["ok"]
    usage = summarize_records(store.snapshot(), project_id="coffee")
    assert usage["projectId"] == "coffee"
    assert usage["jobs"]["total"] == 2 and usage["jobs"]["mock"] == 2 and usage["jobs"]["live"] == 0
    assert usage["jobs"]["open"] == 2 and usage["jobs"]["byStatus"] == {"pending_submit": 2}
    assert usage["jobs"]["byMode"] == {"mock": 2}
    # A simulated Job has no quote and never counts as a reported bill.
    assert usage["cost"]["estimateAmount"] == "0.00" and usage["cost"]["byPriceVersion"] == {}
    assert usage["cost"]["actual"] == {
        "status": "unknown",
        "reportedAmount": "0.00",
        "reportedJobs": 0,
        "unknownJobs": 0,
    }
    assert usage["storyboards"] == {"total": 1, "shotVersions": 2}
    assert usage["assets"] == {"total": 0, "libraryTotal": 0}
    assert usage["daily"][-1]["jobs"] == 2 and usage["daily"][-1]["estimated"] == "0.00"


async def test_live_estimates_are_summed_per_price_version_and_bills_stay_unknown(store, video_clock):
    def handler(request):
        return httpx.Response(200, json=provider_response("PENDING", task_id="usage-task"))

    async with live_service(store, video_clock, handler) as (service, _):
        registry, _, _, _ = mock_tools(store, clock=video_clock)
        saved = save_storyboard(registry, store, [shot("镜头一"), shot("镜头二", durationSeconds=3)])
        m2_run(store, "shot-run", mode="live")
        assert service.shot_generate(generation_args(saved), context=context("shot-run"))["ok"]
        before = store.snapshot()
        usage = summarize_records(before, project_id="coffee")
        assert store.snapshot() == before  # Aggregation is read-only and calls nothing.
    assert usage["jobs"] == {
        "total": 2,
        "open": 2,
        "live": 2,
        "mock": 0,
        "byStatus": {"pending_submit": 2},
        "byMode": {"live": 2},
    }
    # 5s + 3s at 0.60 CNY per output second.
    assert usage["cost"]["estimateAmount"] == "4.80"
    assert usage["cost"]["byPriceVersion"] == {"wan27-beijing-720p-20261010": 2}
    assert usage["cost"]["actual"] == {
        "status": "unknown",
        "reportedAmount": "0.00",
        "reportedJobs": 0,
        "unknownJobs": 2,
    }
    assert usage["daily"] == [{"date": "2030-01-01", "jobs": 2, "live": 2, "estimated": "4.80"}]


def test_assets_and_projects_are_aggregated_separately(store, video_clock):
    registry, service, _, _ = mock_tools(store, clock=video_clock)
    store.ensure_session("other")
    library = AssetService(store)
    library.import_bytes(png_bytes(1280, 720), project_id="other")
    saved = save_storyboard(registry, store, [shot("镜头一")])
    m2_run(store, "shot-run", mode="mock")
    assert service.shot_generate(generation_args(saved), context=context("shot-run"))["ok"]
    coffee = summarize_records(store.snapshot(), project_id="coffee")
    other = summarize_records(store.snapshot(), project_id="other")
    everything = summarize_records(store.snapshot())
    assert coffee["jobs"]["total"] == 1 and other["jobs"]["total"] == 0
    assert coffee["assets"] == {"total": 0, "libraryTotal": 1}
    assert other["assets"] == {"total": 1, "libraryTotal": 1}
    assert everything["projectId"] is None and everything["jobs"]["total"] == 1
    assert everything["assets"] == {"total": 1, "libraryTotal": 1}
    assert everything["storyboards"]["total"] == 1


async def test_token_coverage_never_pretends_unknown_usage_is_zero(store):
    model = ScriptedModel(
        lambda *_: AIMessage(
            content="完成",
            usage_metadata={
                "input_tokens": 100,
                "output_tokens": 5,
                "total_tokens": 105,
                "input_token_details": {"cache_read": 60},
            },
        )
    )
    first = await AgentRunner(store=store, model=model, tools=create_project_tools()).run(
        "coffee", "统计用量"
    )
    usage = summarize_records(store.snapshot(), project_id="coffee")
    assert usage["runs"]["runs"] == 1 and usage["runs"]["recordedCallCount"] == 1
    assert usage["runs"]["callsWithTokenUsage"] == 1 and usage["runs"]["tokenUsageComplete"]
    assert usage["runs"]["observedInputTokens"] == 100 and usage["runs"]["observedOutputTokens"] == 5
    assert usage["runs"]["cacheHitTokens"] == 60 and usage["runs"]["cacheMissTokens"] == 40
    assert usage["runs"]["cacheHitRate"] == 0.6
    blind = ScriptedModel(lambda *_: AIMessage(content="没有用量"))
    await AgentRunner(store=store, model=blind, tools=create_project_tools()).run("coffee", "再统计一次")
    assert len(first["modelCalls"]) == 1 and first["modelCalls"][0]["status"] == "responded"
    usage = summarize_records(store.snapshot(), project_id="coffee")
    assert usage["runs"]["runs"] == 2 and usage["runs"]["callsWithTokenUsage"] == 1
    assert not usage["runs"]["tokenUsageComplete"] and not usage["runs"]["cacheUsageComplete"]
    assert usage["runs"]["observedInputTokens"] == 100
    # Another project's runs never leak into this scope.
    assert summarize_records(store.snapshot(), project_id="other")["runs"]["runs"] == 0


def test_client_registrations_are_counted_once_with_their_own_run(store, video_clock):
    from vagent.video import clients

    store.ensure_session("coffee")
    adapter = MockVideoAdapter(store, clock=video_clock)
    service = JobService(
        store, [adapter], clock=video_clock, config=Config(home=store.home, api_key=None, video_mode="mock")
    )
    registry, _, _, _ = mock_tools(store, clock=video_clock)
    saved = save_storyboard(registry, store, [shot("镜头一")])
    clients.generate_shots(
        service,
        session_id="coffee",
        client_request_id="usage-client",
        storyboard_id=saved["storyboardId"],
        storyboard_version=saved["version"],
        shot_ids=saved["shotIds"],
    )
    usage = summarize_records(store.snapshot(), project_id="coffee")
    assert usage["jobs"]["total"] == 1 and usage["storyboards"]["shotVersions"] == 1
    assert usage["runs"]["modelSteps"] == 0 and usage["runs"]["recordedCallCount"] == 0
