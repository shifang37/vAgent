"""Shared CLI/Web composition and one active execution per data directory."""

import asyncio
import copy
from contextlib import AsyncExitStack, asynccontextmanager
from uuid import uuid4

import httpx

from vagent.cache import AnswerCache
from vagent.config import Config, assert_id
from vagent.context import ContextBuilder
from vagent.errors import AppError
from vagent.mcp_bridge import connect_mcp, server_configs
from vagent.models import DeepSeekModel
from vagent.runner import AgentRunner, RunPolicy
from vagent.skills import SkillCatalog, register_skill_tool
from vagent.storage import FileStore, now
from vagent.tools import create_project_tools
from vagent.usage import summarize_usage


class ApplicationService:
    def __init__(self, config, store, catalog, tools, http_client, *, model=None, policy=None, cache=None):
        self.config, self.store, self.catalog, self.tools = config, store, catalog, tools
        self.http_client, self.model, self.cache = http_client, model, cache
        self.policy = policy or RunPolicy()
        self.mcp_status = []
        self.task = None
        self.cancelled = None
        self.active_run_id = None
        self.subscribers: set[asyncio.Queue] = set()

    @classmethod
    @asynccontextmanager
    async def open(cls, config: Config, *, model=None, policy=None, existing_store=None):
        async with AsyncExitStack() as stack:
            store = existing_store or stack.enter_context(FileStore.open(config.home))
            client = await stack.enter_async_context(httpx.AsyncClient(timeout=60))
            catalog = SkillCatalog.discover(config.skills_root)
            tools = register_skill_tool(create_project_tools(), catalog)
            mcp_status = await stack.enter_async_context(
                connect_mcp(tools, server_configs(config.mcp_config, config.mcp_local))
            )
            cache = AnswerCache.connect(config.redis_url, ttl=config.cache_ttl) if config.redis_url else None
            if cache:
                stack.push_async_callback(cache.aclose)
            service = cls(config, store, catalog, tools, client, model=model, policy=policy, cache=cache)
            service.mcp_status = mcp_status
            try:
                yield service
            finally:
                if service.task and not service.task.done():
                    service.cancelled.set()
                    await service.task

    def runner(self, *, read_only=False, on_event=None, model=None):
        chosen = (
            model
            or self.model
            or DeepSeekModel(self.config.api_key, self.config.model, http_client=self.http_client)
        )
        return AgentRunner(
            store=self.store,
            model=chosen,
            tools=self.tools,
            policy=self.policy,
            context=ContextBuilder(self.config.context_bytes),
            skills=self.catalog.list(),
            read_only=read_only,
            answer_cache=self.cache,
            on_event=on_event,
        )

    def capabilities(self):
        return {
            "model": self.model.name if self.model else self.config.model,
            "apiKeyConfigured": bool(self.config.api_key and self.config.api_key.strip()),
            "modelKind": "injected-test" if self.model else "deepseek",
            "contextBudgetBytes": self.config.context_bytes,
            "redisConfigured": bool(self.cache),
            "answerCacheEnabled": bool(self.cache) and not self.tools.identities,
            "mcp": self.mcp_status,
            "skills": self.catalog.list(),
            "tools": self.tools.inventory(),
            "limits": {
                "modelSteps": self.policy.max_steps,
                "toolCalls": self.policy.max_tool_calls,
                "seconds": self.policy.timeout_seconds,
            },
            "videoGeneration": False,
        }

    def sessions(self):
        state = self.store.snapshot()
        return [
            {
                "id": key,
                "title": state["projects"][key]["goal"][:45] or self._first_prompt(state, key),
                "latestRunId": value.get("latestRunId"),
            }
            for key, value in reversed(list(state["sessions"].items()))
        ]

    @staticmethod
    def _first_prompt(state, key):
        return next((r["prompt"][:45] for r in state["runs"].values() if r["sessionId"] == key), "新建创作")

    def create_session(self):
        session_id = str(uuid4())
        self.store.ensure_session(session_id)
        self.notify(session_id)
        return {"id": session_id}

    def session(self, session_id):
        assert_id(session_id)
        state = self.store.snapshot()
        if session_id not in state["sessions"]:
            raise AppError("NOT_FOUND", "没有这个会话。")
        saved = state["sessions"][session_id]
        run = state["runs"].get(saved.get("latestRunId"))
        # An unfinished run has a provisional transcript; committed history stays intact.
        messages = (run or saved)["messages"]
        visible = []
        for item in messages:
            data = item["data"]
            if item["type"] in {"human", "ai"} and isinstance(data.get("content"), str) and data["content"]:
                visible.append(
                    {"role": "user" if item["type"] == "human" else "assistant", "text": data["content"]}
                )
        return {
            "id": session_id,
            "project": state["projects"][session_id],
            "messages": visible,
            "run": self.run_view(run) if run else None,
            "artifacts": [a for a in state["artifacts"].values() if a["projectId"] == session_id],
        }

    @staticmethod
    def run_view(record):
        fields = (
            "id",
            "sessionId",
            "requestId",
            "model",
            "status",
            "answer",
            "errorCode",
            "resumable",
            "modelSteps",
            "toolCalls",
            "contextBytes",
            "droppedMessages",
            "activeSeconds",
            "policy",
            "readOnly",
            "contextVersion",
            "events",
            "createdAt",
            "updatedAt",
        )
        current_turn = []
        for message in record["messages"]:
            if message["type"] == "human":
                current_turn = []
            current_turn.append(message)
        calls = {}
        for message in current_turn:
            data = message["data"]
            if message["type"] == "ai":
                for call in data.get("tool_calls", []):
                    calls[call["id"]] = {
                        "id": call["id"],
                        "name": call["name"],
                        "arguments": call["args"],
                        "result": None,
                    }
            elif message["type"] == "tool" and data.get("tool_call_id") in calls:
                calls[data["tool_call_id"]]["result"] = data.get("content")
        return {
            **{key: copy.deepcopy(record.get(key)) for key in fields},
            "usage": summarize_usage(record),
            "toolTrace": list(calls.values()),
        }

    def run_record(self, run_id):
        assert_id(run_id)
        record = self.store.snapshot()["runs"].get(run_id)
        if record is None:
            raise AppError("NOT_FOUND", "没有这个 Run。")
        return record

    def notify(self, session_id):
        for queue in tuple(self.subscribers):
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(session_id)

    async def start(self, session_id, prompt, request_id, *, read_only=False):
        assert_id(session_id)
        assert_id(request_id)
        if not prompt.strip() or len(prompt) > 20000:
            raise AppError("INVALID_PROMPT", "需求不能为空，且不能超过 20000 字符。")
        for record in self.store.snapshot()["runs"].values():
            if record["sessionId"] == session_id and record["requestId"] == request_id:
                if record["prompt"] != prompt or record.get("readOnly", False) != read_only:
                    raise AppError("REQUEST_CONFLICT", "相同请求 ID 不能关联不同需求或模式。")
                return self.run_view(record)
        if self.task and not self.task.done():
            raise AppError("RUN_BUSY", "已有任务正在运行，请等待完成或停止后重试。")
        runner = self.runner(read_only=read_only)  # Missing keys fail before creating a run.
        return await self._launch(runner, session_id, prompt=prompt, request_id=request_id)

    async def resume(self, run_id):
        record = self.run_record(run_id)
        if record["status"] == "completed":
            return self.run_view(record)
        if self.task and not self.task.done():
            raise AppError("RUN_BUSY", "已有任务正在运行，请等待完成或停止后重试。")
        runner = self.runner(read_only=record.get("readOnly", False))
        return await self._launch(runner, record["sessionId"], resume_id=run_id)

    async def _launch(self, runner, session_id, *, prompt=None, request_id=None, resume_id=None):
        ready = asyncio.Event()
        errors = []
        self.active_run_id = resume_id
        self.cancelled = asyncio.Event()

        def event_sink(event):
            if event.get("runId"):
                self.active_run_id = event["runId"]
            if self.active_run_id:

                def record_event(draft):
                    events = draft["runs"][self.active_run_id].setdefault("events", [])
                    events.append({**event, "sequence": len(events) + 1, "at": now()})

                self.store.transaction(record_event)
                ready.set()
            self.notify(session_id)

        runner.on_event = event_sink

        async def execute():
            try:
                result = (
                    await runner.resume(resume_id, cancelled=self.cancelled)
                    if resume_id
                    else await runner.run(session_id, prompt, request_id=request_id, cancelled=self.cancelled)
                )
                self.active_run_id = result["id"]
            except Exception as error:
                errors.append(error)
            finally:
                ready.set()
                self.notify(session_id)

        self.task = asyncio.create_task(execute())
        await ready.wait()
        if errors:
            raise errors[0]
        return self.run_view(self.run_record(self.active_run_id))

    def stop(self, run_id):
        record = self.run_record(run_id)
        if self.active_run_id == run_id and self.task and not self.task.done():
            self.cancelled.set()
        elif record["status"] == "running":
            raise AppError("RUN_BUSY", "该 Run 不属于当前 Web 执行进程。")
        return {"runId": run_id, "stopRequested": record["status"] == "running"}
