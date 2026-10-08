"""Loopback-only Web API and bundled UI, using the shared application service."""

import asyncio
import json
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from vagent import __version__
from vagent.application import ApplicationService
from vagent.config import ConfigUpdate
from vagent.errors import AppError, public_error


class MessageInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    prompt: str = Field(min_length=1, max_length=20000)
    clientRequestId: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    readOnly: bool = False


def create_app(config, *, port=3210, model=None, policy=None):
    token = secrets.token_urlsafe(32)
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    root = Path(__file__).parent / "web"
    if not root.is_dir():
        root = Path(__file__).parents[2] / "web"

    @asynccontextmanager
    async def lifespan(app):
        async with ApplicationService.open(config, model=model, policy=policy) as service:
            app.state.service = service
            yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        host = request.headers.get("host", "")
        if host not in allowed_hosts:
            return JSONResponse({"error": {"code": "HOST_DENIED", "message": "仅允许本地访问。"}}, 403)
        origin = request.headers.get("origin")
        if origin and origin != f"http://{host}":
            return JSONResponse({"error": {"code": "ORIGIN_DENIED", "message": "拒绝跨来源请求。"}}, 403)
        if request.headers.get("sec-fetch-site") in {"cross-site", "same-site"}:
            return JSONResponse({"error": {"code": "ORIGIN_DENIED", "message": "仅接受同源请求。"}}, 403)
        if request.method not in {"GET", "HEAD"}:
            if not secrets.compare_digest(request.headers.get("x-csrf-token", ""), token):
                return JSONResponse({"error": {"code": "CSRF_DENIED", "message": "请刷新页面后重试。"}}, 403)
            if request.headers.get("content-type", "").split(";")[0] != "application/json":
                return JSONResponse({"error": {"code": "CONTENT_TYPE", "message": "需要 JSON 请求。"}}, 415)
            size = 0
            chunks = []
            async for chunk in request.stream():
                size += len(chunk)
                if size > 131072:
                    return JSONResponse({"error": {"code": "BODY_LIMIT", "message": "请求内容过大。"}}, 413)
                chunks.append(chunk)
            request._body = b"".join(chunks)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
            "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        return response

    @app.exception_handler(AppError)
    async def app_error(_request, error):
        status = (
            404
            if error.code == "NOT_FOUND"
            else 409
            if error.code
            in {
                "RUN_BUSY",
                "REQUEST_CONFLICT",
                "STALE_RUN",
                "NOT_RESUMABLE",
                "RESUME_CONFIG_CHANGED",
                "CONFIG_BUSY",
                "CONFIG_OVERRIDE",
            }
            else 400
        )
        return JSONResponse({"error": {"code": error.code, "message": str(error)}}, status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request, _error):
        return JSONResponse({"error": {"code": "INVALID_REQUEST", "message": "请求字段不符合要求。"}}, 422)

    @app.exception_handler(Exception)
    async def unexpected_error(_request, error):
        safe = public_error(error)
        return JSONResponse({"error": {"code": safe.code, "message": str(safe)}}, 500)

    @app.get("/api/health")
    async def health():
        return {"status": "ready", "version": __version__, **app.state.service.capabilities()}

    @app.get("/api/session-token")
    async def session_token():
        return {"token": token}

    @app.get("/api/config")
    async def configuration():
        return app.state.service.configuration()

    @app.patch("/api/config")
    async def save_configuration(body: ConfigUpdate):
        return app.state.service.save_configuration(body)

    @app.post("/api/config/validate")
    async def validate_configuration():
        return await app.state.service.validate_configuration()

    @app.get("/api/sessions")
    async def sessions():
        return {"sessions": app.state.service.sessions()}

    @app.post("/api/sessions", status_code=201)
    async def create_session():
        return app.state.service.create_session()

    @app.get("/api/sessions/{session_id}")
    async def session(session_id: str):
        return app.state.service.session(session_id)

    @app.post("/api/sessions/{session_id}/messages", status_code=202)
    async def message(session_id: str, body: MessageInput):
        return await app.state.service.start(
            session_id, body.prompt, body.clientRequestId, read_only=body.readOnly
        )

    @app.get("/api/runs/{run_id}")
    async def run(run_id: str):
        service = app.state.service
        return service.run_view(service.run_record(run_id))

    @app.post("/api/runs/{run_id}/stop")
    async def stop(run_id: str):
        return app.state.service.stop(run_id)

    @app.post("/api/runs/{run_id}/resume", status_code=202)
    async def resume(run_id: str):
        return await app.state.service.resume(run_id)

    @app.get("/api/artifacts")
    async def artifacts():
        return {"artifacts": list(app.state.service.store.snapshot()["artifacts"].values())}

    @app.get("/api/artifacts/{artifact_id}")
    async def artifact(artifact_id: str, version: int | None = None):
        saved = app.state.service.store.snapshot()["artifacts"].get(artifact_id)
        if saved is None:
            raise AppError("NOT_FOUND", "没有这个产物。")
        if version is None:
            return saved
        chosen = next((item for item in saved["versions"] if item["version"] == version), None)
        if chosen is None:
            raise AppError("NOT_FOUND", "没有这个产物版本。")
        return {"id": saved["id"], "projectId": saved["projectId"], "kind": saved["kind"], **chosen}

    @app.get("/api/events")
    async def events(sessionId: str, request: Request):
        service = app.state.service
        service.session(sessionId)

        async def stream():
            queue = service.subscribe(sessionId)

            def snapshot():
                data = json.dumps(service.session(sessionId), ensure_ascii=False, separators=(",", ":"))
                return f"event: snapshot\ndata: {data}\n\n"

            try:
                yield snapshot()
                while not await request.is_disconnected():
                    try:
                        async with asyncio.timeout(15):
                            event = await queue.get()
                        if event["type"] == "snapshot":
                            yield snapshot()
                        else:
                            data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                            yield f"event: assistant.delta\ndata: {data}\n\n"
                    except TimeoutError:
                        yield ": heartbeat\n\n"
            finally:
                service.subscribers.pop(queue, None)

        return StreamingResponse(
            stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
        )

    @app.get("/{asset:path}")
    async def asset(asset: str):
        name = asset or "index.html"
        if name not in {"index.html", "app.js", "styles.css", "mark.svg"} or not (root / name).is_file():
            raise AppError("NOT_FOUND", "页面不存在。")
        return FileResponse(root / name)

    return app
