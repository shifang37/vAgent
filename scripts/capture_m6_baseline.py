"""Capture the M6 E0 baseline: schema 6 shapes, frozen limits and local FFmpeg facts.

Run once from the repository root on the post-M5 implementation:

    .\\.venv\\Scripts\\python.exe scripts/capture_m6_baseline.py

The committed ``docs/M6_E0_BASELINE.json`` is golden evidence, not an output to
regenerate when a later migration or compatibility check fails. The script never
contacts a provider, never registers a Job and never calls a model: it hashes
contract schemas, records the frozen limits M6 must not silently change, and
probes the bundled FFmpeg on synthetic input only.

E1's v6->v7 migration must keep every ``schemaSha256`` entry, every limit and the
tool name groups byte-identical; only the additive v7 collections may appear.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import imageio_ffmpeg
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs" / "M6_E0_BASELINE.json"

CONTRACT_MODULES = (
    "vagent.storage",
    "vagent.video.contracts",
    "vagent.video.films",
    "vagent.video.episodes",
    "vagent.video.storyboards",
)
WANTED_FILTERS = (
    "subtitles",
    "ass",
    "xfade",
    "loudnorm",
    "afade",
    "drawtext",
    "amix",
    "sidechaincompress",
    "aresample",
    "scale",
    "fps",
)
WANTED_ENCODERS = ("mov_text", "ass", "subrip", "aac", "libx264")
WANTED_MUXERS = ("srt", "ass", "mp4", "matroska")
SYSTEM_FONT = Path(r"C:\Windows\Fonts\msyh.ttc")


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
    import vagent.video.contracts as contracts
    import vagent.video.episodes as episodes
    import vagent.video.films as films
    import vagent.video.tools as tools

    return {
        "storageSchemaVersion": 6,
        "film": {
            "assemblyVersion": films.ASSEMBLY_VERSION,
            "normalizationVersion": films.NORMALIZATION_VERSION,
            "normalizationProfile": films.NORMALIZATION_PROFILE,
            "compatibilityVersion": films.COMPATIBILITY_VERSION,
            "filmMediaContractVersion": films.FILM_MEDIA_CONTRACT_VERSION,
            "inputLimit": films.FILM_INPUT_LIMIT,
            "versionLimit": films.FILM_VERSION_LIMIT,
            "assemblyTimeoutSeconds": assembler.ASSEMBLY_TIMEOUT_SECONDS,
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
            "shotDurationMin": contracts.SHOT_DURATION_MIN,
            "shotDurationMax": contracts.SHOT_DURATION_MAX,
            "jobCap": contracts.EPISODE_JOB_CAP,
            "toolsVersion": contracts.EPISODE_TOOLS_VERSION,
        },
        "wait": {"setLimit": tools.WAIT_SET_LIMIT, "timeoutSeconds": tools.WAIT_TIMEOUT_SECONDS},
        "toolGroups": {
            "M2": list(tools.M2_TOOL_NAMES),
            "M3": list(tools.M3_TOOL_NAMES),
            "M5": list(tools.M5_TOOL_NAMES),
            "M5E2": list(tools.M5_E2_TOOL_NAMES),
            "M5E3": list(tools.M5_E3_TOOL_NAMES),
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


def listed(output: str) -> set[str]:
    names = set()
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 2 and re.fullmatch(r"[A-Z.]{1,8}", parts[0] or ""):
            names.add(parts[1])
    return names


def ffmpeg_facts() -> dict[str, object]:
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    version = run(ffmpeg, "-version").stdout.splitlines()
    configuration = next((line for line in version if line.startswith("configuration:")), "")
    filters = listed(run(ffmpeg, "-filters").stdout)
    encoders = listed(run(ffmpeg, "-encoders").stdout)
    muxers = listed(run(ffmpeg, "-muxers").stdout)

    work = Path(tempfile.mkdtemp(prefix="m6-e0-"))
    try:
        srt = work / "cue.srt"
        srt.write_text("1\n00:00:00,000 --> 00:00:00,800\n冰箱上的便条\n", encoding="utf-8")
        escaped = str(srt).replace("\\", "/").replace(":", "\\:")
        burn = work / "burn.mp4"
        burn_result = run(
            ffmpeg,
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=320x180:d=1:r=30",
            "-vf",
            f"subtitles='{escaped}':force_style='FontName=Microsoft YaHei'",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(burn),
        )
        frame = work / "frame.png"
        if burn.exists():
            run(
                ffmpeg,
                "-loglevel",
                "error",
                "-ss",
                "0.4",
                "-i",
                str(burn),
                "-frames:v",
                "1",
                "-y",
                str(frame),
            )
        soft = work / "soft.mp4"
        soft_result = run(
            ffmpeg,
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=320x180:d=1:r=30",
            "-i",
            str(srt),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:s",
            "mov_text",
            "-y",
            str(soft),
        )
        loudness = run(
            ffmpeg,
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-af",
            "loudnorm=print_format=json",
            "-f",
            "null",
            "-",
        )
        measured = {
            key: value for key, value in re.findall(r'"(input_[a-z]+)"\s*:\s*"(-?[0-9.]+)"', loudness.stderr)
        }
        return {
            "binarySha256": sha256_bytes(Path(ffmpeg).read_bytes()),
            "versionLine": version[0] if version else None,
            "configurationSha256": sha256_bytes(configuration.encode("utf-8")),
            "ffprobePresent": Path(ffmpeg).with_name("ffprobe.exe").exists(),
            "capabilities": {
                "filters": {name: name in filters for name in WANTED_FILTERS},
                "encoders": {name: name in encoders for name in WANTED_ENCODERS},
                "muxers": {name: name in muxers for name in WANTED_MUXERS},
            },
            "probes": {
                "burnInChinese": {
                    "returncode": burn_result.returncode,
                    "outputBytes": burn.stat().st_size if burn.exists() else 0,
                    "frameSha256": sha256_bytes(frame.read_bytes()) if frame.exists() else None,
                },
                "softSubtitles": {
                    "returncode": soft_result.returncode,
                    "outputBytes": soft.stat().st_size if soft.exists() else 0,
                },
                "loudnessMeasurement": {"returncode": loudness.returncode, "input": measured},
                "systemCjkFontPresent": SYSTEM_FONT.exists(),
            },
        }
    finally:
        import shutil

        shutil.rmtree(work, ignore_errors=True)


def main() -> int:
    report = {
        "baseline": "m6-e0",
        "purpose": "Pin the schema 6 contract shapes, frozen limits and local FFmpeg facts M6 extends.",
        "submit": 0,
        "modelCalls": 0,
        "providerRequests": 0,
        "schemaSha256": contract_schemas(),
        "limits": frozen_limits(),
        "ffmpeg": ffmpeg_facts(),
    }
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUTPUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
