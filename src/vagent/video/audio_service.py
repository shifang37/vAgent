"""M6 E2 ``audio_generate``: TTS lines become append-only assets and local media.

The paid call runs outside the Store lock (exactly like film assembly): the
service freezes the episode and usage under the lock, synthesizes every new
line into a ``.part`` file, then promotes the files and commits ``AudioMedia`` +
``AudioAsset`` + the Operation in one transaction. A limit or binding violation
rejects the whole batch before any provider call and never leaves a partial set.
"""

import hashlib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from vagent.errors import AppError
from vagent.storage import now
from vagent.video.audio import (
    NARRATOR_SPEAKER,
    TTS_EPISODE_CHARACTER_LIMIT,
    TTS_EPISODE_COST_LIMIT,
    TTS_EPISODE_REQUEST_LIMIT,
    TTS_REQUEST_CHARACTER_LIMIT,
    TTS_REQUEST_COST_LIMIT,
    AudioAsset,
    AudioAssetVersion,
    AudioGenerateArgs,
    AudioMedia,
    ProviderAudioSource,
    audio_media_path,
    speaker_of,
    tts_cost,
)
from vagent.video.episodes import Episode, Scene
from vagent.video.tts import (
    CosyVoiceAdapter,
    LocalTtsAdapter,
    TtsError,
    TtsRequestV1,
    capability_for,
    inspect_wav,
    limits_snapshot,
    tts_capabilities,
)

TOOL_NAME = "audio_generate"
MESSAGES = {
    "CONFIRMATION_REQUIRED": "配音会调用 TTS 供应商并产生费用，需要用户显式确认。",
    "TTS_TEXT_LIMIT": "单条台词超过 500 字符或本集累计超过 2000 字符，整批未提交。",
    "TTS_BUDGET_LIMIT": "本集 TTS 请求次数或金额额度不足，整批未提交。",
    "TTS_PROVIDER_UNKNOWN": "供应商、模型或音色未在 tts-providers-v1 中核实。",
    "TTS_EMPTY_EPISODE": "该分集没有可配音的台词。",
    "VOICE_CONFLICT": "同一集同一角色已绑定另一个音色，整批未提交。",
    "AUDIO_ASSET_MISSING": "引用的音频资产或媒体文件缺失。",
}


def _fail(code: str, detail: str = "") -> AppError:
    message = MESSAGES.get(code) or code
    return AppError(code, f"{message}{detail}" if detail else message)


def _price_for(provider: str, model: str) -> Decimal | None:
    for table in tts_capabilities("live") + tts_capabilities("mock"):
        if table["provider"] == provider and table["model"] == model:
            value = table.get("pricePerThousandCharacters")
            return Decimal(value) if table.get("priceKnown") and value else None
    return None


def _speaker(line) -> str:
    return line.speaker or NARRATOR_SPEAKER


@dataclass(frozen=True)
class PlannedLine:
    speaker: str
    character_id: str | None
    text: str
    scene_id: str
    paragraph: int
    voice_id: str
    speed: float
    reused_asset_id: str | None = None
    reused_version: int | None = None

    @property
    def reused(self) -> bool:
        return self.reused_asset_id is not None


def _episode(state: dict, episode_id: str, project_id: str) -> Episode:
    raw = state["episodes"].get(episode_id)
    if raw is None or raw["projectId"] != project_id:
        raise AppError("EPISODE_NOT_FOUND", f"分集 {episode_id} 不存在，未保存。")
    return Episode.model_validate(raw)


def _character_for_speaker(state: dict, project_id: str, speaker: str) -> str | None:
    for raw in state.get("characters", {}).values():
        if raw["projectId"] != project_id:
            continue
        from vagent.video.episodes import Character

        character = Character.model_validate(raw)
        latest = character.latest
        if latest.name == speaker or speaker in latest.aliases:
            return character.id
    return None


def episode_lines(state: dict, episode: Episode) -> list[dict]:
    """The frozen dialogue in scene/shot order; no re-reading of the source text."""
    lines: list[dict] = []
    for scene_ref in episode.latest.scenes:
        raw = state["scenes"].get(scene_ref.scene_id)
        if raw is None or raw["episodeId"] != episode.id:
            continue
        scene = Scene.model_validate(raw)
        for line in scene.latest.dialogue:
            lines.append(
                {
                    "sceneId": scene.id,
                    "paragraph": int(line.paragraph),
                    "speaker": _speaker(line),
                    "text": line.text,
                }
            )
    return lines


def episode_voice_assets(
    state: dict, project_id: str, episode_id: str
) -> list[tuple[AudioAsset, AudioAssetVersion]]:
    found: list[tuple[AudioAsset, AudioAssetVersion]] = []
    for raw in state.get("audio", {}).values():
        if raw["projectId"] != project_id:
            continue
        asset = AudioAsset.model_validate(raw)
        if asset.kind != "voice":
            continue
        for version in asset.versions:
            source = version.source
            if isinstance(source, ProviderAudioSource) and source.episode_id == episode_id:
                found.append((asset, version))
    return found


def episode_tts_usage(state: dict, project_id: str, episode_id: str) -> dict:
    """Recompute characters, requests and known cost from the domain records."""
    characters = requests = 0
    priced: list[Decimal] = []
    unpriced = False
    for _asset, version in episode_voice_assets(state, project_id, episode_id):
        source = version.source
        assert isinstance(source, ProviderAudioSource)
        counted = len(source.text)
        characters += counted
        requests += 1
        price = _price_for(source.provider, source.model)
        if price is None:
            unpriced = True
        else:
            priced.append(tts_cost(counted, price) or Decimal("0"))
    cost = None if unpriced or not priced else sum(priced, Decimal("0"))
    return {
        "characters": characters,
        "requests": requests,
        "cost": cost,
        "charactersLimit": TTS_EPISODE_CHARACTER_LIMIT,
        "requestsLimit": TTS_EPISODE_REQUEST_LIMIT,
        "costLimit": TTS_EPISODE_COST_LIMIT,
        "snapshot": limits_snapshot(characters=characters, requests=requests, cost=cost),
    }


def _voice_bindings(state: dict, project_id: str, episode_id: str) -> dict[str, dict]:
    bindings: dict[str, dict] = {}
    for asset, version in episode_voice_assets(state, project_id, episode_id):
        source = version.source
        assert isinstance(source, ProviderAudioSource)
        role = speaker_of(source)
        bindings.setdefault(
            role,
            {
                "speaker": role,
                "characterId": source.character_id,
                "provider": source.provider,
                "model": source.model,
                "voiceId": source.voice_id,
                "speed": source.speed,
                "audioId": asset.id,
                "audioVersion": version.version,
            },
        )
    return bindings


def _find_reusable(
    state: dict,
    *,
    project_id: str,
    episode_id: str,
    speaker: str,
    text: str,
    provider: str,
    model: str,
    voice_id: str,
    speed: float,
) -> tuple[AudioAsset, AudioAssetVersion] | None:
    for asset, version in episode_voice_assets(state, project_id, episode_id):
        source = version.source
        assert isinstance(source, ProviderAudioSource)
        if (
            speaker_of(source) == speaker
            and source.text == text
            and source.provider == provider
            and source.model == model
            and source.voice_id == voice_id
            and source.speed == speed
        ):
            return asset, version
    return None


def _asset_view(state: dict, asset: AudioAsset, version: AudioAssetVersion) -> dict:
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


def provider_summary(mode: str) -> dict:
    table = tts_capabilities(mode)[0]
    return {
        key: table[key]
        for key in (
            "provider",
            "model",
            "mode",
            "capabilitiesVersion",
            "priceKnown",
            "currency",
            "pricePerThousandCharacters",
            "priceVersion",
            "priceSourceUrl",
            "rpm",
            "sampleRate",
            "channels",
            "realSample",
            "verification",
            "endpoint",
            "voices",
        )
    }


def episode_audio_view(state: dict, project_id: str, episode_id: str, *, mode: str) -> dict | None:
    """The additive ``episode_read`` projection: voices, usage, bindings and assets."""
    if episode_id not in state["episodes"] or state["episodes"][episode_id]["projectId"] != project_id:
        return None
    usage = episode_tts_usage(state, project_id, episode_id)
    return {
        "provider": provider_summary(mode),
        "usage": usage["snapshot"],
        "bindings": list(_voice_bindings(state, project_id, episode_id).values()),
        "assets": [
            _asset_view(state, asset, version)
            for asset, version in episode_voice_assets(state, project_id, episode_id)
        ],
    }


class AudioService:
    def __init__(self, jobs):
        self.jobs = jobs
        self.store = jobs.store
        self.media = jobs.media
        self.files = jobs.media.files
        self.transport = None
        self.adapter_override = None

    # -- read side -------------------------------------------------------------
    def view(self, project_id: str, episode_id: str, *, mode: str | None = None) -> dict:
        mode = mode or self.mode()
        state = self.store.snapshot()
        episode = _episode(state, episode_id, project_id)
        usage = episode_tts_usage(state, project_id, episode.id)
        return {
            "episodeId": episode.id,
            "provider": provider_summary(mode),
            "usage": usage["snapshot"],
            "bindings": list(_voice_bindings(state, project_id, episode.id).values()),
            "assets": [
                _asset_view(state, asset, version)
                for asset, version in episode_voice_assets(state, project_id, episode.id)
            ],
        }

    def mode(self) -> str:
        config = getattr(self.jobs, "config", None)
        return getattr(config, "video_mode", "mock") or "mock"

    # -- generation ------------------------------------------------------------
    async def generate(self, args: dict, *, context) -> dict:
        try:
            parsed = AudioGenerateArgs.model_validate(args)
        except ValidationError as error:
            fields = [".".join(str(part) for part in issue["loc"]) or "arguments" for issue in error.errors()]
            raise AppError(
                "INVALID_ARGUMENTS", "参数格式不符合 Schema：" + ", ".join(fields)[:1000]
            ) from None
        raw = parsed.model_dump(mode="json", by_alias=True, exclude_none=True)
        previous = self.store.operation_result(context.operation_key, TOOL_NAME, raw)
        if previous is not None:
            return previous
        with self.store.locked():
            state = self.store.snapshot()
            self._check_context(state, context)
            mode = self._run_mode(state, context)
            if not parsed.confirm:
                raise _fail("CONFIRMATION_REQUIRED")
            episode = _episode(state, parsed.episode_id, context.project_id)
            planned, capability = self._plan(
                state, project_id=context.project_id, episode=episode, parsed=parsed, mode=mode
            )
        new_lines = [item for item in planned if not item.reused]
        # A cache-only replay needs no credentials and no provider client.
        adapter = self._adapter(mode) if new_lines else None
        produced: list[tuple[PlannedLine, object, str, str, str]] = []
        try:
            for line in new_lines:
                assert adapter is not None
                request = TtsRequestV1(
                    provider=capability["provider"],
                    model=capability["model"],
                    voice_id=line.voice_id,
                    text=line.text,
                    speed=line.speed,
                    sample_rate=48000,
                    episode_id=episode.id,
                    speaker=line.speaker,
                    character_id=line.character_id,
                )
                result = await adapter.synthesize(request)
                if result.characters > TTS_REQUEST_CHARACTER_LIMIT:
                    raise _fail("TTS_TEXT_LIMIT")
                media_id = str(uuid4())
                final_relative = audio_media_path(context.project_id, media_id, "audio/wav")
                part_relative = final_relative + ".part"
                with self.files.open(part_relative, writing=True) as handle:
                    handle.write(result.audio)
                    handle.flush()
                produced.append((line, result, media_id, final_relative, part_relative))
        except BaseException:
            for _line, _result, _media_id, _final, part_relative in produced:
                self.files.remove_part(part_relative)
            raise
        try:
            result = self.store.operation(
                context.operation_key,
                TOOL_NAME,
                raw,
                lambda draft: self._commit(
                    draft,
                    project_id=context.project_id,
                    episode=episode,
                    parsed=parsed,
                    mode=mode,
                    capability=capability,
                    planned=planned,
                    produced=produced,
                ),
            )
            if not result.get("ok"):
                for _line, _result, _media_id, _final, part_relative in produced:
                    self.files.remove_part(part_relative)
            return result
        except BaseException:
            for _line, _result, _media_id, _final, part_relative in produced:
                self.files.remove_part(part_relative)
            raise

    def _check_context(self, state: dict, context) -> None:
        run = state["runs"].get(context.run_id)
        if (
            run is None
            or context.project_id != context.session_id
            or context.project_id not in state["projects"]
            or run.get("sessionId") != context.session_id
        ):
            raise AppError("TOOL_CONTEXT_INVALID", "工具的项目、会话、Run 或调用标识不匹配。")
        if run.get("readOnly", False):
            raise AppError("READ_ONLY", "只读 Run 不能调用写工具。")

    def _run_mode(self, state: dict, context) -> str:
        run = state["runs"].get(context.run_id) or {}
        mode = run.get("videoMode") or self.mode()
        if mode not in {"mock", "live"}:
            raise AppError("VIDEO_MODEL_UNAVAILABLE", "当前 Run 没有可用的视频/语音模式。")
        return mode

    def _adapter(self, mode: str):
        if self.adapter_override is not None:
            return self.adapter_override
        if mode == "live":
            config = getattr(self.jobs, "config", None)
            return CosyVoiceAdapter(
                api_key=(getattr(config, "video_api_key", None) if config else None) or "",
                workspace_id=getattr(config, "video_workspace_id", None) if config else None,
                transport=self.transport,
            )
        return LocalTtsAdapter()

    def _voice_for(self, parsed: AudioGenerateArgs, speaker: str) -> str:
        for voice in parsed.voices:
            if voice.speaker == speaker:
                return voice.voice_id
        raise _fail("TTS_PROVIDER_UNKNOWN", f"（台词角色 {speaker} 没有配置音色）")

    def _speed_for(self, parsed: AudioGenerateArgs, speaker: str) -> float:
        for voice in parsed.voices:
            if voice.speaker == speaker:
                return voice.speed
        raise _fail("TTS_PROVIDER_UNKNOWN", f"（台词角色 {speaker} 没有配置音色）")

    def _selected_lines(self, state: dict, episode: Episode, parsed: AudioGenerateArgs) -> list[dict]:
        available = episode_lines(state, episode)
        if not available:
            raise _fail("TTS_EMPTY_EPISODE")
        if parsed.lines is None:
            return available
        selected = []
        for ref in parsed.lines:
            matches = [
                line
                for line in available
                if line["sceneId"] == ref.scene_id
                and line["paragraph"] == ref.paragraph
                and (ref.text is None or line["text"] == ref.text)
            ]
            if not matches:
                raise AppError("INVALID_ARGUMENTS", "台词范围与已冻结的台词不一致，整批未提交。")
            if len(matches) > 1:
                raise AppError("INVALID_ARGUMENTS", "同一段落有多条台词，请用 text 精确指定。")
            if matches[0] not in selected:
                selected.append(matches[0])
        return selected

    def _plan(
        self,
        state: dict,
        *,
        project_id: str,
        episode: Episode,
        parsed: AudioGenerateArgs,
        mode: str,
    ) -> tuple[list[PlannedLine], dict]:
        capability = None
        for voice in parsed.voices:
            table, _entry = capability_for(mode, voice.voice_id)
            if capability is None:
                capability = table
            elif table["provider"] != capability["provider"] or table["model"] != capability["model"]:
                raise _fail("TTS_PROVIDER_UNKNOWN", "（同一批次不能用两个供应商或模型）")
        assert capability is not None
        bindings = _voice_bindings(state, project_id, episode.id)
        for voice in parsed.voices:
            existing = bindings.get(voice.speaker)
            if existing is not None and (
                existing["provider"] != capability["provider"]
                or existing["model"] != capability["model"]
                or existing["voiceId"] != voice.voice_id
                or existing["speed"] != voice.speed
            ):
                raise _fail("VOICE_CONFLICT", f"（{voice.speaker}）")
        planned: list[PlannedLine] = []
        new_characters = 0
        new_requests = 0
        new_cost: Decimal | None = Decimal("0")
        price = Decimal(capability["pricePerThousandCharacters"]) if capability.get("priceKnown") else None
        for line in self._selected_lines(state, episode, parsed):
            speaker = line["speaker"]
            text = line["text"]
            if len(text) > TTS_REQUEST_CHARACTER_LIMIT:
                raise _fail("TTS_TEXT_LIMIT")
            voice_id = self._voice_for(parsed, speaker)
            speed = self._speed_for(parsed, speaker)
            character_id = _character_for_speaker(state, project_id, speaker)
            reused = _find_reusable(
                state,
                project_id=project_id,
                episode_id=episode.id,
                speaker=speaker,
                text=text,
                provider=capability["provider"],
                model=capability["model"],
                voice_id=voice_id,
                speed=speed,
            )
            planned.append(
                PlannedLine(
                    speaker=speaker,
                    character_id=character_id,
                    text=text,
                    scene_id=line["sceneId"],
                    paragraph=line["paragraph"],
                    voice_id=voice_id,
                    speed=speed,
                    reused_asset_id=reused[0].id if reused else None,
                    reused_version=reused[1].version if reused else None,
                )
            )
            if reused is None:
                new_characters += len(text)
                new_requests += 1
                line_cost = tts_cost(len(text), price) if price is not None else None
                if line_cost is not None and line_cost > TTS_REQUEST_COST_LIMIT:
                    raise _fail("TTS_BUDGET_LIMIT")
                if price is None:
                    new_cost = None
                elif new_cost is not None:
                    new_cost += line_cost or Decimal("0")
        usage = episode_tts_usage(state, project_id, episode.id)
        if usage["characters"] + new_characters > TTS_EPISODE_CHARACTER_LIMIT:
            raise _fail("TTS_TEXT_LIMIT")
        if usage["requests"] + new_requests > TTS_EPISODE_REQUEST_LIMIT:
            raise _fail("TTS_BUDGET_LIMIT")
        total_cost = usage["cost"] if usage["cost"] is not None else Decimal("0")
        if new_cost is not None:
            total_cost += new_cost
            if total_cost > TTS_EPISODE_COST_LIMIT:
                raise _fail("TTS_BUDGET_LIMIT")
        return planned, capability

    def _commit(
        self,
        draft: dict,
        *,
        project_id: str,
        episode: Episode,
        parsed: AudioGenerateArgs,
        mode: str,
        capability: dict,
        planned: list[PlannedLine],
        produced,
    ) -> dict:
        timestamp = now()
        episode = _episode(draft, episode.id, project_id)
        usage = episode_tts_usage(draft, project_id, episode.id)
        bindings = _voice_bindings(draft, project_id, episode.id)
        for voice in parsed.voices:
            existing = bindings.get(voice.speaker)
            if existing is not None and (
                existing["provider"] != capability["provider"]
                or existing["model"] != capability["model"]
                or existing["voiceId"] != voice.voice_id
                or existing["speed"] != voice.speed
            ):
                raise _fail("VOICE_CONFLICT", f"（{voice.speaker}）")
        pending = []
        for item in produced:
            line, result, media_id, final_relative, part_relative = item
            existing = _find_reusable(
                draft,
                project_id=project_id,
                episode_id=episode.id,
                speaker=line.speaker,
                text=line.text,
                provider=capability["provider"],
                model=capability["model"],
                voice_id=line.voice_id,
                speed=line.speed,
            )
            if existing is not None:
                self.files.remove_part(part_relative)
                continue
            part_path = Path(self.store.home) / part_relative
            size, digest = _hash_file(part_path)
            if size != result.size_bytes or digest != result.sha256:
                raise TtsError("AUDIO_FORMAT_UNSUPPORTED", "（产物哈希不一致）", stage="validate")
            pending.append((line, result, media_id, final_relative, part_relative))
        characters = sum(result.characters for _line, result, *_rest in pending)
        requests = len(pending)
        price = Decimal(capability["pricePerThousandCharacters"]) if capability.get("priceKnown") else None
        cost = Decimal("0") if price is not None else None
        for _line, result, *_rest in pending:
            if cost is not None and price is not None:
                line_cost = tts_cost(result.characters, price) or Decimal("0")
                if line_cost > TTS_REQUEST_COST_LIMIT:
                    raise _fail("TTS_BUDGET_LIMIT")
                cost += line_cost
        if usage["characters"] + characters > TTS_EPISODE_CHARACTER_LIMIT:
            raise _fail("TTS_TEXT_LIMIT")
        if usage["requests"] + requests > TTS_EPISODE_REQUEST_LIMIT:
            raise _fail("TTS_BUDGET_LIMIT")
        if cost is not None and (usage["cost"] or Decimal("0")) + cost > TTS_EPISODE_COST_LIMIT:
            raise _fail("TTS_BUDGET_LIMIT")
        for line, result, media_id, final_relative, _part_relative in pending:
            size = result.size_bytes
            digest = result.sha256
            facts = inspect_wav(result.audio)
            media = AudioMedia(
                id=media_id,
                project_id=project_id,
                relative_path=final_relative,
                mime_type="audio/wav",
                size_bytes=size,
                sha256=digest,
                duration_seconds=facts.duration_seconds,
                sample_rate=facts.sample_rate,
                channels=facts.channels,
                created_at=timestamp,
            )
            source = ProviderAudioSource(
                provider=capability["provider"],
                model=capability["model"],
                voice_id=line.voice_id,
                text=line.text,
                speed=line.speed,
                character_id=line.character_id,
                episode_id=episode.id,
                speaker=line.speaker,
            )
            version = AudioAssetVersion(
                version=1,
                kind="voice",
                media_id=media_id,
                source=source,
                duration_seconds=facts.duration_seconds,
                sample_rate=facts.sample_rate,
                channels=facts.channels,
                sha256=digest,
                size_bytes=size,
                created_at=timestamp,
            )
            asset = AudioAsset(
                id=str(uuid4()),
                project_id=project_id,
                kind="voice",
                current_version=1,
                versions=(version,),
                revision=0,
                created_at=timestamp,
            )
            self.files.promote(final_relative)
            draft["media"][media.id] = media.model_dump(mode="json", by_alias=True)
            draft["audio"][asset.id] = asset.model_dump(mode="json", by_alias=True)
        return self._result(
            draft,
            project_id=project_id,
            episode=episode,
            mode=mode,
            planned=planned,
            committed={(line.scene_id, line.paragraph) for line, *_rest in pending},
        )

    def _result(
        self,
        draft: dict,
        *,
        project_id: str,
        episode: Episode,
        mode: str,
        planned: list[PlannedLine],
        committed: set[tuple[str, int]],
    ) -> dict:
        capability = tts_capabilities(mode)[0]
        entries = []
        for line in planned:
            found = _find_reusable(
                draft,
                project_id=project_id,
                episode_id=episode.id,
                speaker=line.speaker,
                text=line.text,
                provider=capability["provider"],
                model=capability["model"],
                voice_id=line.voice_id,
                speed=line.speed,
            )
            if found is None:
                raise _fail("AUDIO_ASSET_MISSING")
            asset, version = found
            entries.append(
                {
                    "speaker": line.speaker,
                    "characterId": line.character_id,
                    "sceneId": line.scene_id,
                    "paragraph": line.paragraph,
                    "characters": len(line.text),
                    "textSha256": hashlib.sha256(line.text.encode("utf-8")).hexdigest(),
                    "audioId": asset.id,
                    "audioVersion": version.version,
                    "durationSeconds": version.duration_seconds,
                    "reused": (line.scene_id, line.paragraph) not in committed,
                }
            )
        usage = episode_tts_usage(draft, project_id, episode.id)
        return {
            "episodeId": episode.id,
            "provider": {key: value for key, value in provider_summary(mode).items() if key != "voices"},
            "lines": entries,
            "usage": usage["snapshot"],
            "bindings": list(_voice_bindings(draft, project_id, episode.id).values()),
            "assets": [
                _asset_view(draft, asset, version)
                for asset, version in episode_voice_assets(draft, project_id, episode.id)
            ],
        }


def _hash_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()
