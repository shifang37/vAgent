"""M7 E3 client-side BGM/SFX import: a controlled upload, never a model tool.

Timeline audio needs real music and effects, but exposing local paths to the model
would be unsafe. The frozen decision is therefore a client action (Web form or CLI
path): the server validates the bytes, requires ``license`` and ``sourceUrl``, dedupes
by SHA-256, writes an :class:`AudioMedia` + :class:`AudioAsset` and returns only the
``audioId`` to callers. WAV is validated as 48 kHz PCM; M4A is probed with the bundled
FFmpeg (the package ships no ffprobe). Nothing here contacts a provider.
"""

import hashlib
from dataclasses import dataclass
from typing import Literal
from uuid import uuid4

from vagent.errors import AppError
from vagent.storage import now
from vagent.video.assembler import bundled_engine
from vagent.video.audio import (
    AUDIO_MAX_CUE_SECONDS,
    AudioAsset,
    AudioAssetVersion,
    AudioMedia,
    ImportedAudioSource,
    audio_media_path,
)
from vagent.video.subtitle_service import parse_streams
from vagent.video.tts import inspect_wav

AUDIO_IMPORT_MAX_BYTES = 25 * 1024 * 1024
AUDIO_IMPORT_KINDS = ("bgm", "sfx")
MESSAGES = {
    "AUDIO_LICENSE_MISSING": "导入音频必须填写 license 与 sourceUrl。",
    "AUDIO_FORMAT_UNSUPPORTED": "音频格式不受支持：只接受 WAV 或 MP4/M4A 音频。",
    "AUDIO_TOO_LARGE": "导入音频超过 25 MB 上限。",
    "AUDIO_TOO_LONG": "导入音频超过 60 秒上限。",
    "AUDIO_CONFLICT": "音频资产或媒体写入冲突，未导入。",
}


class ImportError_(AppError):
    def __init__(self, code: str, detail: str = ""):
        message = MESSAGES.get(code, "音频导入失败。")
        super().__init__(code, f"{message}{detail}" if detail else message)


@dataclass(frozen=True)
class AudioFacts:
    mime_type: str
    extension: str
    duration_seconds: float
    sample_rate: int
    channels: int


def _sniff(data: bytes) -> tuple[str, str]:
    """Return ``(mime, extension)`` from the container header; no filename trust."""
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio/wav", "wav"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return "audio/mp4", "m4a"
    raise ImportError_("AUDIO_FORMAT_UNSUPPORTED")


async def _probe_m4a(path) -> AudioFacts:
    import asyncio

    engine = bundled_engine()
    try:
        process = await asyncio.create_subprocess_exec(
            str(engine.path),
            "-hide_banner",
            "-nostdin",
            "-i",
            str(path),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError:
        raise ImportError_("AUDIO_FORMAT_UNSUPPORTED", "（无法读取音频容器）") from None
    _stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
    streams = parse_streams((stderr or b"").decode("utf-8", "replace"))
    if streams.audio is None or streams.duration_seconds is None:
        raise ImportError_("AUDIO_FORMAT_UNSUPPORTED", "（没有可用的音频轨）")
    if streams.duration_seconds > AUDIO_MAX_CUE_SECONDS:
        raise ImportError_("AUDIO_TOO_LONG")
    return AudioFacts(
        mime_type="audio/mp4",
        extension="m4a",
        duration_seconds=round(streams.duration_seconds, 6),
        sample_rate=streams.audio["sample_rate"] or 48000,
        channels=streams.audio["channels"] or 2,
    )


def _probe_wav(data: bytes) -> AudioFacts:
    try:
        facts = inspect_wav(data)
    except Exception:
        raise ImportError_("AUDIO_FORMAT_UNSUPPORTED") from None
    if facts.duration_seconds > AUDIO_MAX_CUE_SECONDS:
        raise ImportError_("AUDIO_TOO_LONG")
    return AudioFacts(
        mime_type="audio/wav",
        extension="wav",
        duration_seconds=facts.duration_seconds,
        sample_rate=facts.sample_rate,
        channels=facts.channels,
    )


def _existing(state: dict, project_id: str, kind: str, sha256: str):
    for raw in state.get("audio", {}).values():
        if raw.get("projectId") != project_id or raw.get("kind") != kind:
            continue
        asset = AudioAsset.model_validate(raw)
        for version in asset.versions:
            if version.sha256 == sha256:
                return asset, version
    return None


def audio_asset_view(state: dict, asset: AudioAsset, version: AudioAssetVersion) -> dict:
    source = version.source
    return {
        "audioId": asset.id,
        "audioVersion": version.version,
        "kind": asset.kind,
        "durationSeconds": version.duration_seconds,
        "sampleRate": version.sample_rate,
        "channels": version.channels,
        "sha256": version.sha256,
        "sizeBytes": version.size_bytes,
        "createdAt": version.created_at,
        "source": source.model_dump(mode="json", by_alias=True),
        "mediaUrl": f"/api/media/{version.media_id}/content",
        "mediaAvailable": version.media_id in state["media"],
    }


def imported_assets(state: dict, project_id: str) -> list[dict]:
    """Every imported BGM/SFX of one project, newest first."""
    rows = []
    for raw in state.get("audio", {}).values():
        if raw.get("projectId") != project_id:
            continue
        asset = AudioAsset.model_validate(raw)
        if asset.kind not in AUDIO_IMPORT_KINDS:
            continue
        if not isinstance(asset.versions[-1].source, ImportedAudioSource):
            continue
        rows.append(audio_asset_view(state, asset, asset.versions[-1]))
    rows.sort(key=lambda item: (item["createdAt"], item["audioId"]), reverse=True)
    return rows


async def import_audio(
    jobs,
    *,
    project_id: str,
    data: bytes,
    kind: Literal["bgm", "sfx"],
    title: str,
    source_url: str,
    license: str,
    filename: str | None = None,
) -> dict:
    """Validate, dedupe and commit one client-imported audio asset."""
    if kind not in AUDIO_IMPORT_KINDS:
        raise ImportError_("AUDIO_FORMAT_UNSUPPORTED", "（kind 必须是 bgm 或 sfx）")
    if not title or not source_url or not license:
        raise ImportError_("AUDIO_LICENSE_MISSING")
    try:
        source = ImportedAudioSource(title=title, sourceUrl=source_url, license=license)
    except ValueError:
        raise ImportError_("AUDIO_LICENSE_MISSING", "（sourceUrl 必须是 http(s) 地址）") from None
    if len(data) > AUDIO_IMPORT_MAX_BYTES:
        raise ImportError_("AUDIO_TOO_LARGE")
    if not data:
        raise ImportError_("AUDIO_FORMAT_UNSUPPORTED")
    sha256 = hashlib.sha256(data).hexdigest()
    store = jobs.store
    state = store.snapshot()
    found = _existing(state, project_id, kind, sha256)
    if found is not None:
        asset, version = found
        return {
            "audioId": asset.id,
            "audioVersion": version.version,
            "kind": asset.kind,
            "durationSeconds": version.duration_seconds,
            "replayed": True,
            "asset": audio_asset_view(state, asset, version),
        }
    mime_type, extension = _sniff(data)
    media_id = str(uuid4())
    final_relative = audio_media_path(project_id, media_id, mime_type)
    part_relative = final_relative + ".part"
    files = jobs.media.files
    try:
        with files.open(part_relative, writing=True) as handle:
            handle.write(data)
            handle.flush()
        part_path = store.home / part_relative
        facts = _probe_wav(data) if mime_type == "audio/wav" else await _probe_m4a(part_path)
    except BaseException:
        files.remove_part(part_relative)
        raise
    timestamp = now()
    try:

        def commit(draft: dict) -> dict:
            existing = _existing(draft, project_id, kind, sha256)
            if existing is not None:
                files.remove_part(part_relative)
                asset, version = existing
                return {
                    "audioId": asset.id,
                    "audioVersion": version.version,
                    "kind": asset.kind,
                    "durationSeconds": version.duration_seconds,
                    "replayed": True,
                    "asset": audio_asset_view(draft, asset, version),
                }
            media = AudioMedia(
                id=media_id,
                project_id=project_id,
                relative_path=final_relative,
                mime_type=mime_type,
                size_bytes=len(data),
                sha256=sha256,
                duration_seconds=facts.duration_seconds,
                sample_rate=facts.sample_rate,
                channels=facts.channels,
                created_at=timestamp,
            )
            version = AudioAssetVersion(
                version=1,
                kind=kind,
                media_id=media_id,
                source=source,
                duration_seconds=facts.duration_seconds,
                sample_rate=facts.sample_rate,
                channels=facts.channels,
                sha256=sha256,
                size_bytes=len(data),
                created_at=timestamp,
            )
            asset = AudioAsset(
                id=str(uuid4()),
                project_id=project_id,
                kind=kind,
                current_version=1,
                versions=(version,),
                revision=0,
                created_at=timestamp,
            )
            files.promote(final_relative)
            draft["media"][media.id] = media.model_dump(mode="json", by_alias=True)
            draft["audio"][asset.id] = asset.model_dump(mode="json", by_alias=True)
            return {
                "audioId": asset.id,
                "audioVersion": version.version,
                "kind": asset.kind,
                "durationSeconds": version.duration_seconds,
                "replayed": False,
                "asset": audio_asset_view(draft, asset, version),
            }

        return store.transaction(commit)
    except BaseException:
        files.remove_part(part_relative)
        raise
