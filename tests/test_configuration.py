import asyncio
import json
from contextlib import asynccontextmanager

import httpx
import pytest
from conftest import ScriptedModel
from langchain_core.messages import AIMessage

from vagent.config import ConfigUpdate, load_config, update_local_settings
from vagent.errors import AppError
from vagent.web import create_app


@asynccontextmanager
async def configured_app(tmp_path, *, model=None, env=None, handler=None):
    config = load_config({"VAGENT_HOME": str(tmp_path / "settings"), **(env or {})})
    app = create_app(config, model=model)
    async with app.router.lifespan_context(app):
        async with (
            httpx.AsyncClient(transport=httpx.MockTransport(handler or validation_response)) as provider,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:3210"
            ) as client,
        ):
            app.state.service.http_client = provider
            client.headers["X-CSRF-Token"] = (await client.get("/api/session-token")).json()["token"]
            yield client, app.state.service


def validation_response(request):
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": "OK"}}],
            "usage": {"prompt_tokens": 6, "completion_tokens": 1},
        },
    )


def test_local_config_persists_and_environment_has_precedence(tmp_path):
    env = {"VAGENT_HOME": str(tmp_path / "new-home")}
    config = load_config(env)
    assert config.sources == {"apiKey": "unset", "model": "default", "videoMode": "default"}
    updated = update_local_settings(config, ConfigUpdate(apiKey="local-test-secret", model="deepseek-test"))
    reloaded = load_config(env)
    assert reloaded == updated
    assert reloaded.api_key == "local-test-secret"
    assert reloaded.sources == {"apiKey": "local", "model": "local", "videoMode": "default"}
    assert "local-test-secret" not in repr(reloaded)
    overridden = load_config({**env, "DEEPSEEK_API_KEY": "env-key", "VAGENT_DEEPSEEK_MODEL": "env-model"})
    path = config.home / "config.yml"
    original = path.read_bytes()
    for change in [
        ConfigUpdate(apiKey="replacement"),
        ConfigUpdate(clearApiKey=True),
        ConfigUpdate(model="x"),
    ]:
        with pytest.raises(AppError) as caught:
            update_local_settings(overridden, change)
        assert caught.value.code == "CONFIG_OVERRIDE"
        assert path.read_bytes() == original
    assert overridden.api_key == "env-key" and overridden.model == "env-model"
    cleared = update_local_settings(updated, ConfigUpdate(clearApiKey=True))
    assert cleared.api_key is None and cleared.model == "deepseek-test"
    assert load_config(env) == cleared


def test_atomic_config_failure_keeps_prior_file_and_effective_config(tmp_path, monkeypatch):
    config = update_local_settings(
        load_config({"VAGENT_HOME": str(tmp_path)}), ConfigUpdate(apiKey="original-key")
    )
    original = (tmp_path / "config.yml").read_bytes()

    def fail_replace(*_):
        raise OSError("untrusted-exception-key")

    monkeypatch.setattr("vagent.config.os.replace", fail_replace)
    with pytest.raises(AppError) as caught:
        update_local_settings(config, ConfigUpdate(apiKey="new-key"))
    assert caught.value.code == "CONFIG_WRITE"
    assert "untrusted-exception-key" not in str(caught.value)
    assert (tmp_path / "config.yml").read_bytes() == original
    assert not list(tmp_path.glob("config-*.tmp"))
    assert config.api_key == "original-key"


@pytest.mark.parametrize("body", ["apiKey: [secret]", "[secret]", "false", "unknown: secret", "x" * 16385])
def test_corrupt_config_preserved_without_echoing_its_contents(tmp_path, body):
    path = tmp_path / "config.yml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(AppError) as caught:
        load_config({"VAGENT_HOME": str(tmp_path)})
    assert caught.value.code == "INVALID_CONFIG" and "secret" not in str(caught.value)
    assert path.read_text(encoding="utf-8") == body


async def test_wizard_save_validate_change_clear_and_restart(tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        return validation_response(request)

    async with configured_app(tmp_path, handler=handler) as (client, service):
        before = (await client.get("/api/config")).json()
        assert before["editable"] == {"apiKey": True, "model": True, "videoMode": False}
        secret = "wizard-test-secret"
        saved = await client.patch("/api/config", json={"apiKey": secret, "model": "deepseek-test"})
        assert saved.status_code == 200 and secret not in saved.text
        assert not requests  # Saving never incurs a model call.
        assert service.runner().model.name == "deepseek-test"
        verified = await client.post("/api/config/validate", json={})
        assert verified.json()["validation"]["status"] == "verified"
        assert len(requests) == 1
        request = requests[0]
        assert str(request.url) == "https://api.deepseek.com/chat/completions"
        assert request.headers["authorization"] == f"Bearer {secret}"
        body = json.loads(request.content)
        assert body["model"] == "deepseek-test" and body["max_tokens"] == 8
        assert body["thinking"] == {"type": "disabled"} and body["stream"] is False
        same = await client.patch("/api/config", json={"apiKey": secret, "model": "deepseek-test"})
        assert same.json()["validation"] == verified.json()["validation"]
        changed = await client.patch("/api/config", json={"model": "deepseek-flash"})
        assert changed.json()["validation"]["status"] == "unverified"
        for route in ["/api/config", "/api/health", "/api/sessions", "/api/artifacts"]:
            response = await client.get(route)
            assert secret not in response.text
        assert secret not in json.dumps(service.store.snapshot())
        assert "apiKey" not in json.dumps(service.store.snapshot())
    async with configured_app(tmp_path) as (client, service):
        assert service.config.api_key == secret and service.config.model == "deepseek-flash"
        assert (await client.get("/api/config")).json()["validation"]["status"] == "unverified"
        cleared = await client.patch("/api/config", json={"clearApiKey": True})
        assert cleared.json()["apiKeyConfigured"] is False
        missing = await client.post("/api/config/validate", json={})
        assert missing.json()["error"]["code"] == "MISSING_KEY"


@pytest.mark.parametrize(
    "payload",
    [
        {"apiKey": ""},
        {"apiKey": "secret\r\nvalue"},
        {"apiKey": None},
        {"apiKey": "test-secret", "clearApiKey": True},
        {"model": "invalid model"},
        {"unknown": "test-secret"},
        {"clearApiKey": "true"},
    ],
)
async def test_config_rejects_invalid_requests_without_echoing_inputs(tmp_path, payload):
    async with configured_app(tmp_path) as (client, service):
        rejected = await client.patch("/api/config", json=payload)
        assert rejected.status_code == 422 and "secret" not in rejected.text
        assert service.config.api_key is None
        assert not (service.config.home / "config.yml").exists()


async def test_config_endpoints_require_csrf_and_cannot_override_environment(tmp_path):
    async with configured_app(tmp_path, env={"DEEPSEEK_API_KEY": "env-secret"}) as (client, _):
        for method, route in [("PATCH", "/api/config"), ("POST", "/api/config/validate")]:
            response = await client.request(method, route, json={}, headers={"X-CSRF-Token": "wrong"})
            assert response.status_code == 403
        config = (await client.get("/api/config")).json()
        assert config["editable"]["apiKey"] is False
        rejected = await client.patch("/api/config", json={"apiKey": "new-secret"})
        assert rejected.status_code == 409 and rejected.json()["error"]["code"] == "CONFIG_OVERRIDE"
        assert "secret" not in rejected.text


@pytest.mark.parametrize(
    "status,code", [(401, "AUTH_ERROR"), (403, "AUTH_ERROR"), (429, "RATE_LIMIT"), (500, "EXECUTION_ERROR")]
)
async def test_config_validation_failure_is_sanitized_and_never_retried(tmp_path, status, code):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, json={"error": {"message": "secret-provider-detail"}})

    async with configured_app(tmp_path, handler=handler) as (client, service):
        await client.patch("/api/config", json={"apiKey": "test-secret"})
        failed = await client.post("/api/config/validate", json={})
        assert failed.json()["error"]["code"] == code
        assert "secret" not in failed.text
        assert len(requests) == 1 and not service.config_busy
        assert (await client.get("/api/config")).json()["validation"]["status"] == "failed"


@pytest.mark.parametrize("content", [None, " ", 12, {}])
async def test_config_validation_requires_nonempty_text(tmp_path, content):
    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    async with configured_app(tmp_path, handler=handler, env={"DEEPSEEK_API_KEY": "test"}) as (client, _):
        result = await client.post("/api/config/validate", json={})
        assert result.json()["error"]["code"] == "INVALID_RESPONSE"


async def test_validation_does_not_echo_untrusted_usage_values(tmp_path):
    def handler(request):
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "secret"}}],
                "usage": {"prompt_tokens": "secret", "completion_tokens": True},
            },
        )

    async with configured_app(tmp_path, handler=handler, env={"DEEPSEEK_API_KEY": "test"}) as (client, _):
        result = await client.post("/api/config/validate", json={})
        assert "secret" not in result.text
        assert result.json()["validation"]["inputTokens"] is None
        assert result.json()["validation"]["outputTokens"] is None


async def test_config_and_active_runs_are_mutually_exclusive(tmp_path):
    model_entered, provider_entered, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def respond(*_):
        model_entered.set()
        await asyncio.Event().wait()
        return AIMessage(content="unused")

    async def handler(request):
        provider_entered.set()
        await release.wait()
        return validation_response(request)

    async with configured_app(tmp_path, model=ScriptedModel(respond), handler=handler) as (client, service):
        await client.patch("/api/config", json={"apiKey": "test"})
        run = await service.start("test", "hold", "request")
        await model_entered.wait()
        for method, route in [("PATCH", "/api/config"), ("POST", "/api/config/validate")]:
            result = await client.request(method, route, json={})
            assert result.status_code == 409 and result.json()["error"]["code"] == "CONFIG_BUSY"
        service.stop(run["id"])
        await service.task
        task = asyncio.create_task(client.post("/api/config/validate", json={}))
        await provider_entered.wait()
        try:
            for operation in [service.start("test", "new", "new"), service.resume(run["id"])]:
                with pytest.raises(AppError) as caught:
                    await operation
                assert caught.value.code == "CONFIG_BUSY"
            assert (await client.patch("/api/config", json={})).status_code == 409
        finally:
            release.set()
            await task
        assert not service.config_busy
