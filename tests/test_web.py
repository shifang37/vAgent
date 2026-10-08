import asyncio
import json
from contextlib import asynccontextmanager

import httpx
import pytest
from conftest import ScriptedModel, tool_call
from langchain_core.messages import AIMessage

from vagent.application import ApplicationService
from vagent.config import Config
from vagent.errors import AppError
from vagent.models import DemoModel
from vagent.runner import RunPolicy
from vagent.web import create_app


@asynccontextmanager
async def client_for(tmp_path, model=None, policy=None):
    app = create_app(Config(home=tmp_path / "web", api_key=None), model=model, policy=policy)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:3210"
        ) as client:
            token = (await client.get("/api/session-token")).json()["token"]
            client.headers.update({"X-CSRF-Token": token})
            yield client, app.state.service


async def test_web_real_runner_artifacts_events_duplicate_and_restart(tmp_path):
    async with client_for(tmp_path, DemoModel()) as (client, service):
        health = (await client.get("/api/health")).json()
        assert health["modelKind"] == "injected-test"
        assert {s["name"] for s in health["skills"]} == {"video-brief", "shot-description"}
        session_id = (await client.post("/api/sessions", json={})).json()["id"]
        body = {"prompt": "测试创作方案", "clientRequestId": "web-request-1"}
        response = await client.post(f"/api/sessions/{session_id}/messages", json=body)
        assert response.status_code == 202, response.text
        run_id = response.json()["id"]
        await service.task
        data = (await client.get(f"/api/sessions/{session_id}")).json()
        assert data["run"]["status"] == "completed"
        assert len(data["artifacts"]) == 1
        assert len(data["messages"]) == 2
        assert len(data["run"]["toolTrace"]) == 3
        assert all(call["result"] for call in data["run"]["toolTrace"])
        events = data["run"]["events"]
        assert [e["sequence"] for e in events] == list(range(1, len(events) + 1))
        assert {e["type"] for e in events} >= {
            "run.started",
            "tool.completed",
            "context.prepared",
            "model.usage",
        }
        context = next(e for e in events if e["type"] == "context.prepared")
        assert context["inputBytes"] <= context["budgetBytes"] and context["skillCount"] == 2
        artifact_id = data["artifacts"][0]["id"]
        assert (await client.get(f"/api/artifacts/{artifact_id}?version=1")).json()["content"]
        assert (await client.get(f"/api/artifacts/{artifact_id}?version=42")).status_code == 404
        repeated = await client.post(f"/api/sessions/{session_id}/messages", json=body)
        assert repeated.json()["id"] == run_id
        conflict = await client.post(
            f"/api/sessions/{session_id}/messages", json={**body, "prompt": "不同内容"}
        )
        assert conflict.status_code == 409
        assert len(service.store.snapshot()["artifacts"]) == 1
    async with client_for(tmp_path, DemoModel()) as (client, _):
        data = (await client.get(f"/api/sessions/{session_id}")).json()
        assert data["run"]["id"] == run_id and data["run"]["events"] == events
        assert data["artifacts"][0]["id"] == artifact_id


async def test_web_boundary_validation_missing_key_and_static_allowlist(tmp_path):
    async with client_for(tmp_path) as (client, service):
        assert (await client.get("/")).status_code == 200
        for path in ["/.env", "/state.json", "/README.md", "/api/unknown"]:
            assert (await client.get(path)).status_code == 404
        for headers in [
            {"Host": "attacker.example"},
            {"Origin": "https://attacker.example"},
            {"Sec-Fetch-Site": "cross-site"},
            {"Origin": "http://localhost:3210"},
        ]:
            assert (await client.get("/api/session-token", headers=headers)).status_code == 403
        assert (
            await client.post("/api/sessions", json={}, headers={"X-CSRF-Token": "wrong"})
        ).status_code == 403
        assert (await client.post("/api/sessions", content="{}")).status_code == 415
        large = await client.post("/api/sessions/test/messages", json={"prompt": "a" * 140000})
        assert large.status_code == 413
        invalid = await client.post("/api/sessions/test/messages", json={"prompt": "sensitive-test-value"})
        assert invalid.status_code == 422 and "sensitive-test-value" not in invalid.text
        missing = await client.post(
            "/api/sessions/test/messages", json={"prompt": "test", "clientRequestId": "test"}
        )
        assert missing.json()["error"]["code"] == "MISSING_KEY"
        assert not service.store.snapshot()["runs"]


async def test_web_stop_resume_and_busy_share_original_budget(tmp_path):
    entered = asyncio.Event()
    hold = True

    async def respond(*_):
        entered.set()
        if hold:
            await asyncio.Event().wait()
        return AIMessage(content="恢复完成")

    model = ScriptedModel(respond)
    async with client_for(tmp_path, model, RunPolicy(max_steps=3, timeout_seconds=30)) as (client, service):
        body = {"prompt": "等待", "clientRequestId": "wait"}
        first = await client.post("/api/sessions/test/messages", json=body)
        run_id = first.json()["id"]
        await entered.wait()
        duplicate = await client.post("/api/sessions/test/messages", json=body)
        assert duplicate.json()["id"] == run_id
        busy = await client.post("/api/sessions/other/messages", json={**body, "clientRequestId": "other"})
        assert busy.status_code == 409
        assert (await client.post(f"/api/runs/{run_id}/stop", json={})).status_code == 200
        await service.task
        stopped = (await client.get(f"/api/runs/{run_id}")).json()
        assert stopped["status"] == "cancelled" and stopped["resumable"]
        hold = False
        assert (await client.post(f"/api/runs/{run_id}/resume", json={})).status_code == 202
        await service.task
        final = (await client.get(f"/api/runs/{run_id}")).json()
        assert final["status"] == "completed" and final["modelSteps"] == 2
        assert final["policy"]["maxSteps"] == 3
        assert final["usage"]["recordedCallCount"] == 2


async def test_read_only_web_refuses_model_write_attempt(tmp_path):
    model = ScriptedModel(
        lambda _, step: (
            tool_call("artifact_save", {"kind": "brief", "title": "不得保存", "content": "no"})
            if step == 0
            else AIMessage(content="无法写入")
        )
    )
    async with client_for(tmp_path, model) as (client, service):
        response = await client.post(
            "/api/sessions/readonly/messages",
            json={"prompt": "只读", "clientRequestId": "ro", "readOnly": True},
        )
        await service.task
        run = (await client.get(f"/api/runs/{response.json()['id']}")).json()
        assert run["readOnly"] and not service.store.snapshot()["artifacts"]
        assert any(e.get("errorCode") == "UNKNOWN_TOOL" for e in run["events"])


async def test_service_memory_persists_between_runs_and_projects_are_isolated(tmp_path):
    def respond(messages, step):
        if step == 0:
            return tool_call(
                "project_update", {"expectedRevision": 0, "audience": "城市上班族", "style": "暖色"}
            )
        if step in {1, 2}:
            assert "城市上班族" in messages[0].content
            return AIMessage(content="已记住")
        assert "城市上班族" not in messages[0].content
        return AIMessage(content="另一个项目")

    config = Config(home=tmp_path / "memory", api_key=None)
    async with ApplicationService.open(config, model=ScriptedModel(respond)) as service:
        await service.start("coffee", "记住受众", "first")
        await service.task
        await service.start("coffee", "沿用受众", "second")
        await service.task
        assert service.session("coffee")["project"]["audience"] == "城市上班族"
        await service.start("other", "你好", "third")
        await service.task
        assert service.session("other")["run"]["status"] == "completed"
        serialized = json.dumps(service.capabilities())
        assert "api_key" not in serialized


async def test_mcp_startup_failure_does_not_leave_store_locked(tmp_path):
    config_file = tmp_path / "mcp.json"
    config_file.write_text(
        '{"servers": [{"name":"bad","command":"missing-vagent-executable","read_only_tools":["read"]}]}'
    )
    config = Config(home=tmp_path / "broken", api_key=None, mcp_config=config_file)
    with pytest.raises((AppError, ExceptionGroup)):
        async with ApplicationService.open(config):
            pass
    assert not (config.home / "instance.lock").exists()


async def test_web_exposes_saved_length_check_and_rejects_oversize_revision(tmp_path):
    artifact_id = None

    def respond(messages, step):
        nonlocal artifact_id
        if step == 0:
            return tool_call("artifact_save", {"kind": "brief", "title": "版本测试", "content": "雨夜咖啡"})
        if step == 1:
            artifact_id = json.loads(messages[-1].content)["data"]["artifactId"]
            return AIMessage(content="已保存")
        if step == 2:
            return tool_call(
                "artifact_save",
                {
                    "artifactId": artifact_id,
                    "expectedVersion": 1,
                    "kind": "brief",
                    "title": "版本测试",
                    "content": "雨夜咖啡馆",
                },
            )
        return AIMessage(content="已修改完成")

    async with client_for(tmp_path, ScriptedModel(respond)) as (client, service):
        await client.post(
            "/api/sessions/quality/messages",
            json={
                "prompt": "保存brief，正文不超过4字",
                "clientRequestId": "quality-save",
            },
        )
        await service.task
        data = (await client.get("/api/sessions/quality")).json()
        assert data["project"]["contentLimits"] == {"brief": 4}
        assert data["artifacts"][0]["versions"][0]["contentCheck"]["characters"] == 4
        response = await client.post(
            "/api/sessions/quality/messages",
            json={
                "prompt": "继续修改方案",
                "clientRequestId": "quality-revise",
            },
        )
        await service.task
        failed = (await client.get(f"/api/runs/{response.json()['id']}")).json()
        assert failed["status"] == "failed" and failed["errorCode"] == "QUALITY_UNRESOLVED"
        assert any(event.get("errorCode") == "CONTENT_LENGTH" for event in failed["events"])
        artifact = (await client.get(f"/api/artifacts/{artifact_id}")).json()
        assert len(artifact["versions"]) == 1
        assert artifact["versions"][0]["content"] == "雨夜咖啡"
