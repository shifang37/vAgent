"""Capture the M7 E0 baseline: schema 8 shapes, frozen limits and the local trim probe.

Run once from the repository root on the post-M6 implementation:

    .\\.venv\\Scripts\\python.exe scripts/capture_m7_baseline.py

The committed ``docs/M7_E0_BASELINE.json`` is golden evidence, not an output to
regenerate when a later migration or compatibility check fails. The script never
contacts a provider, never registers a Job and never calls a model: it hashes
contract schemas, records the frozen limits M7 must not silently change, and probes
the bundled FFmpeg on synthetic input only.

E1's v8->v9 migration must keep every ``schemaSha256`` entry, every limit and the
tool name groups byte-identical; only the additive v9 collections and their new
models may appear. The timeline trim probe records what ``ffmpeg -ss/-t`` does on
this machine so later render claims are measured, not assumed.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import imageio_ffmpeg
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs" / "M7_E0_BASELINE.json"

CONTRACT_MODULES = (
    "vagent.storage",
    "vagent.video.contracts",
    "vagent.video.films",
    "vagent.video.episodes",
    "vagent.video.storyboards",
    "vagent.video.renders",
    "vagent.video.audio",
    "vagent.video.subtitles",
    "vagent.video.tts",
)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def contract_schemas() -> dict[str, dict[str, str]]:
    """Hash every contract model defined by each module, not imported into it."""
    result: dict[str, dict[str, str]] = {}
    for module_name in CONTRACT_MODULES:
        module = importlib.import_module(module_name)
        models: dict[str, str] = {}
        for name, value in vars(module).items():
            if not isinstance(value, type) or not issubclass(value, BaseModel):
                continue
            if value.__module__ != module_name:
                continue
            canonical = json.dumps(value.model_json_schema(), sort_keys=True, separators=(",", ":"))
            models[name] = sha256_bytes(canonical.encode("utf-8"))
        result[module_name] = dict(sorted(models.items()))
    return result


def frozen_limits() -> dict[str, object]:
    import vagent.video.assembler as assembler
    import vagent.video.audio as audio
    import vagent.video.contracts as contracts
    import vagent.video.episodes as episodes
    import vagent.video.films as films
    import vagent.video.renders as renders
    import vagent.video.subtitles as subtitles
    import vagent.video.tools as tools

    return {
        "storageSchemaVersion": 8,
        "film": {
            "assemblyVersion": films.ASSEMBLY_VERSION,
            "normalizationVersion": films.NORMALIZATION_VERSION,
            "normalizationProfile": films.NORMALIZATION_PROFILE,
            "compatibilityVersion": films.COMPATIBILITY_VERSION,
            "filmMediaContractVersion": films.FILM_MEDIA_CONTRACT_VERSION,
            "mediaValidationVersion": films.MEDIA_VALIDATION_VERSION,
            "inputLimit": films.FILM_INPUT_LIMIT,
            "versionLimit": films.FILM_VERSION_LIMIT,
            "assemblyTimeoutSeconds": assembler.ASSEMBLY_TIMEOUT_SECONDS,
            "targetWidth": assembler.TARGET_WIDTH,
            "targetHeight": assembler.TARGET_HEIGHT,
            "targetFps": assembler.TARGET_FPS,
            "targetAudioRate": assembler.TARGET_AUDIO_RATE,
            "targetAudioChannels": assembler.TARGET_AUDIO_CHANNELS,
            "maxInputs": assembler.MAX_INPUTS,
        },
        "render": {
            "renderVersion": films.RENDER_VERSION,
            "nativeAssembly": films.RENDER_NATIVE_ASSEMBLY,
            "profiles": list(films.EXPORT_PROFILES),
            "subtitleModes": list(films.SUBTITLE_MODES),
            "maxSeconds": films.RENDER_MAX_SECONDS,
            "timeoutSeconds": films.RENDER_TIMEOUT_SECONDS,
            "jobTimeoutSeconds": films.RENDER_JOB_TIMEOUT_SECONDS,
            "titleCardLimit": films.TITLE_CARD_LIMIT,
            "transitionSecondsMin": float(films.TRANSITION_SECONDS_MIN),
            "transitionSecondsMax": float(films.TRANSITION_SECONDS_MAX),
            "maxAudioTracks": films.MAX_RENDER_AUDIO_TRACKS,
            "taskLimit": renders.RENDER_TASK_LIMIT,
            "sceneLimit": renders.RENDER_SCENE_LIMIT,
            "titleStyleProfile": renders.TITLE_STYLE_PROFILE,
        },
        "episode": {
            "minSeconds": episodes.EPISODE_MIN_SECONDS,
            "maxSeconds": episodes.EPISODE_MAX_SECONDS,
            "minShots": episodes.EPISODE_MIN_SHOTS,
            "maxShots": episodes.EPISODE_MAX_SHOTS,
            "sceneMaxShots": episodes.SCENE_MAX_SHOTS,
            "sceneLimit": episodes.SCENE_LIMIT,
            "costCap": episodes.EPISODE_COST_CAP,
            "shotEstimateAmount": episodes.SHOT_ESTIMATE_AMOUNT,
            "sourceTextMaxCharacters": episodes.SOURCE_TEXT_MAX_CHARACTERS,
            "textSplitContract": episodes.TEXT_SPLIT_CONTRACT,
            "shotDurationMin": contracts.SHOT_DURATION_MIN,
            "shotDurationMax": contracts.SHOT_DURATION_MAX,
            "jobCap": contracts.EPISODE_JOB_CAP,
            "toolsVersion": contracts.EPISODE_TOOLS_VERSION,
        },
        "audio": {
            "contractVersion": audio.AUDIO_MEDIA_CONTRACT_VERSION,
            "validationVersion": audio.AUDIO_VALIDATION_VERSION,
            "versionLimit": audio.AUDIO_VERSION_LIMIT,
            "kindLimits": dict(audio.AUDIO_KIND_LIMITS),
            "maxCueSeconds": audio.AUDIO_MAX_CUE_SECONDS,
        },
        "subtitle": {
            "alignVersion": subtitles.SUBTITLE_ALIGN_VERSION,
            "styleProfile": subtitles.SUBTITLE_STYLE_PROFILE,
            "cueLimit": subtitles.SUBTITLE_CUE_LIMIT,
            "cueMaxChars": subtitles.SUBTITLE_CUE_MAX_CHARS,
            "lineMaxChars": subtitles.SUBTITLE_LINE_MAX_CHARS,
            "sceneCueLimit": subtitles.SUBTITLE_SCENE_CUE_LIMIT,
            "versionLimit": subtitles.SUBTITLE_VERSION_LIMIT,
            "timelineLimit": subtitles.SUBTITLE_TIMELINE_LIMIT,
        },
        "wait": {"setLimit": tools.WAIT_SET_LIMIT, "timeoutSeconds": tools.WAIT_TIMEOUT_SECONDS},
        "tools": {
            "videoToolsVersion": tools.VIDEO_TOOLS_VERSION,
            "videoRulesVersion": tools.VIDEO_RULES_VERSION,
            "liveVideoToolsVersion": tools.LIVE_VIDEO_TOOLS_VERSION,
            "liveVideoRulesVersion": tools.LIVE_VIDEO_RULES_VERSION,
        },
        "toolGroups": {
            "M2": list(tools.M2_TOOL_NAMES),
            "M3": list(tools.M3_TOOL_NAMES),
            "M5": list(tools.M5_TOOL_NAMES),
            "M5E2": list(tools.M5_E2_TOOL_NAMES),
            "M5E3": list(tools.M5_E3_TOOL_NAMES),
            "M6E1": list(tools.M6_E1_TOOL_NAMES),
            "M6E2": list(tools.M6_E2_TOOL_NAMES),
            "M6E3": list(tools.M6_E3_TOOL_NAMES),
        },
    }


def run(ffmpeg: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [ffmpeg, "-hide_banner", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def mp4_metadata(path: Path) -> dict[str, object]:
    from vagent.video.mp4 import inspect_mp4

    with path.open("rb") as handle:
        metadata = inspect_mp4(handle, path.stat().st_size)
    return metadata.model_dump(mode="json", by_alias=True)


def ffmpeg_facts() -> dict[str, object]:
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    version = run(ffmpeg, "-version").stdout.splitlines()
    configuration = next((line for line in version if line.startswith("configuration:")), "")
    ffprobe = Path(ffmpeg).with_name("ffprobe")

    work = Path(tempfile.mkdtemp(prefix="m7-e0-"))
    try:
        source = work / "source.mp4"
        source_result = run(
            ffmpeg,
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=30:duration=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            "-y",
            str(source),
        )
        trimmed = work / "trim.mp4"
        trim_result = run(
            ffmpeg,
            "-loglevel",
            "error",
            "-ss",
            "0.5",
            "-i",
            str(source),
            "-t",
            "1.0",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-y",
            str(trimmed),
        )
        trimmed_metadata = mp4_metadata(trimmed) if trimmed.exists() else {}
        measured = float(trimmed_metadata.get("durationSeconds", 0.0)) if trimmed_metadata else None
        return {
            "packageVersion": getattr(imageio_ffmpeg, "__version__", None),
            "binarySha256": sha256_bytes(Path(ffmpeg).read_bytes()),
            "versionLine": version[0] if version else None,
            "configurationSha256": sha256_bytes(configuration.encode("utf-8")),
            "ffprobePresent": ffprobe.exists() or ffprobe.with_suffix(".exe").exists(),
            "probes": {
                "syntheticSource": {
                    "returncode": source_result.returncode,
                    "outputBytes": source.stat().st_size if source.exists() else 0,
                    "metadata": mp4_metadata(source) if source.exists() else {},
                },
                "trim500msForOneSecond": {
                    "returncode": trim_result.returncode,
                    "outputBytes": trimmed.stat().st_size if trimmed.exists() else 0,
                    "sha256": sha256_bytes(trimmed.read_bytes()) if trimmed.exists() else None,
                    "metadata": trimmed_metadata,
                    "measuredDurationSeconds": measured,
                    "durationDelta": round(measured - 1.0, 6) if measured is not None else None,
                    "stderrLines": [
                        line
                        for line in trim_result.stderr.splitlines()
                        if re.search(r"error|warning|non-monotonic", line, re.IGNORECASE)
                    ],
                },
            },
        }
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main() -> int:
    report = {
        "baseline": "m7-e0",
        "purpose": "Pin the schema 8 contract shapes, frozen limits and local FFmpeg trim facts M7 extends.",
        "submit": 0,
        "modelCalls": 0,
        "providerRequests": 0,
        "schemaSha256": contract_schemas(),
        "limits": frozen_limits(),
        "ffmpeg": ffmpeg_facts(),
    }
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {OUTPUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
