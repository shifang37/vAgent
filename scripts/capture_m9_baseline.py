"""Capture the M9 E0 baseline: the frozen 16:9 facts and the frozen 9:16 targets.

Run once from the repository root on the released v0.6.0 tree:

    .\\.venv\\Scripts\\python.exe scripts/capture_m9_baseline.py

The committed ``docs/M9_E0_BASELINE.json`` is golden evidence for M9, not an output
to regenerate when a later compatibility check fails. E0 does not change product
code, schema, tools or capabilities, so every hash M9 inherits from M8 must stay
byte-identical: ``contractsSha256``, ``capabilitiesSha256``, both tool surfaces,
and ``routeProbe.rowsSha256``. The script never contacts a provider, never
registers a Job, never calls a model and never reads a credential.

The hashing, tool-surface and route-probe helpers are imported from
``scripts/capture_m8_baseline.py`` on purpose: M8 froze that script with its
baseline, so importing it keeps exactly one implementation of the frozen route
ordering instead of a second copy that could drift.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from capture_m8_baseline import (  # noqa: E402
    accounting_limits,
    contract_schemas,
    price_table,
    published_surface,
    route_probe,
)

OUTPUT = ROOT / "docs" / "M9_E0_BASELINE.json"
# E0 froze these bytes on the v0.6.0 tree. Recording the release rather than reading
# ``pyproject.toml`` keeps the frozen file stable across the v0.7.0 bump at E4.
FROZEN_RELEASE_VERSION = "0.6.0"


# --- frozen 16:9 facts the vertical milestone must not disturb -----------------


def horizontal_facts() -> dict[str, object]:
    """The exact 16:9 geometry, wrapping and gates E1/E2 must leave byte-identical."""
    from vagent.video import assembler, contracts, films
    from vagent.video.subtitles import ASS_STYLE, SUBTITLE_LINE_MAX_CHARS, SUBTITLE_STYLE_PROFILE

    return {
        "shotSpec": {
            "model": "ShotSpecV3",
            "t2vAspectRatio": "16:9",
            "t2vDurations": list(contracts.SHOT_DURATIONS),
            "i2vAspectRatio": None,
        },
        "videoRequest": {
            "model": "VideoRequestV6",
            "ratio": "16:9",
            "capabilitiesVersion": contracts.MULTI_PROVIDER_CAPABILITIES_VERSION,
            "publishedSpec": {
                "mode": "t2v",
                "durationSeconds": 5,
                "resolution": "720p",
                "aspectRatio": "16:9",
            },
        },
        "exportProfiles": {
            name: {
                "width": profile["width"],
                "height": profile["height"],
                "upscaled": profile["upscaled"],
            }
            for name, profile in (
                (films.PROFILE_720P, films.profile_for("soft", name=films.PROFILE_720P)),
                (films.PROFILE_1080P, films.profile_for("soft", name=films.PROFILE_1080P)),
            )
        },
        "normalizationProfile": assembler.NORMALIZATION_PROFILE,
        "mediaGeometry": {"width": 1280, "height": 720, "checkedIn": "src/vagent/video/media.py:295"},
        "subtitleStyle": {
            "profile": SUBTITLE_STYLE_PROFILE,
            "lineMaxChars": SUBTITLE_LINE_MAX_CHARS,
            "assStyle": dict(ASS_STYLE),
        },
    }


def vertical_targets() -> dict[str, object]:
    """What E1-E4 will add; recorded now so E1 can prove it changed nothing else."""
    from vagent.video.mixer import SILENCE_PEAK_DB

    return {
        "frames": {"supported": ["16:9", "9:16"], "default": "16:9", "onePerProject": True},
        "config": {"key": "videoFrame", "env": "VAGENT_VIDEO_FRAME", "values": ["16:9", "9:16"]},
        "geometry": {
            "vertical": {"width": 1080, "height": 1920, "fps": 30, "profile": "1080p-vertical-h264-aac-v1"},
            "normalizationProfile": "h264-1080x1920-30fps-aac-48k-stereo-v1",
            "notSupported": ["720x1280", "1:1", "5:4"],
        },
        "contracts": {
            "shotSpec": "ShotSpecV4",
            "videoRequest": "VideoRequestV7",
            "job": "JobV7",
            "capabilitiesVersion": "multi-provider-v4",
            "historicalKept": ["ShotSpecV3", "VideoRequestV6", "JobV6", "multi-provider-v3"],
        },
        "subtitle": {
            "verticalProfile": "subtitle-zh-v2",
            "lineMaxChars": 14,
            "bottomSafeAreaRatio": 0.12,
            "sideMarginRatio": 0.06,
            "srtLineMaxCharsUnchanged": 24,
        },
        "newErrorCodes": [
            "FRAME_UNSUPPORTED",
            "CHARACTER_ASSET_FRAME_MISMATCH",
            "TIMELINE_FRAME_MISMATCH",
            "SUBTITLE_STYLE_FRAME_MISMATCH",
        ],
        "toolSurface": {"toolsVersion": 18, "rulesVersion": 18, "newVideoTools": [], "promptChanged": False},
        "schemaVersion": 9,
        "newCollections": [],
        "priceRule": {
            "verticalPricesPublishedOnlyAfterOfficialCheck": True,
            "unverifiedVerticalProvider": "ROUTE_UNAVAILABLE",
            "crossCurrencyConversion": False,
        },
        "mixHardening": {
            "silencePeakDb": SILENCE_PEAK_DB,
            "silentPremixErrorCode": "LOUDNESS_OUT_OF_RANGE",
            "silentPremixDetail": "（混音为静音，未交付）",
            "droppedAudioReasons": ["no-dialogue-line", "no-cue", "outside-edit"],
            "allAudioDroppedErrorCode": "RENDER_SOURCE_MISSING",
        },
    }


def main() -> int:
    import asyncio

    contracts = contract_schemas()
    surface = asyncio.run(published_surface())
    report = {
        "baseline": "m9-e0",
        "purpose": (
            "Pin the post-M8 16:9 contract schemas, published tool surfaces, capability and "
            "price tables, accounting ceilings, route ordering and mix gates, plus the frozen "
            "9:16 targets M9 E1-E4 must add without changing any of the pinned bytes."
        ),
        "releaseVersion": FROZEN_RELEASE_VERSION,
        "zeroSpend": {
            "submit": 0,
            "modelCalls": 0,
            "ttsRequests": 0,
            "providerRequests": 0,
            "credentialsRead": False,
            "jobsRegistered": 0,
        },
        "contracts": contracts,
        "tools": surface["tools"],
        "capabilities": surface["capabilities"],
        "prices": price_table(),
        "limits": accounting_limits(),
        "routeProbe": route_probe(surface["capabilities"]["live"]),
        "frames": {
            "horizontal": horizontal_facts(),
            "verticalTargets": vertical_targets(),
        },
    }
    OUTPUT.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"wrote {OUTPUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
