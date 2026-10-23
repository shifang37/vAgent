"""Loopback-only Web API and bundled UI, using the shared application service."""

import asyncio
import json
import re
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from vagent import __version__
from vagent.application import ApplicationService
from vagent.config import ConfigUpdate, assert_id
from vagent.contracts import Identifier
from vagent.errors import AppError, public_error
from vagent.video.contracts import IMAGE_MAX_BYTES
from vagent.video.media import DownloadRetry, file_io
from vagent.video.media_response import media_response
from vagent.video.storyboards import ASSET_EXTENSIONS

# One JSON document, or one bounded image/audio upload plus multipart framing.
MAX_JSON_BYTES = 131072
MAX_UPLOAD_BYTES = IMAGE_MAX_BYTES + 65536
MAX_AUDIO_UPLOAD_BYTES = 25 * 1024 * 1024 + 65536
UPLOAD_PATHS = {"/api/assets", "/api/audio/import"}


class MessageInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    prompt: str = Field(min_length=1, max_length=20000)
    clientRequestId: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    readOnly: bool = False


class DownloadRetryInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    clientRequestId: Identifier
    expectedRevision: int = Field(ge=0)


class ShotGenerationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    clientRequestId: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    storyboardId: Identifier
    storyboardVersion: int = Field(ge=1)
    shotIds: list[Identifier] = Field(min_length=1, max_length=64)
    confirm: bool = True


class ShotRedoInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    clientRequestId: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    storyboardId: Identifier
    storyboardVersion: int = Field(ge=1)
    expectedRevision: int = Field(ge=0)


class FilmAssembleInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    clientRequestId: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    storyboardId: Identifier
    expectedRevision: int = Field(ge=0)


class SubtitleCueInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    startMs: int = Field(ge=0, le=90_000)
    endMs: int = Field(gt=0, le=90_000)
    text: str = Field(min_length=1, max_length=48)
    speaker: str | None = Field(default=None, min_length=1, max_length=40)
    shotId: Identifier | None = None
    sceneId: Identifier | None = None


class SubtitleSaveInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    clientRequestId: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    scope: Literal["storyboard", "episode"]
    storyboardId: Identifier | None = None
    episodeId: Identifier | None = None
    trackId: Identifier | None = None
    expectedRevision: int | None = Field(default=None, ge=0)
    cues: list[SubtitleCueInput] | None = Field(default=None, max_length=200)


class SubtitleExportInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    clientRequestId: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    trackId: Identifier
    mode: Literal["soft", "burn", "both"]
    profile: Literal["720p-h264-aac-v1", "1080p-h264-aac-v1"] = "720p-h264-aac-v1"
    trackVersion: int | None = Field(default=None, ge=1)


class TransitionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    afterShotId: Identifier
    type: Literal["cut", "fade", "crossfade"] = "cut"
    durationMs: int = Field(default=0, ge=0, le=1000)


class TitleCardInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    text: str = Field(min_length=1, max_length=120)
    startMs: int = Field(ge=0)
    durationMs: int = Field(gt=0)
    styleProfile: Literal["title-zh-v1"] = "title-zh-v1"


class SeasonSaveInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    clientRequestId: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    seasonId: Identifier | None = None
    title: str = Field(min_length=1, max_length=120)
    episodeIds: list[Identifier] = Field(min_length=1, max_length=4)
    expectedRevision: int | None = Field(default=None, ge=0)


class SeasonGenerateInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    clientRequestId: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    seasonId: Identifier
    episodeIds: list[Identifier] | None = Field(default=None, min_length=1, max_length=3)
    resumeBatchId: Identifier | None = None
    confirm: bool = True

    @model_validator(mode="after")
    def exclusive_targets(self):
        if self.episodeIds is not None and self.resumeBatchId is not None:
            raise ValueError("episodeIds and resumeBatchId are mutually exclusive")
        return self


class TimelineClipInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    shotId: Identifier
    shotVersionId: Identifier
    storyboardId: Identifier
    sceneId: Identifier | None = None
    inMs: int = Field(ge=0, le=90_000)
    outMs: int = Field(gt=0, le=90_000)

    @model_validator(mode="after")
    def ordered_window(self):
        if self.outMs <= self.inMs:
            raise ValueError("outMs must be later than inMs")
        return self


class TimelineSubtitleInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    trackId: Identifier
    trackVersion: int = Field(ge=1)


class TimelineAudioInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    audioId: Identifier
    audioVersion: int = Field(ge=1)
    kind: Literal["voice", "bgm", "sfx"]
    gainDb: float = Field(default=0.0, ge=-60, le=12, allow_inf_nan=False)
    startMs: int = Field(default=0, ge=0, le=90_000)


class TimelineSaveInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    clientRequestId: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    episodeId: Identifier
    timelineId: Identifier | None = None
    clips: list[TimelineClipInput] = Field(min_length=1, max_length=20)
    transitions: list[TransitionInput] = Field(default_factory=list, max_length=19)
    subtitle: TimelineSubtitleInput | None = None
    audio: list[TimelineAudioInput] = Field(default_factory=list, max_length=64)
    titleCards: list[TitleCardInput] = Field(default_factory=list, max_length=4)
    expectedRevision: int | None = Field(default=None, ge=0)


class FilmRenderInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    clientRequestId: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    scope: Literal["storyboard", "episode", "timeline"]
    storyboardId: Identifier | None = None
    episodeId: Identifier | None = None
    timelineId: Identifier | None = None
    timelineVersion: int | None = Field(default=None, ge=1)
    trackId: Identifier | None = None
    trackVersion: int | None = Field(default=None, ge=1)
    profile: Literal["720p-h264-aac-v1", "1080p-h264-aac-v1"] = "720p-h264-aac-v1"
    subtitleMode: Literal["soft", "burn", "both"] = "soft"
    transitions: list[TransitionInput] = Field(default_factory=list, max_length=63)
    titleCards: list[TitleCardInput] = Field(default_factory=list, max_length=4)
    expectedRevision: int | None = Field(default=None, ge=0)
    confirm: bool = True


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
            content_type = request.headers.get("content-type", "").split(";")[0].strip()
            upload = request.method == "POST" and request.url.path in UPLOAD_PATHS
            expected = "multipart/form-data" if upload else "application/json"
            if content_type != expected:
                return JSONResponse({"error": {"code": "CONTENT_TYPE", "message": "需要 JSON 请求。"}}, 415)
            limit = (
                MAX_UPLOAD_BYTES
                if request.url.path == "/api/assets"
                else MAX_AUDIO_UPLOAD_BYTES
                if upload
                else MAX_JSON_BYTES
            )
            size = 0
            chunks = []
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:
                    return JSONResponse({"error": {"code": "BODY_LIMIT", "message": "请求内容过大。"}}, 413)
                chunks.append(chunk)
            request._body = b"".join(chunks)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
            "connect-src 'self'; media-src 'self'; object-src 'none'; base-uri 'none'; "
            "frame-ancestors 'none'; form-action 'self'"
        )
        return response

    @app.exception_handler(AppError)
    async def app_error(_request, error):
        status = (
            404
            if error.code
            in {
                "NOT_FOUND",
                "JOB_NOT_FOUND",
                "MEDIA_NOT_FOUND",
                "ASSET_NOT_FOUND",
                "SEASON_NOT_FOUND",
                "TIMELINE_NOT_FOUND",
            }
            else 410
            if error.code in {"JOB_MEDIA_UNAVAILABLE", "ASSET_UNAVAILABLE"}
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
                "JOB_QUERY_NOT_PAUSED",
                "JOB_REVISION_CONFLICT",
                "JOB_DOWNLOAD_RETRY_UNAVAILABLE",
                "OPERATION_CONFLICT",
                "VERSION_CONFLICT",
                "SEASON_VERSION_CONFLICT",
                "SEASON_EPISODE_CONFLICT",
                "BATCH_CONFLICT",
                "BATCH_INTERRUPTED",
                "TIMELINE_VERSION_CONFLICT",
            }
            else 400
        )
        response = JSONResponse({"error": {"code": error.code, "message": str(error)}}, status)
        if _request.method == "HEAD":
            response.body = b""
        return response

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
        return await app.state.service.save_configuration(body)

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
        return await file_io(app.state.service.session, session_id)

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

    @app.get("/api/jobs")
    async def jobs(sessionId: str | None = None):
        return {"jobs": await file_io(app.state.service.jobs, sessionId)}

    @app.get("/api/jobs/{job_id}")
    async def job(job_id: str):
        return await file_io(app.state.service.job, job_id)

    @app.post("/api/jobs/{job_id}/retry-query")
    async def retry_query(job_id: str):
        return app.state.service.retry_query(job_id)

    @app.post("/api/jobs/{job_id}/retry-download", status_code=202)
    async def retry_download(job_id: str, body: DownloadRetryInput):
        return await file_io(
            app.state.service.retry_download, job_id, DownloadRetry.model_validate(body.model_dump())
        )

    @app.get("/api/usage")
    async def usage(projectId: str | None = None):
        return app.state.service.usage(projectId)

    @app.get("/api/assets")
    async def assets(projectId: str | None = None):
        return {"assets": app.state.service.assets(projectId)}

    @app.post("/api/assets", status_code=201)
    async def import_asset(request: Request):
        form = await request.form(max_files=1, max_fields=4)
        upload, project_id = form.get("file"), form.get("projectId")
        if isinstance(upload, str) or upload is None or not isinstance(project_id, str):
            raise AppError("INVALID_REQUEST", "需要 projectId 与一个图片文件。")
        assert_id(project_id)
        data = await upload.read()
        return await file_io(app.state.service.import_asset, project_id, data, upload.filename)

    @app.get("/api/assets/{asset_id}")
    async def asset_detail(asset_id: str, projectId: str | None = None):
        return app.state.service.asset(asset_id, projectId)

    @app.api_route("/api/assets/{asset_id}/content", methods=["GET", "HEAD"])
    async def asset_content(asset_id: str, request: Request, download: str | None = None):
        asset, lease = await file_io(
            app.state.service.video_jobs.assets.open_content,
            asset_id,
            cancel_cleanup=lambda result: result[1].close(),
        )
        return media_response(
            asset,
            lease,
            method=request.method,
            request_headers=request.headers,
            download=download == "1",
            content_type=asset.mime_type,
            extension=ASSET_EXTENSIONS[asset.mime_type],
        )

    @app.get("/api/storyboards")
    async def storyboards(projectId: str):
        return {"storyboards": app.state.service.storyboards(projectId)}

    @app.get("/api/episodes")
    async def episodes(projectId: str):
        return await file_io(app.state.service.episodes, projectId)

    @app.get("/api/episodes/{episode_id}")
    async def episode_detail(episode_id: str, projectId: str):
        return await file_io(app.state.service.episode, projectId, episode_id)

    @app.get("/api/seasons")
    async def seasons(projectId: str):
        return await file_io(app.state.service.seasons, projectId)

    @app.get("/api/seasons/{season_id}")
    async def season_detail(season_id: str, projectId: str, batchId: str | None = None):
        return await file_io(app.state.service.season, projectId, season_id, batchId)

    @app.post("/api/sessions/{session_id}/seasons/save", status_code=201)
    async def seasons_save(session_id: str, body: SeasonSaveInput):
        return await file_io(app.state.service.save_season, session_id, body.model_dump(exclude_none=True))

    @app.get("/api/seasons/{season_id}/estimate")
    async def season_estimate(
        season_id: str, projectId: str, episodeIds: str | None = None, resumeBatchId: str | None = None
    ):
        ids = [item for item in (episodeIds or "").split(",") if item]
        return await file_io(
            app.state.service.preview_season,
            projectId,
            season_id,
            episode_ids=ids or None,
            resume_batch_id=resumeBatchId,
        )

    @app.post("/api/sessions/{session_id}/seasons/generate", status_code=202)
    async def seasons_generate(session_id: str, body: SeasonGenerateInput):
        return await file_io(
            app.state.service.generate_season, session_id, body.model_dump(exclude_none=True)
        )

    @app.get("/api/episodes/{episode_id}/timeline")
    async def episode_timeline(episode_id: str, projectId: str):
        return await file_io(app.state.service.timeline_editor, projectId, episode_id)

    @app.get("/api/timelines/{timeline_id}")
    async def timeline_detail(timeline_id: str, projectId: str, version: int | None = None):
        return await file_io(app.state.service.timeline, projectId, timeline_id=timeline_id, version=version)

    @app.post("/api/sessions/{session_id}/timelines/save", status_code=201)
    async def timelines_save(session_id: str, body: TimelineSaveInput):
        return await file_io(app.state.service.save_timeline, session_id, body.model_dump(exclude_none=True))

    @app.get("/api/audio")
    async def imported_audio(projectId: str):
        return await file_io(app.state.service.imported_audio, projectId)

    @app.post("/api/audio/import", status_code=201)
    async def import_audio(request: Request):
        form = await request.form(max_files=1, max_fields=8)
        upload = form.get("file")
        project_id = form.get("projectId")
        if isinstance(upload, str) or upload is None or not isinstance(project_id, str):
            raise AppError("INVALID_REQUEST", "需要 projectId 与一个音频文件。")
        assert_id(project_id)
        request_data = {
            "clientRequestId": form.get("clientRequestId"),
            "kind": form.get("kind"),
            "title": form.get("title"),
            "sourceUrl": form.get("sourceUrl"),
            "license": form.get("license"),
        }
        if not all(isinstance(value, str) and value for value in request_data.values()):
            raise AppError("INVALID_REQUEST", "需要 clientRequestId、kind、title、sourceUrl 与 license。")
        if request_data["kind"] not in {"bgm", "sfx"}:
            raise AppError("AUDIO_FORMAT_UNSUPPORTED", "kind 必须是 bgm 或 sfx。")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", request_data["clientRequestId"]):
            raise AppError("INVALID_REQUEST", "clientRequestId 格式不正确。")
        data = await upload.read()
        # The import service is async (it may probe an M4A container with FFmpeg), so
        # it must run on the event loop rather than in the blocking file helper.
        return await app.state.service.import_audio(project_id, request_data, data, upload.filename)

    @app.get("/api/storyboards/{storyboard_id}")
    async def storyboard_detail(storyboard_id: str, projectId: str):
        return app.state.service.storyboard(projectId, storyboard_id)

    @app.get("/api/storyboards/{storyboard_id}/estimate")
    async def storyboard_estimate(storyboard_id: str, projectId: str, storyboardVersion: int, shotIds: str):
        shot_ids = [item for item in shotIds.split(",") if item]
        if not 1 <= len(shot_ids) <= 64 or len(set(shot_ids)) != len(shot_ids):
            raise AppError("INVALID_REQUEST", "shotIds 必须是 1～64 个不重复的镜头 ID。")
        return app.state.service.preview_shots(
            projectId,
            storyboard_id=storyboard_id,
            storyboard_version=storyboardVersion,
            shot_ids=shot_ids,
        )

    @app.post("/api/sessions/{session_id}/shots/generate", status_code=202)
    async def shots_generate(session_id: str, body: ShotGenerationInput):
        return await file_io(app.state.service.generate_shots, session_id, body.model_dump())

    @app.post("/api/sessions/{session_id}/shots/{shot_id}/redo", status_code=202)
    async def shots_redo(session_id: str, shot_id: str, body: ShotRedoInput):
        return await file_io(app.state.service.redo_shot, session_id, shot_id, body.model_dump())

    @app.get("/api/films/{storyboard_id}")
    async def film_detail(storyboard_id: str, projectId: str):
        return await file_io(app.state.service.film, projectId, storyboard_id)

    @app.post("/api/sessions/{session_id}/films/assemble", status_code=202)
    async def films_assemble(session_id: str, body: FilmAssembleInput):
        return await app.state.service.assemble_film(session_id, body.model_dump())

    @app.post("/api/sessions/{session_id}/films/render", status_code=202)
    async def films_render(session_id: str, body: FilmRenderInput):
        return await app.state.service.film_render(session_id, body.model_dump(exclude_none=True))

    @app.get("/api/renders")
    async def renders(
        projectId: str,
        renderId: str | None = None,
        storyboardId: str | None = None,
        episodeId: str | None = None,
        filmId: str | None = None,
        timelineId: str | None = None,
    ):
        return await file_io(
            app.state.service.render_read,
            projectId,
            render_id=renderId,
            storyboard_id=storyboardId,
            episode_id=episodeId,
            film_id=filmId,
            timeline_id=timelineId,
        )

    @app.get("/api/renders/{render_id}")
    async def render_detail(render_id: str, projectId: str):
        return await file_io(app.state.service.render_read, projectId, render_id=render_id)

    @app.get("/api/films/{film_id}/versions/{version}/subtitles.srt")
    async def film_subtitle_srt(film_id: str, version: int):
        text, filename = await file_io(app.state.service.render_sidecar, film_id, version, kind="srt")
        return Response(
            content=text.encode("utf-8"),
            media_type="application/x-subrip; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/api/films/{film_id}/versions/{version}/subtitles.ass")
    async def film_subtitle_ass(film_id: str, version: int):
        text, filename = await file_io(app.state.service.render_sidecar, film_id, version, kind="ass")
        return Response(
            content=text.encode("utf-8"),
            media_type="text/x-ssa; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/api/subtitles")
    async def subtitle(
        projectId: str,
        storyboardId: str | None = None,
        episodeId: str | None = None,
        trackId: str | None = None,
    ):
        return await file_io(
            app.state.service.subtitle,
            projectId,
            storyboard_id=storyboardId,
            episode_id=episodeId,
            track_id=trackId,
        )

    @app.post("/api/sessions/{session_id}/subtitles/save", status_code=201)
    async def subtitles_save(session_id: str, body: SubtitleSaveInput):
        return await file_io(app.state.service.save_subtitles, session_id, body.model_dump(exclude_none=True))

    @app.post("/api/sessions/{session_id}/subtitles/export", status_code=202)
    async def subtitles_export(session_id: str, body: SubtitleExportInput):
        return await app.state.service.export_subtitles(session_id, body.model_dump(exclude_none=True))

    @app.get("/api/subtitles/{track_id}/{version}/srt")
    async def subtitle_srt(track_id: str, version: int):
        text, filename = await file_io(app.state.service.subtitle_sidecar, track_id, version, kind="srt")
        return Response(
            content=text.encode("utf-8"),
            media_type="application/x-subrip; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/api/subtitles/{track_id}/{version}/ass")
    async def subtitle_ass(track_id: str, version: int):
        text, filename = await file_io(app.state.service.subtitle_sidecar, track_id, version, kind="ass")
        return Response(
            content=text.encode("utf-8"),
            media_type="text/x-ssa; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/api/media/{media_id}")
    async def media(media_id: str):
        return await file_io(app.state.service.media.view, media_id)

    @app.api_route("/api/media/{media_id}/content", methods=["GET", "HEAD"])
    async def content(media_id: str, request: Request, download: str | None = None):
        asset, lease = await file_io(
            app.state.service.media.open_content, media_id, cancel_cleanup=lambda result: result[1].close()
        )
        if getattr(asset, "kind", None) == "audio":
            from vagent.video.audio import AUDIO_EXTENSIONS

            return media_response(
                asset,
                lease,
                method=request.method,
                request_headers=request.headers,
                download=download == "1",
                content_type=asset.mime_type,
                extension=AUDIO_EXTENSIONS[asset.mime_type],
            )
        return media_response(
            asset, lease, method=request.method, request_headers=request.headers, download=download == "1"
        )

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
        await file_io(service.session, sessionId)

        async def stream():
            queue = service.subscribe(sessionId)

            async def snapshot():
                data = json.dumps(
                    await file_io(service.session, sessionId), ensure_ascii=False, separators=(",", ":")
                )
                return f"event: snapshot\ndata: {data}\n\n"

            try:
                yield await snapshot()
                while not await request.is_disconnected():
                    try:
                        async with asyncio.timeout(15):
                            event = await queue.get()
                        if event["type"] == "snapshot":
                            yield await snapshot()
                        elif event["type"] in {
                            "assistant.delta",
                            "job.updated",
                            "run.waiting",
                            "run.resumed",
                            "run.resume_blocked",
                            "run.completed",
                        }:
                            if event["type"].startswith("run."):
                                # Capture current state at delivery, not when queued:
                                # a prior overflow snapshot may already be newer.
                                event = {**event, "snapshot": await file_io(service.session, sessionId)}
                            data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                            yield f"event: {event['type']}\ndata: {data}\n\n"
                        else:
                            yield await snapshot()
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
        if (
            name
            not in {
                "index.html",
                "app.js",
                "job-state.js",
                "storyboard-state.js",
                "film-state.js",
                "subtitle-state.js",
                "episode-state.js",
                "season-state.js",
                "render-state.js",
                "timeline-state.js",
                "styles.css",
                "mark.svg",
            }
            or not (root / name).is_file()
        ):
            raise AppError("NOT_FOUND", "页面不存在。")
        return FileResponse(root / name)

    return app
