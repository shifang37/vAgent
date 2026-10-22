"""M6 audio domain: the ``kind=audio`` media branch and append-only audio assets.

E1 froze the record shape so schema 7 can index audio files; E2 writes the first
real records (deterministic local TTS or a verified provider response) and opens
the read path. Files are still owned by :class:`MediaFiles`; nothing here opens a
path, so a record and its bytes are committed by the audio service in one step.
"""

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field, model_validator

from vagent.contracts import (
    Contract,
    Fingerprint,
    Identifier,
    JsonTuple,
    PositiveSeconds,
    UtcTimestamp,
)
from vagent.errors import AppError
from vagent.video.contracts import MediaId

AUDIO_MEDIA_CONTRACT_VERSION = 3
AUDIO_VALIDATION_VERSION = "audio-pcm-v1"
AUDIO_EXTENSIONS = {"audio/wav": "wav", "audio/mp4": "m4a"}
AUDIO_VERSION_LIMIT = 64
AUDIO_KIND_LIMITS = {"voice": 40, "bgm": 8, "sfx": 16}
AUDIO_MAX_CUE_SECONDS = 60

# M6 E2 freezes the TTS surface: one request per dialogue line, provider pricing
# verified from the official CosyVoice page (2 CNY per 10,000 characters).
TTS_PROVIDERS_VERSION = "tts-providers-v1"
TTS_EPISODE_CHARACTER_LIMIT = 2000
TTS_REQUEST_CHARACTER_LIMIT = 500
TTS_EPISODE_REQUEST_LIMIT = 40
TTS_PRICE_PER_THOUSAND_CHARACTERS = Decimal("0.20")
TTS_EPISODE_COST_LIMIT = Decimal("0.40")
TTS_REQUEST_COST_LIMIT = Decimal("0.10")
TTS_COST_QUANTUM = Decimal("0.0001")
NARRATOR_SPEAKER = "画外"

MESSAGES = {
    "AUDIO_ASSET_MISSING": "引用的音频资产或媒体文件缺失。",
    "AUDIO_LICENSE_MISSING": "导入音频缺少许可与来源。",
    "AUDIO_FORMAT_UNSUPPORTED": "音频格式不受支持，本轮只接受 48 kHz PCM WAV。",
    "TTS_TEXT_LIMIT": "单条台词超过 500 字符或本集累计超过 2000 字符。",
    "TTS_BUDGET_LIMIT": "本集 TTS 请求次数或金额额度不足。",
    "TTS_PROVIDER_UNKNOWN": "供应商、模型或音色未在 tts-providers-v1 中核实。",
    "VOICE_CONFLICT": "同一集同一角色已绑定另一个音色。",
}


def tts_cost(characters: int, price_per_thousand: Decimal | None = None) -> Decimal | None:
    """Exact per-request cost, quantized to 0.0001 CNY; ``None`` when unpriced."""
    if price_per_thousand is None:
        return None
    return (Decimal(characters) / Decimal(1000) * price_per_thousand).quantize(TTS_COST_QUANTUM)


def speaker_of(source: "ProviderAudioSource") -> str:
    return source.speaker or NARRATOR_SPEAKER


class AudioMedia(Contract):
    """A local audio file. It never fakes a generation Job or reuses a video asset."""

    contract_version: Literal[3] = AUDIO_MEDIA_CONTRACT_VERSION
    kind: Literal["audio"] = "audio"
    id: MediaId
    project_id: Identifier
    relative_path: str = Field(max_length=150)
    mime_type: Literal["audio/wav", "audio/mp4"]
    size_bytes: int = Field(gt=0)
    sha256: Fingerprint
    duration_seconds: PositiveSeconds
    sample_rate: int = Field(gt=0)
    channels: int = Field(gt=0)
    validation_version: Literal["audio-pcm-v1"] = AUDIO_VALIDATION_VERSION
    created_at: UtcTimestamp

    @model_validator(mode="after")
    def controlled_path(self):
        extension = AUDIO_EXTENSIONS[self.mime_type]
        if self.relative_path != f"media/{self.project_id}/{self.id}.{extension}":
            raise ValueError("Audio media paths are determined exclusively by server IDs")
        return self


def audio_media_path(project_id: str, media_id: str, mime_type: str) -> str:
    return f"media/{project_id}/{media_id}.{AUDIO_EXTENSIONS[mime_type]}"


def is_audio_media(raw: dict | None) -> bool:
    return raw is not None and raw.get("kind") == "audio"


class ProviderAudioSource(Contract):
    """A generated voice line; the free text is the exact TTS input (never re-read)."""

    source: Literal["provider"] = "provider"
    provider: str = Field(min_length=1, max_length=40)
    model: str = Field(min_length=1, max_length=80)
    voice_id: str = Field(min_length=1, max_length=80)
    text: str = Field(min_length=1, max_length=TTS_REQUEST_CHARACTER_LIMIT)
    speed: float = Field(default=1.0, ge=0.5, le=2.0, allow_inf_nan=False)
    character_id: Identifier | None = None
    # E2: the episode and dialogue role freeze TTS accounting and voice binding.
    episode_id: Identifier | None = None
    speaker: str | None = Field(default=None, min_length=1, max_length=40)


class AudioLineRef(Contract):
    """One frozen dialogue line: scene plus paragraph, optionally disambiguated by text."""

    scene_id: Identifier
    paragraph: int = Field(ge=0, le=100)
    text: str | None = Field(default=None, min_length=1, max_length=TTS_REQUEST_CHARACTER_LIMIT)


class AudioVoice(Contract):
    """A speaker-to-voice mapping; the provider/model comes from the mode's table."""

    speaker: str = Field(min_length=1, max_length=40)
    voice_id: str = Field(min_length=1, max_length=80)
    speed: float = Field(default=1.0, ge=0.5, le=2.0, allow_inf_nan=False)


class AudioGenerateArgs(Contract):
    episode_id: Identifier
    lines: JsonTuple[AudioLineRef] | None = Field(default=None, max_length=256)
    voices: JsonTuple[AudioVoice] = Field(min_length=1, max_length=20)
    confirm: bool = False

    @model_validator(mode="after")
    def unique_targets(self):
        if len({voice.speaker for voice in self.voices}) != len(self.voices):
            raise ValueError("Each speaker may appear only once in voices")
        if self.lines is not None:
            keys = [(line.scene_id, line.paragraph) for line in self.lines]
            if len(set(keys)) != len(keys):
                raise ValueError("A dialogue line may appear only once in lines")
        return self


class ImportedAudioSource(Contract):
    """A user-imported CC0/public-domain track; licence and source are mandatory."""

    source: Literal["import"] = "import"
    title: str = Field(min_length=1, max_length=120)
    source_url: str = Field(min_length=1, max_length=500)
    license: str = Field(min_length=1, max_length=120)

    @model_validator(mode="after")
    def provenance_is_a_url(self):
        if not self.source_url.lower().startswith(("https://", "http://")):
            raise ValueError("Imported audio must cite an http(s) source URL")
        return self


AudioSource = Annotated[ProviderAudioSource | ImportedAudioSource, Field(discriminator="source")]


class AudioAssetVersion(Contract):
    version: int = Field(ge=1)
    kind: Literal["voice", "bgm", "sfx"]
    media_id: MediaId
    source: AudioSource
    duration_seconds: PositiveSeconds
    sample_rate: int = Field(gt=0)
    channels: int = Field(gt=0)
    sha256: Fingerprint
    size_bytes: int = Field(gt=0)
    created_at: UtcTimestamp


class AudioAsset(Contract):
    """An append-only audio asset: E1 freezes the shape, E2 writes the bytes."""

    id: Identifier
    project_id: Identifier
    kind: Literal["voice", "bgm", "sfx"]
    current_version: int = Field(ge=1)
    versions: JsonTuple[AudioAssetVersion] = Field(min_length=1, max_length=AUDIO_VERSION_LIMIT)
    revision: int = Field(ge=0)
    created_at: UtcTimestamp

    @model_validator(mode="after")
    def append_only_history(self):
        if [item.version for item in self.versions] != list(range(1, len(self.versions) + 1)):
            raise ValueError("Audio versions must be the continuous 1..N sequence")
        if self.current_version != len(self.versions):
            raise ValueError("currentVersion must identify the latest audio version")
        if any(item.kind != self.kind for item in self.versions):
            raise ValueError("Every audio version must keep the asset kind")
        return self


def _voice_bucket(state: dict, asset: "AudioAsset", source: "ProviderAudioSource") -> str:
    """Per-episode voice assets count against that episode; others stay project-wide."""
    if asset.kind != "voice" or source.episode_id is None:
        return "project"
    raw = state["episodes"].get(source.episode_id)
    if raw is None or raw["projectId"] != asset.project_id:
        raise AppError("AUDIO_CONFLICT", "配音资产引用了不存在或跨项目的分集。")
    return source.episode_id


def validate_audio_changes(previous: dict[str, dict], state: dict) -> None:
    """Every Store commit re-checks append-only audio history and its media record."""
    assets = state.get("audio", {})
    if previous.keys() - assets.keys():
        raise AppError("AUDIO_IMMUTABLE", "已保存的音频资产不能删除。")
    counts: dict[tuple[str, str], int] = {}
    for key, raw in assets.items():
        asset = AudioAsset.model_validate(raw)
        if asset.id != key or asset.project_id not in state["projects"]:
            raise AppError("AUDIO_CONFLICT", "音频资产 ID 或所属项目不匹配。")
        bucket = "project"
        for version in asset.versions:
            media_raw = state["media"].get(version.media_id)
            if media_raw is None or media_raw.get("kind") != "audio":
                raise AppError("AUDIO_ASSET_MISSING", "音频版本缺少对应的本地音频媒体。")
            media = AudioMedia.model_validate(media_raw)
            if (
                media.project_id != asset.project_id
                or media.sha256 != version.sha256
                or media.size_bytes != version.size_bytes
                or media.duration_seconds != version.duration_seconds
                or media.sample_rate != version.sample_rate
                or media.channels != version.channels
            ):
                raise AppError("AUDIO_ASSET_MISSING", "音频版本与本地音频媒体的测量不一致。")
            if version.duration_seconds > AUDIO_MAX_CUE_SECONDS:
                raise AppError("AUDIO_CONFLICT", "单条音频超过 60 秒的冻结上限。")
            source = version.source
            if bucket == "project" and isinstance(source, ProviderAudioSource):
                bucket = _voice_bucket(state, asset, source)
        counts[(asset.kind, bucket)] = counts.get((asset.kind, bucket), 0) + 1
        if counts[(asset.kind, bucket)] > AUDIO_KIND_LIMITS[asset.kind]:
            raise AppError("AUDIO_CONFLICT", "音频资产数量超过冻结上限。")
        old_raw = previous.get(key)
        if old_raw is None:
            if asset.revision != 0 or len(asset.versions) != 1:
                raise AppError("AUDIO_CONFLICT", "新音频资产必须先保存第 1 版。")
            continue
        old = AudioAsset.model_validate(old_raw)
        if asset == old:
            continue
        if (
            asset.project_id != old.project_id
            or asset.kind != old.kind
            or asset.created_at != old.created_at
            or asset.revision != old.revision + 1
            or len(asset.versions) != len(old.versions) + 1
            or asset.versions[: len(old.versions)] != old.versions
        ):
            raise AppError("AUDIO_IMMUTABLE", "音频版本只增不改，旧版本必须保留。")
