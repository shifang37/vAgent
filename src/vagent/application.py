"""Shared CLI/Web composition and one active execution per data directory."""

import asyncio
import copy
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from uuid import uuid4

from vagent.cache import AnswerCache
from vagent.config import (
    VIDEO_SETTINGS,
    Config,
    ConfigUpdate,
    assert_id,
    config_sources,
    require_key,
    update_local_settings,
)
from vagent.context import ContextBuilder
from vagent.errors import AppError, public_error
from vagent.http import ModelHttpClient
from vagent.mcp_bridge import connect_mcp, server_configs
from vagent.models import DeepSeekModel
from vagent.runner import AgentRunner, RunPolicy
from vagent.skills import discover_for, register_skill_tool
from vagent.storage import FileStore, now
from vagent.tools import create_project_tools
from vagent.usage import summarize_usage
from vagent.video import clients
from vagent.video.assets import asset_reference_loader
from vagent.video.contracts import ImageInputRule
from vagent.video.jobs import JobService
from vagent.video.media import file_io
from vagent.video.media_http import MediaHttpClient
from vagent.video.media_worker import MediaWorker
from vagent.video.providers.fal import FalAdapter
from vagent.video.providers.kling import KlingAdapter
from vagent.video.providers.mock import MockVideoAdapter
from vagent.video.providers.wan import WanAdapter
from vagent.video.storyboards import storyboard_in_state
from vagent.video.tools import register_video_tools, resolve_job_wait, resolve_jobset_wait
from vagent.video.usage import summarize_records
from vagent.video.views import current_storyboard_view, job_view, storyboard_summaries, storyboard_view
from vagent.video.worker import JobWorker
from vagent.wait_runtime import WaitCoordinator, execution_control
from vagent.waiting import WAIT_EXECUTION_VERSIONS


class ApplicationService:
    def __init__(
        self,
        config,
        store,
        catalog,
        tools,
        http_client,
        *,
        model=None,
        policy=None,
        cache=None,
        on_event=None,
    ):
        self.config, self.store, self.catalog, self.tools = config, store, catalog, tools
        self._settings_config = config
        self.video_probe = None
        self.wan_adapter = None
        self.kling_adapter = None
        self.fal_adapter = None
        self.tool_views = {config.video_mode: tools}
        self.http_client, self.model, self.cache = http_client, model, cache
        self.policy = policy or RunPolicy()
        self.mcp_status = []
        self.task = None
        self.cancelled = None
        self.active_run_id = None
        self.subscribers: dict[asyncio.Queue, str] = {}
        self.drafts: dict[str, dict] = {}
        self.validation = {"status": "unverified"}
        self.config_busy = False
        self.video_jobs = None
        self.job_worker = None
        self.media = None
        self.media_worker = None
        self.films = None
        self._event_loop = None
        self._job_revisions = {key: job["revision"] for key, job in store.snapshot()["jobs"].items()}
        self.on_event = on_event
        self.wait_coordinator = None
        self.shutdown = asyncio.Event()

    @classmethod
    @asynccontextmanager
    async def open(cls, config: Config, *, model=None, policy=None, existing_store=None, on_event=None):
        async with AsyncExitStack() as stack:
            store = existing_store or stack.enter_context(FileStore.open(config.home))
            client = await stack.enter_async_context(ModelHttpClient(timeout=60))
            catalog = discover_for(config)
            tools = register_skill_tool(create_project_tools(), catalog)
            # A separate HTTP pool is closed after Workers release all in-flight calls.
            wan = await stack.enter_async_context(
                WanAdapter(
                    api_key=config.video_api_key,
                    workspace_id=config.video_workspace_id,
                    model=config.video_model,
                    region=config.video_region,
                    reference_loader=asset_reference_loader(store, ImageInputRule()),
                )
            )
            # The E2 providers publish one frozen combination each; a missing Key
            # disables only that provider and never blocks the others.
            kling = await stack.enter_async_context(KlingAdapter(api_key=config.kling_api_key))
            fal = await stack.enter_async_context(FalAdapter(api_key=config.fal_api_key))
            video_jobs = JobService(store, [MockVideoAdapter(store), wan, kling, fal], config=config)
            media_client = await stack.enter_async_context(MediaHttpClient())
            tool_views = {mode: tools.copy() for mode in ("off", "mock", "live")}
            for mode in ("mock", "live"):
                register_video_tools(tool_views[mode], video_jobs, mode=mode)
            tools = tool_views[config.video_mode]
            mcp_status = await stack.enter_async_context(
                connect_mcp(tools, server_configs(config.mcp_config, config.mcp_local))
            )
            for view in tool_views.values():
                if view is not tools:
                    view.include_external_tools(tools)
            cache = AnswerCache.connect(config.redis_url, ttl=config.cache_ttl) if config.redis_url else None
            if cache:
                stack.push_async_callback(cache.aclose)
            service = cls(
                config,
                store,
                catalog,
                tools,
                client,
                model=model,
                policy=policy,
                cache=cache,
                on_event=on_event,
            )
            service.mcp_status = mcp_status
            service.tool_views = tool_views
            service.video_jobs = video_jobs
            service.wan_adapter = wan
            service.kling_adapter = kling
            service.fal_adapter = fal
            service.media = video_jobs.media
            service.films = video_jobs.films
            service._event_loop = asyncio.get_running_loop()
            video_jobs.on_change = service._job_event
            service.job_worker = JobWorker(video_jobs)
            service.media_worker = MediaWorker(service.media, media_client)
            service.wait_coordinator = WaitCoordinator(
                store,
                service._waiting_runner,
                resolvers={
                    "job": lambda binding: resolve_job_wait(store, binding, service=video_jobs),
                    "jobset": lambda binding: resolve_jobset_wait(store, binding, service=video_jobs),
                },
                on_event=service._wait_event,
            )
            stack.push_async_callback(service.aclose)
            await file_io(service.media.refresh_all)
            service.wait_coordinator.start()
            service.job_worker.start()
            service.media_worker.start()
            yield service

    async def aclose(self):
        # Exit is not a user stop. Close both scheduling gates before awaiting any
        # provider cancellation, so its final write cannot wake another model call.
        self.shutdown.set()
        if self.wait_coordinator:
            self.wait_coordinator.shutdown.set()
        async with AsyncExitStack() as closing:
            closing.push_async_callback(self._close_execution)
            if self.wait_coordinator:
                closing.push_async_callback(self.wait_coordinator.stop)
            if self.job_worker:
                closing.push_async_callback(self.job_worker.stop)
            if self.media_worker:
                closing.push_async_callback(self.media_worker.stop)

    async def _close_execution(self):
        if self.task and not self.task.done():
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task

    def _observe_event(self, event):
        if self.on_event:
            with suppress(Exception):
                self.on_event(event)

    def _job_event(self, job):
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        if self._event_loop is not None and current_loop is not self._event_loop:
            self._event_loop.call_soon_threadsafe(self._job_event, job)
            return
        if job.revision <= self._job_revisions.get(job.id, -1):
            return
        self._job_revisions[job.id] = job.revision
        event = {
            "type": "job.updated",
            "sessionId": job.context.session_id,
            "runId": job.context.run_id,
            "job": job_view(job),
        }
        self.notify(job.context.session_id, event)
        self._observe_event(event)
        if self.wait_coordinator:
            self.wait_coordinator.notify()

    def jobs(self, session_id=None):
        if session_id is not None:
            assert_id(session_id)
        return [
            job_view(self.media.refresh(job)) for job in reversed(self.video_jobs.list(session_id=session_id))
        ]

    def job(self, job_id):
        assert_id(job_id)
        return job_view(self.media.refresh(self.video_jobs.get(job_id)))

    def retry_download(self, job_id, request):
        assert_id(job_id)
        job = self.video_jobs.get(job_id)
        return self.media.retry_download(job_id, request, project_id=job.context.project_id)

    def retry_query(self, job_id):
        assert_id(job_id)
        job = self.video_jobs.get(job_id)
        return job_view(self.video_jobs.retry_query(job_id, project_id=job.context.project_id))

    def storyboards(self, session_id):
        """Storyboard summaries of one project; never another project's records."""
        assert_id(session_id)
        return storyboard_summaries(self.store.snapshot(), session_id)

    def storyboard(self, session_id, storyboard_id=None):
        """The one storyboard view shared by the panel, the CLI and the tools."""
        assert_id(session_id)
        state = self.store.snapshot()
        if storyboard_id is None:
            view = current_storyboard_view(state, session_id)
            if view is None:
                raise AppError("NOT_FOUND", "当前项目还没有结构化分镜，请先保存。")
            return view
        assert_id(storyboard_id)
        return storyboard_view(state, storyboard_in_state(state, session_id, storyboard_id))

    def assets(self, project_id=None):
        """Asset metadata in the whole library, or one project's own imports."""
        if project_id is not None:
            assert_id(project_id)
        return self.video_jobs.assets.list(project_id=project_id)

    def asset(self, asset_id, project_id=None):
        if project_id is not None:
            assert_id(project_id)
        assert_id(asset_id)
        return self.video_jobs.assets.view(asset_id, project_id=project_id)

    def import_asset(self, project_id, data, filename=None):
        """Trusted local import: validated, deduplicated and never an Agent tool."""
        assert_id(project_id)
        return self.video_jobs.assets.import_bytes(data, project_id=project_id, filename=filename)

    def preview_shots(self, session_id, *, storyboard_id, storyboard_version, shot_ids):
        """Per-shot estimates of one call, without registering or submitting anything."""
        assert_id(session_id)
        return self.video_jobs.preview_shots(
            session_id,
            {
                "storyboardId": storyboard_id,
                "storyboardVersion": storyboard_version,
                "shotIds": list(shot_ids),
                "confirm": False,
            },
        )

    def generate_shots(self, session_id, request: dict) -> dict:
        """Batch registration from the local UI, under its own client Run."""
        assert_id(session_id)
        return clients.generate_shots(
            self.video_jobs,
            session_id=session_id,
            client_request_id=request["clientRequestId"],
            storyboard_id=request["storyboardId"],
            storyboard_version=request["storyboardVersion"],
            shot_ids=request["shotIds"],
            confirm=request["confirm"],
        )

    def redo_shot(self, session_id, shot_id, request: dict) -> dict:
        """Explicit per-shot redo from the local UI or CLI, never a model turn."""
        assert_id(session_id)
        assert_id(shot_id)
        return clients.redo_shot(
            self.video_jobs,
            session_id=session_id,
            client_request_id=request["clientRequestId"],
            storyboard_id=request["storyboardId"],
            storyboard_version=request["storyboardVersion"],
            shot_id=shot_id,
            expected_revision=request["expectedRevision"],
        )

    def film(self, session_id, storyboard_id):
        """Current assembled film of one storyboard, or an empty projection."""
        assert_id(session_id)
        assert_id(storyboard_id)
        return self.films.view(session_id, storyboard_id)

    async def assemble_film(self, session_id, request: dict) -> dict:
        """Explicit local concatenation from the CLI/Web; zero provider calls."""
        assert_id(session_id)
        return await clients.assemble_film(
            self.video_jobs,
            session_id=session_id,
            client_request_id=request["clientRequestId"],
            storyboard_id=request["storyboardId"],
            expected_revision=request["expectedRevision"],
        )

    def usage(self, project_id=None):
        """Local aggregation of Jobs, estimates, tokens and the asset library."""
        if project_id is not None:
            assert_id(project_id)
        return summarize_records(self.store.snapshot(), project_id=project_id)

    async def work(self):
        """Keep the shared background services alive without creating a new Run."""
        done, _ = await asyncio.wait(
            [self.job_worker.start(), self.media_worker.start(), self.wait_coordinator.start()],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in done:
            task.result()
        raise AppError("BACKGROUND_STOPPED", "后台工作循环已退出，请检查状态后重启服务。")

    async def wait_for_run(self, run_id):
        """CLI waits for notifications; the graph releases execution while suspended."""
        queue = self.subscribe(self.run_record(run_id)["sessionId"])
        try:
            while True:
                record = self.run_record(run_id)
                executing = execution_control(self.store).run_id == run_id or (
                    self.active_run_id == run_id and self.task and not self.task.done()
                )
                if not executing and (
                    record["status"] not in {"running", "waiting_external"} or record.get("waitResumeError")
                ):
                    return record
                for background in (
                    self.job_worker._task,
                    self.media_worker._task,
                    self.wait_coordinator._task,
                ):
                    if background.done():
                        background.result()
                        raise AppError("BACKGROUND_STOPPED", "后台工作循环已退出，任务状态已保留。")
                try:
                    # Re-read durable state after notification loss or a missed startup event.
                    # A timeout scope preserves simultaneous cancellation on Python 3.11.
                    async with asyncio.timeout(0.5):
                        await queue.get()
                except TimeoutError:
                    pass
        finally:
            self.subscribers.pop(queue, None)

    def _waiting_runner(self, record):
        if self.config_busy:
            raise AppError("CONFIG_BUSY", "配置验证正在进行，外部结果已保留，稍后继续原 Run。")
        self._check_resume_video(record)
        return self.runner(
            read_only=record.get("readOnly", False),
            video_mode=record.get("videoMode", "off"),
            on_event=self._event_sink(record["sessionId"], run_id=record["id"]),
        )

    def _wait_event(self, event):
        record = self.run_record(event["runId"])
        self._event_sink(record["sessionId"], run_id=record["id"])(event)

    def runner(self, *, read_only=False, on_event=None, model=None, video_mode=None):
        mode = self.config.video_mode if video_mode is None else video_mode
        if mode not in self.tool_views:
            raise AppError("RESUME_CONFIG_CHANGED", "缺少原 Run 模式的工具视图。")
        tools = self.tool_views[mode]
        chosen = (
            model
            or self.model
            or DeepSeekModel(self.config.api_key, self.config.model, http_client=self.http_client)
        )
        return AgentRunner(
            store=self.store,
            model=chosen,
            tools=tools,
            policy=self.policy,
            context=ContextBuilder(self.config.context_bytes),
            skills=self.catalog.list(),
            read_only=read_only,
            answer_cache=self.cache,
            on_event=on_event,
        )

    def headless_runner(self, *, model=None, on_event=None, video_mode=None):
        """The planning-only view: storyboard tools stay, generation tools do not.

        The Run keeps the published tools/rules version and its own signature; only
        the callable set is narrowed, so a real model can draft but never register a
        paid Job before the user authorizes the fixed plan. ``video_mode`` lets a
        resume rebuild the recorded planning view even when the current mode differs.
        """
        from vagent.headless import PLANNING_EXCLUDED_TOOLS, PLANNING_RULES

        mode = self.config.video_mode if video_mode is None else video_mode
        if mode not in {"mock", "live"}:
            raise AppError("VIDEO_MODE_OFF", "headless 需要 mock 或 live 视频模式；当前为 off。")
        tools = self.tool_views[mode].without_tools(PLANNING_EXCLUDED_TOOLS)
        chosen = (
            model
            or self.model
            or DeepSeekModel(self.config.api_key, self.config.model, http_client=self.http_client)
        )
        return AgentRunner(
            store=self.store,
            model=chosen,
            tools=tools,
            policy=self.policy,
            context=ContextBuilder(self.config.context_bytes),
            skills=self.catalog.list(),
            answer_cache=self.cache,
            on_event=on_event,
            extra_rules=PLANNING_RULES,
        )

    def capabilities(self):
        return {
            "model": self.model.name if self.model else self.config.model,
            "apiKeyConfigured": bool(self.config.api_key and self.config.api_key.strip()),
            "modelKind": "injected-test" if self.model else "deepseek",
            "contextBudgetBytes": self.config.context_bytes,
            "redisConfigured": bool(self.cache),
            "answerCacheEnabled": bool(self.cache)
            and not self.tools.identities
            and not self.tools.bypass_answer_cache,
            "mcp": self.mcp_status,
            "skills": self.catalog.list(),
            "skillDiagnostics": self.catalog.diagnostics,
            "tools": self.tools.inventory(),
            "limits": {
                "modelSteps": self.policy.max_steps,
                "toolCalls": self.policy.max_tool_calls,
                "seconds": self.policy.timeout_seconds,
            },
            "videoGeneration": self.config.video_mode in {"mock", "live"},
            "videoMode": self.config.video_mode,
            "videoSimulated": self.config.video_mode == "mock",
            "videoMediaAvailable": any(
                raw.get("mediaAvailability", {}).get("status") == "available"
                for raw in self.store.snapshot()["jobs"].values()
            ),
            "jobWorkerRunning": bool(
                self.job_worker and self.job_worker._task and not self.job_worker._task.done()
            ),
            "mediaWorkerRunning": bool(
                self.media_worker and self.media_worker._task and not self.media_worker._task.done()
            ),
            "configuration": self.configuration(),
        }

    def configuration(self):
        saved = self._settings_config
        sources = config_sources(saved)
        restart_fields = [
            name
            for name, (attr, _, _) in VIDEO_SETTINGS.items()
            if getattr(saved, attr) != getattr(self.config, attr)
        ]
        return {
            "model": self.config.model,
            **{
                name: getattr(saved, attr)
                for name, (attr, _, _) in VIDEO_SETTINGS.items()
                if name not in {"videoApiKey", "klingApiKey", "falApiKey"}
            },
            "activeVideoMode": self.config.video_mode,
            "videoApiKeyConfigured": bool(saved.video_api_key),
            "klingApiKeyConfigured": bool(saved.kling_api_key),
            "falApiKeyConfigured": bool(saved.fal_api_key),
            "videoPermissionStatus": "unverified",
            "providers": self.provider_configuration(),
            "videoProbe": copy.deepcopy(self.video_probe),
            "videoEstimate": self.video_jobs.price.model_dump(mode="json", by_alias=True)
            if self.video_jobs and self.video_jobs.price
            else None,
            "restartRequired": bool(restart_fields),
            "restartFields": restart_fields,
            "apiKeyConfigured": bool(self.config.api_key and self.config.api_key.strip()),
            "sources": sources,
            "editable": {key: source not in {"environment", "provided"} for key, source in sources.items()},
            "validation": copy.deepcopy(self.validation),
            "busy": self.config_busy
            or bool(self.task and not self.task.done())
            or execution_control(self.store).lock.locked(),
        }

    def provider_configuration(self):
        """Per-provider keys, frozen rates and verification state for the settings page."""
        if not self.video_jobs:
            return []
        entries, seen = [], set()
        for capabilities in self.video_jobs.capabilities("live"):
            provider = capabilities.provider
            if provider in seen:
                continue
            seen.add(provider)
            rate = getattr(capabilities, "rate", None)
            if rate is not None:
                estimate = {
                    "currency": rate.currency,
                    "unitPrice": rate.unit_price,
                    "priceVersion": rate.price_version,
                    "sourceUrl": rate.source_url,
                }
                resource = (
                    {
                        "unit": capabilities.resource.unit,
                        "unitsPerSecond": capabilities.resource.units_per_second,
                    }
                    if getattr(capabilities, "resource", None)
                    else None
                )
            else:
                price = self.video_jobs.price
                estimate = (
                    {
                        "currency": price.currency,
                        "unitPrice": price.unit_price,
                        "priceVersion": price.price_version,
                        "sourceUrl": price.source_url,
                    }
                    if price
                    else None
                )
                resource = None
            entries.append(
                {
                    "provider": provider,
                    "model": self.config.video_model if provider == "wan" else capabilities.model,
                    "keyConfigured": bool(self.video_jobs.provider_key(provider)),
                    # New providers publish no verified zero-cost auth probe.
                    "permissionStatus": "unverified",
                    "estimate": estimate,
                    "resource": resource,
                }
            )
        return entries

    def _check_config_idle(self):
        if (
            self.config_busy
            or (self.task and not self.task.done())
            or execution_control(self.store).lock.locked()
        ):
            raise AppError("CONFIG_BUSY", "请等待当前执行或配置验证结束后再修改配置。")

    async def save_configuration(self, update: ConfigUpdate):
        self._check_config_idle()
        sources = config_sources(self._settings_config)
        new_video_key = any(
            name in update.model_fields_set and getattr(update, name) is not None
            for name in ("videoApiKey", "klingApiKey", "falApiKey")
        )
        already_live = any(
            (
                self._settings_config.video_api_key,
                self._settings_config.kling_api_key,
                self._settings_config.fal_api_key,
            )
        )
        auto_live = (
            new_video_key
            and not already_live
            and self._settings_config.video_mode == "off"
            and "videoMode" not in update.model_fields_set
            and sources["videoMode"] not in {"environment", "provided"}
        )
        if auto_live:
            # K2 freeze (decision 2): the first saved video key switches off to
            # live once; an explicit mode in any save always writes the choice.
            update = ConfigUpdate.model_validate(
                {**update.model_dump(exclude_unset=True), "videoMode": "live"}
            )
        previous = (self.config.api_key, self.config.model)
        previous_video = (
            self._settings_config.video_api_key,
            self._settings_config.video_workspace_id,
        )
        updated = update_local_settings(self._settings_config, update)
        self._settings_config = updated
        # K2: saved video settings apply to new registrations immediately, with no
        # restart; in-flight and historical Jobs keep their frozen identities.
        self.config = updated
        self.video_jobs.config = updated
        self.wan_adapter.update_credentials(
            api_key=updated.video_api_key, workspace_id=updated.video_workspace_id
        )
        if self.kling_adapter is not None:
            self.kling_adapter.update_credentials(api_key=updated.kling_api_key)
        if self.fal_adapter is not None:
            self.fal_adapter.update_credentials(api_key=updated.fal_api_key)
        if previous != (self.config.api_key, self.config.model):
            self.validation = {"status": "unverified"}
        if (updated.video_api_key, updated.video_workspace_id) != previous_video:
            self.video_probe = None
        touched_credentials = {"videoApiKey", "videoWorkspaceId"} & update.model_fields_set
        if touched_credentials and updated.video_api_key:
            # Zero-cost read-only probe: one query for a task ID that cannot
            # exist. It registers nothing and never blocks the saved settings.
            # Kling and fal publish no verified free auth endpoint, so their saved
            # keys stay explicitly unverified instead of probing with a paid task.
            self.config_busy = True
            try:
                self.video_probe = await self.wan_adapter.probe_authentication()
            finally:
                self.config_busy = False
        result = self.configuration()
        result["autoEnabledLive"] = auto_live
        return result

    async def validate_configuration(self):
        self._check_config_idle()
        key = require_key(self.config.api_key)
        self.config_busy = True
        self.validation = {"status": "validating"}
        try:
            async with asyncio.timeout(15):
                response = await self.http_client.post(
                    "https://api.deepseek.com/chat/completions",
                    headers={"Authorization": f"Bearer {key}"},
                    json={
                        "model": self.config.model,
                        "messages": [{"role": "user", "content": "Reply OK."}],
                        "max_tokens": 8,
                        "thinking": {"type": "disabled"},
                        "stream": False,
                    },
                    timeout=15,
                    extensions={"vagent_connect_retries": False},
                )
                if response.status_code in {401, 403}:
                    raise AppError("AUTH_ERROR", "DeepSeek 凭证无效或没有模型权限。")
                if response.status_code == 429:
                    raise AppError("RATE_LIMIT", "DeepSeek 请求限流，请稍后重试。")
                response.raise_for_status()
                body = response.json()
                choices = body.get("choices") if isinstance(body, dict) else None
                first = choices[0] if isinstance(choices, list) and choices else None
                message = first.get("message") if isinstance(first, dict) else None
                content = message.get("content") if isinstance(message, dict) else None
                if not isinstance(content, str) or not content.strip():
                    raise AppError("INVALID_RESPONSE", "验证未取得有效模型回复。")
                usage = body.get("usage") or {}
                usage = usage if isinstance(usage, dict) else {}

                def token_count(name):
                    value = usage.get(name)
                    return value if type(value) is int and value >= 0 else None

                self.validation = {
                    "status": "verified",
                    "checkedAt": now(),
                    "model": self.config.model,
                    "inputTokens": token_count("prompt_tokens"),
                    "outputTokens": token_count("completion_tokens"),
                }
        except asyncio.CancelledError:
            self.validation = {"status": "unverified"}
            raise
        except Exception as error:
            safe = public_error(error)
            self.validation = {"status": "failed", "checkedAt": now(), "errorCode": safe.code}
            raise safe from None
        finally:
            self.config_busy = False
        return self.configuration()

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
            "jobs": self.jobs(session_id),
            "storyboard": current_storyboard_view(state, session_id),
            "wait": self._wait_view(state["waits"].get((run or {}).get("activeWaitId"))),
            "draft": copy.deepcopy(self.drafts.get(session_id)),
        }

    @staticmethod
    def _wait_view(binding):
        if not binding:
            return None
        return {
            key: copy.deepcopy(binding.get(key))
            for key in ("id", "resource", "status", "startedAt", "deadlineAt", "autoResume")
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
            "activeWaitId",
            "externalWaitSeconds",
            "externalWaitStartedAt",
            "waitResumeError",
            "policy",
            "readOnly",
            "contextVersion",
            "videoMode",
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

    def subscribe(self, session_id):
        queue = asyncio.Queue(maxsize=64)
        self.subscribers[queue] = session_id
        return queue

    def notify(self, session_id, event=None):
        for queue, subscribed_id in tuple(self.subscribers.items()):
            if subscribed_id != session_id:
                continue
            if queue.full():
                while not queue.empty():
                    queue.get_nowait()
                # A slow reader gets current text and sequence in a snapshot,
                # never an arbitrary hole in its token stream.
                queue.put_nowait({"type": "snapshot"})
            else:
                queue.put_nowait(event or {"type": "snapshot"})

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
        if self.config_busy:
            raise AppError("CONFIG_BUSY", "配置验证正在进行，请稍后发送需求。")
        if self.task and not self.task.done():
            raise AppError("RUN_BUSY", "已有任务正在运行，请等待完成或停止后重试。")
        if any(
            record["status"] in {"running", "waiting_external"}
            for record in self.store.snapshot()["runs"].values()
        ):
            raise AppError("RUN_BUSY", "已有任务正在运行或等待外部结果，请完成或停止后重试。")
        runner = self.runner(read_only=read_only)  # Missing keys fail before creating a run.
        return await self._launch(runner, session_id, prompt=prompt, request_id=request_id)

    async def resume(self, run_id):
        record = self.run_record(run_id)
        if record["status"] == "completed":
            return self.run_view(record)
        if self.config_busy:
            raise AppError("CONFIG_BUSY", "配置验证正在进行，请稍后继续任务。")
        if self.task and not self.task.done():
            raise AppError("RUN_BUSY", "已有任务正在运行，请等待完成或停止后重试。")
        self._check_resume_video(record)
        runner = self.runner(
            read_only=record.get("readOnly", False), video_mode=record.get("videoMode", "off")
        )
        return await self._launch(runner, record["sessionId"], resume_id=run_id)

    def _check_resume_video(self, record):
        if record.get("videoMode", "off") != "live":
            return
        try:
            jobs = [
                job
                for job in self.video_jobs.list(session_id=record["sessionId"])
                if job.context.run_id == record["id"]
            ]
            providers = {job.request.provider for job in jobs} or {self.config.video_provider}
            for provider in providers:
                self.video_jobs.check_live_configuration(provider)
            for job in jobs:
                self.video_jobs.check_runtime(job)
        except AppError:
            raise AppError(
                "RESUME_CONFIG_CHANGED",
                "原 live Run 的视频配置不可用，任务与等待结果已保留；请补齐原视频 Key 或业务空间配置。",
            ) from None

    def _event_sink(self, session_id, *, run_id=None, ready=None):
        def event_sink(event):
            nonlocal run_id
            if event.get("runId"):
                run_id = event["runId"]
                self.active_run_id = run_id
            event = {**event, "runId": run_id, "sessionId": session_id}
            if event["type"] == "assistant.delta":
                draft = self.drafts.get(session_id)
                if draft is None or (draft["runId"], draft["step"]) != (event["runId"], event["step"]):
                    draft = {"runId": event["runId"], "step": event["step"], "text": ""}
                    self.drafts[session_id] = draft
                draft["text"] += event["text"]
                draft["sequence"] = event["sequence"]
                self.notify(session_id, event)
                self._observe_event(event)
                return  # Drafts never enter state.json or the event journal.
            if event["type"] in {"model.started", "model.completed", "assistant.discarded", "run.completed"}:
                self.drafts.pop(session_id, None)
            if run_id:

                def record_event(draft):
                    events = draft["runs"][run_id].setdefault("events", [])
                    events.append({**event, "sequence": len(events) + 1, "at": now()})

                self.store.transaction(record_event)
                if ready:
                    ready.set()
            if event["type"] in {"run.waiting", "run.resumed", "run.resume_blocked", "run.completed"}:
                self.notify(session_id, event)
            else:
                self.notify(session_id)
            self._observe_event(event)

        return event_sink

    async def _launch(self, runner, session_id, *, prompt=None, request_id=None, resume_id=None):
        ready = asyncio.Event()
        errors = []
        self.active_run_id = resume_id
        self.cancelled = asyncio.Event()

        runner.on_event = self._event_sink(session_id, run_id=resume_id, ready=ready)

        async def execute():
            try:
                result = (
                    await runner.resume(resume_id, cancelled=self.cancelled, shutdown=self.shutdown)
                    if resume_id
                    else await runner.run(
                        session_id,
                        prompt,
                        request_id=request_id,
                        cancelled=self.cancelled,
                        shutdown=self.shutdown,
                    )
                )
                self.active_run_id = result["id"]
            except Exception as error:
                errors.append(error)
            finally:
                self.drafts.pop(session_id, None)
                ready.set()
                self.notify(session_id)

        self.task = asyncio.create_task(execute())
        await ready.wait()
        if errors:
            raise errors[0]
        return self.run_view(self.run_record(self.active_run_id))

    def stop(self, run_id):
        record = self.run_record(run_id)
        if record.get("executionVersion") in WAIT_EXECUTION_VERSIONS and self.wait_coordinator:
            stopped = self.wait_coordinator.stop_run(run_id)
            self.notify(record["sessionId"])
            return {"runId": run_id, "stopRequested": stopped}
        if self.active_run_id == run_id and self.task and not self.task.done():
            self.cancelled.set()
        elif record["status"] == "running":
            raise AppError("RUN_BUSY", "该 Run 不属于当前 Web 执行进程。")
        return {"runId": run_id, "stopRequested": record["status"] == "running"}
