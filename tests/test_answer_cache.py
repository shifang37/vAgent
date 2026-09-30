import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys

import pytest
from conftest import ScriptedModel, tool_call
from langchain_core.messages import AIMessage, HumanMessage

from vagent.cache import AnswerCache
from vagent.config import load_config
from vagent.errors import AppError
from vagent.runner import AgentRunner
from vagent.storage import FileStore
from vagent.tools import create_project_tools
from vagent.usage import summarize_usage


class MemoryRedis:
    def __init__(self):
        self.values = {}
        self.expiry = {}

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, *, ex):
        self.values[key] = value
        self.expiry[key] = ex


def make_runner(store, cache, callback=None, **kwargs):
    model = ScriptedModel(
        callback
        or (
            lambda messages, step: AIMessage(
                content="只读回答",
                usage_metadata={"input_tokens": 20, "output_tokens": 3, "total_tokens": 23},
            )
        )
    )
    model.cache_config = {"adapter": "fixture-v1", "temperature": 0}
    return AgentRunner(
        store=store, model=model, tools=create_project_tools(), answer_cache=cache, read_only=True, **kwargs
    )


def reset_history(store, session="test"):
    store.transaction(lambda state: state["sessions"][session].update(messages=[]))


async def test_hit_skips_model_and_usage_survives_reopen(store):
    backend = MemoryRedis()
    runner = make_runner(store, AnswerCache(backend, ttl=12))
    first = await runner.run("test", "解释镜头")
    assert first["answerCache"] == {"miss": 1, "stored": 1}
    reset_history(store)
    second = await runner.run("test", "解释镜头")
    assert second["answer"] == first["answer"]
    assert runner.model.calls == 1
    assert second["modelSteps"] == 0 and second["modelCalls"] == []
    assert second["inputTokens"] == second["outputTokens"] == 0
    assert summarize_usage(second)["modelCallCount"] == 0
    assert second["answerCache"] == {"hit": 1}
    assert set(backend.expiry.values()) == {12}
    assert await runner.resume(second["id"]) == second
    home = store.home
    store.close()
    with FileStore.open(home) as reopened:
        assert reopened.snapshot()["runs"][second["id"]]["answerCache"] == {"hit": 1}


async def test_changed_history_project_artifacts_and_model_miss(store):
    runner = make_runner(store, AnswerCache(MemoryRedis()))
    await runner.run("test", "解释镜头")
    assert (await runner.run("test", "解释镜头"))["answerCache"].get("miss") == 1
    reset_history(store)
    store.transaction(lambda state: state["projects"]["test"].update(style="rain", revision=1))
    assert (await runner.run("test", "解释镜头"))["answerCache"].get("miss") == 1
    reset_history(store)
    create_project_tools().execute(
        "artifact_save",
        {"kind": "brief", "title": "new", "content": "text"},
        store=store,
        project_id="test",
        operation_key="save",
    )
    assert (await runner.run("test", "解释镜头"))["answerCache"].get("miss") == 1
    reset_history(store)
    runner.model.cache_config["temperature"] = 1
    assert (await runner.run("test", "解释镜头"))["answerCache"].get("miss") == 1


async def test_key_covers_all_dependencies():
    args = dict(
        scope="home",
        signature="rules-skills-v2",
        model_config={"model": "one"},
        messages=[HumanMessage(content="question")],
        tools=[],
        project={"id": "p"},
        artifacts=[],
    )
    key = AnswerCache.key(**args)
    assert "question" not in key and "home" not in key
    for field, value in dict(
        scope="other",
        signature="changed",
        model_config={"model": "two"},
        messages=[HumanMessage(content="new")],
        tools=[{"tool": "new"}],
        project={"id": "other"},
        artifacts=[{"version": 2}],
    ).items():
        assert AnswerCache.key(**{**args, field: value}) != key


async def test_read_only_enforced_and_tool_calls_not_cached(store):
    backend = MemoryRedis()
    runner = make_runner(
        store,
        AnswerCache(backend),
        lambda messages, step: (
            tool_call("artifact_save", {"kind": "brief", "title": "x", "content": "x"})
            if step == 0
            else AIMessage(content="无法保存")
        ),
    )
    specs = runner.tools.specs()
    assert {s["function"]["name"] for s in specs} == {"project_read", "artifact_read"}
    result = await runner.run("test", "保存")
    assert result["status"] == "completed" and store.snapshot()["artifacts"] == {}
    assert "UNKNOWN_TOOL" in runner.model.inputs[1][-1].content
    assert result["modelSteps"] == 2
    assert len(backend.values) == 1  # Only final plain text, never the tool call.


async def test_normal_runs_bypass_cache(store):
    backend = MemoryRedis()
    runner = make_runner(store, AnswerCache(backend))
    runner = AgentRunner(
        store=store, model=runner.model, tools=create_project_tools(), answer_cache=runner.answer_cache
    )
    result = await runner.run("test", "hello")
    assert result["answerCache"] == {} and backend.values == {}


@pytest.mark.parametrize(
    "raw",
    [
        b"broken",
        b"[]",
        b"{}",
        b'{"version":1,"answer":""}',
        b'{"version":1,"answer":"x","tool_calls":[]}',
        b"\xff",
    ],
)
async def test_corrupt_entries_fall_back(store, raw):
    class Corrupt(MemoryRedis):
        async def get(self, key):
            return raw

    runner = make_runner(store, AnswerCache(Corrupt()))
    result = await runner.run("test", "hi")
    assert result["status"] == "completed" and runner.model.calls == 1
    assert result["answerCache"]["invalid"] == 1


async def test_timeout_and_write_failure_do_not_fail_run(store):
    class Broken(MemoryRedis):
        async def get(self, key):
            await asyncio.sleep(5)

        async def set(self, *args, **kwargs):
            raise ConnectionError("secret-url")

    result = await make_runner(store, AnswerCache(Broken(), timeout=0.01)).run("test", "hi")
    assert result["status"] == "completed" and result["answerCache"] == {"error": 2}
    assert "secret-url" not in json.dumps(result)


async def test_cancelled_cache_access_propagates():
    class Slow(MemoryRedis):
        async def get(self, key):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await AnswerCache(Slow()).get("key")


@pytest.mark.parametrize(
    "reply",
    [
        AIMessage(content=""),
        tool_call("project_read"),
        AIMessage(content="partial", response_metadata={"finish_reason": "length"}),
    ],
)
async def test_invalid_or_partial_reply_not_stored(reply):
    backend = MemoryRedis()
    assert await AnswerCache(backend).put("key", reply) == "skipped"
    assert not backend.values


async def test_resume_preserves_mode_and_request_id_conflicts(store):
    runner = make_runner(
        store, AnswerCache(MemoryRedis()), lambda messages, step: (_ for _ in ()).throw(ConnectionError())
    )
    result = await runner.run("test", "hi", request_id="same")
    assert result["resumable"]
    normal = AgentRunner(store=store, model=runner.model, tools=create_project_tools())
    with pytest.raises(AppError, match="配置"):
        await normal.resume(result["id"])
    with pytest.raises(AppError):
        await normal.run("test", "hi", request_id="same")
    runner.model.callback = lambda messages, step: AIMessage(content="done")
    assert (await runner.resume(result["id"]))["status"] == "completed"


def test_config_validates_and_redacts():
    config = load_config({"VAGENT_REDIS_URL": "redis://user:secret@localhost:6379/0"})
    assert "secret" not in repr(config)
    for env in (
        {"VAGENT_CACHE_TTL": "0"},
        {"VAGENT_REDIS_URL": "https://localhost"},
        {"VAGENT_REDIS_URL": "redis://host:bad"},
    ):
        with pytest.raises(AppError):
            load_config(env)


def test_cli_cache_config_redaction_and_read_only_option(tmp_path):
    env = {**os.environ, "VAGENT_REDIS_URL": "redis://user:hidden-secret@localhost:6379/0"}
    result = subprocess.run(
        [sys.executable, "-m", "vagent", "config", "show"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0
    assert "hidden-secret" not in result.stdout + result.stderr
    assert json.loads(result.stdout)["redis_configured"] is True
    from vagent.cli import parser

    assert parser().parse_args(["run", "hi", "--read-only"]).read_only


async def test_failed_model_not_cached_and_redis_disabled_read_only(store):
    backend = MemoryRedis()
    runner = make_runner(store, AnswerCache(backend), lambda messages, step: AIMessage(content=""))
    result = await runner.run("test", "hi")
    assert result["status"] == "failed" and not backend.values
    runner = make_runner(store, None)
    result = await runner.run("test", "hi")
    assert result["status"] == "completed" and result["answerCache"] == {}


async def test_real_redis_expiry_and_reconnect(tmp_path, store):
    executable = shutil.which("redis-server")
    if not executable:
        pytest.skip("redis-server executable is not installed")
    with socket.socket() as socket_:
        socket_.bind(("127.0.0.1", 0))
        port = socket_.getsockname()[1]
    config = tmp_path / "redis.conf"
    config.write_text(f'bind 127.0.0.1\nport {port}\nsave ""\nappendonly no\ndir .\n')
    process = subprocess.Popen(
        [executable, config.name],
        cwd=tmp_path,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    cache = AnswerCache.connect(f"redis://127.0.0.1:{port}/0", ttl=1)
    replacement = None
    try:
        ready = False
        for _ in range(50):
            try:
                await cache.client.ping()
                ready = True
                break
            except Exception:
                if process.poll() is not None:
                    break
                await asyncio.sleep(0.1)
        assert ready, "Temporary Redis failed to start"
        runner = make_runner(store, cache)
        first = await runner.run("test", "hi")
        assert first["answerCache"] == {"miss": 1, "stored": 1}
        reset_history(store)
        await cache.aclose()
        replacement = AnswerCache.connect(f"redis://127.0.0.1:{port}/0", ttl=1)
        runner.answer_cache = replacement
        assert (await runner.run("test", "hi"))["answerCache"] == {"hit": 1}
        await asyncio.sleep(1.1)
        reset_history(store)
        assert (await runner.run("test", "hi"))["answerCache"] == {"miss": 1, "stored": 1}
        process.terminate()
        process.wait(timeout=5)
        reset_history(store)
        assert (await runner.run("test", "hi"))["status"] == "completed"
    finally:
        await cache.aclose()
        if replacement:
            await replacement.aclose()
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
