"""M8 E1: read-only provider route preview (``provider-route-v1``).

Every case here is offline: the preview reads the frozen capability/price table,
whether a key is configured, and (for storyboards) local state. It never contacts
a provider, never registers a Job and never writes storage.
"""

import json
from decimal import Decimal
from pathlib import Path

import pytest
from shot_support import insert_image, mock_tools, save_storyboard, shot

from vagent.config import Config
from vagent.errors import AppError
from vagent.video import routing
from vagent.video.contracts import (
    FAL_PRICE_VERSION,
    KLING_PRICE_VERSION,
    WAN_I2V_MODEL,
    WAN_I2V_PRICE_VERSION,
    WAN_MODEL,
    WAN_PRICE_VERSION,
    canonical_fingerprint,
    fal_capabilities,
    multi_provider_capabilities,
    wan_shot_capabilities,
)
from vagent.video.jobs import JobService

ROOT = Path(__file__).resolve().parents[1]
BASELINE = json.loads((ROOT / "docs" / "M8_E0_BASELINE.json").read_text(encoding="utf-8"))
KEYS = {"wan": True, "kling": True, "fal": True}
AUDIO_MODES = {"wan": "auto", "kling": "native", "fal": "silent"}


def live_config(home, **changes):
    return Config(
        **{
            "home": home,
            "api_key": None,
            "video_mode": "live",
            "video_api_key": "wan-test-secret",
            "kling_api_key": "kling-test-secret",
            "fal_api_key": "fal-test-secret",
            **changes,
        }
    )


def by_provider(preview, provider):
    return next(item for item in preview.candidates if item.provider == provider)


def probe_row(preview, request_row):
    """Project one preview onto the frozen routeProbe row shape.

    The probe drops the quote of every blocked candidate; the preview keeps a
    verified price for reasons that are about environment or accounting (off
    mode, missing key, scope, ceiling), so only that projection is dropped here.
    """
    return {
        "request": {
            "scope": request_row["scope"],
            "mode": request_row["mode"],
            "durationSeconds": request_row["durationSeconds"],
            "aspectRatio": request_row["aspectRatio"],
        },
        "candidates": [
            {
                "provider": item.provider,
                "model": item.model,
                "capabilitiesVersion": item.capabilities_version,
                "audioMode": AUDIO_MODES[item.provider],
                "available": item.available,
                "reason": item.reason,
                "quote": (
                    None
                    if not item.available
                    else {
                        "currency": item.currency,
                        "unitPrice": item.unit_price,
                        "quantity": item.quantity,
                        "amount": item.amount,
                        "priceVersion": item.price_version,
                        "sourceUrl": item.source_url,
                        **(
                            {"resource": {"unit": "VIDEO_UNIT", "units": item.resource_units}}
                            if item.resource_units is not None
                            else {}
                        ),
                    }
                ),
            }
            for item in preview.candidates
        ],
        "recommended": preview.recommended,
    }


def spec_request(mode="t2v", duration=5, aspect=None, scope="shot"):
    return routing.make_request(
        scope=scope,
        mode=mode,
        duration_seconds=duration,
        aspect_ratio=aspect,
    )


def test_preview_reproduces_the_frozen_route_probe():
    rows = []
    for row in BASELINE["routeProbe"]["rows"]:
        request = routing.make_request(
            scope=row["request"]["scope"],
            mode=row["request"]["mode"],
            duration_seconds=row["request"]["durationSeconds"],
            aspect_ratio=row["request"]["aspectRatio"],
        )
        preview = routing.preview(request, keys_configured=KEYS)
        rows.append(probe_row(preview, row["request"]))
        assert preview.recommended == row["recommended"]
    # Same amounts, same units, same order: E1 reproduces the E0 probe byte for byte.
    assert [row["candidates"] for row in rows] == [
        row["candidates"] for row in BASELINE["routeProbe"]["rows"]
    ]
    assert canonical_fingerprint(rows) == BASELINE["routeProbe"]["rowsSha256"]


@pytest.mark.parametrize("scope", ["episode", "batch"])
def test_episode_and_batch_scopes_reject_usd_but_keep_the_quote(scope):
    preview = routing.preview(spec_request(scope=scope), keys_configured=KEYS)
    wan, kling, fal = preview.candidates
    assert (wan.provider, wan.available, wan.amount) == ("wan", True, "3.00")
    assert (kling.reason, kling.amount) == ("EPISODE_COST_LIMIT", "0.56")
    assert (fal.reason, fal.amount) == ("EPISODE_COST_LIMIT", "0.42")
    assert preview.recommended == "wan"
    assert any("只接受 CNY" in note for note in preview.notes)
    assert any("不换汇" in note for note in preview.notes)


@pytest.mark.parametrize("mode", ["t2v", "i2v", "flf2v"])
@pytest.mark.parametrize("duration", [5, 10])
@pytest.mark.parametrize("provider", ["wan", "kling", "fal"])
def test_the_full_provider_mode_duration_matrix(provider, duration, mode):
    """wan x kling x fal, t2v/i2v/flf2v, 5/10 seconds, all at the default ceilings."""
    expected = {
        "wan": {
            ("t2v", 5): (True, None, "3.00"),
            ("t2v", 10): (False, "VIDEO_COST_LIMIT", "6.00"),
            ("i2v", 5): (True, None, "3.00"),
            ("i2v", 10): (False, "VIDEO_COST_LIMIT", "6.00"),
            ("flf2v", 5): (True, None, "3.00"),
            ("flf2v", 10): (False, "VIDEO_COST_LIMIT", "6.00"),
        },
        "kling": {
            ("t2v", 5): (True, None, "0.56"),
            ("t2v", 10): (False, "VIDEO_UNSUPPORTED_SPEC", None),
            ("i2v", 5): (False, "VIDEO_UNSUPPORTED_MODE", None),
            ("i2v", 10): (False, "VIDEO_UNSUPPORTED_MODE", None),
            ("flf2v", 5): (False, "VIDEO_UNSUPPORTED_MODE", None),
            ("flf2v", 10): (False, "VIDEO_UNSUPPORTED_MODE", None),
        },
        "fal": {
            ("t2v", 5): (True, None, "0.42"),
            ("t2v", 10): (False, "VIDEO_UNSUPPORTED_SPEC", None),
            ("i2v", 5): (False, "VIDEO_UNSUPPORTED_MODE", None),
            ("i2v", 10): (False, "VIDEO_UNSUPPORTED_MODE", None),
            ("flf2v", 5): (False, "VIDEO_UNSUPPORTED_MODE", None),
            ("flf2v", 10): (False, "VIDEO_UNSUPPORTED_MODE", None),
        },
    }
    preview = routing.preview(spec_request(mode=mode, duration=duration), keys_configured=KEYS)
    candidate = by_provider(preview, provider)
    assert (candidate.available, candidate.reason, candidate.amount) == expected[provider][(mode, duration)]
    assert preview.recommended == next((item.provider for item in preview.candidates if item.available), None)


@pytest.mark.parametrize(
    ("mode", "duration", "expected"),
    [
        # (provider, available, reason, amount, model, price_version)
        ("t2v", 5, [("wan", True, None, "3.00", WAN_MODEL, WAN_PRICE_VERSION)]),
        ("t2v", 10, [("wan", False, "VIDEO_COST_LIMIT", "6.00", WAN_MODEL, WAN_PRICE_VERSION)]),
        ("i2v", 5, [("wan", True, None, "3.00", WAN_I2V_MODEL, WAN_I2V_PRICE_VERSION)]),
        ("flf2v", 10, [("wan", False, "VIDEO_COST_LIMIT", "6.00", WAN_I2V_MODEL, WAN_I2V_PRICE_VERSION)]),
    ],
)
def test_wan_entries_follow_the_mode_and_the_per_job_ceiling(mode, duration, expected):
    preview = routing.preview(spec_request(mode=mode, duration=duration), keys_configured=KEYS)
    wan = by_provider(preview, "wan")
    assert [(wan.provider, wan.available, wan.reason, wan.amount, wan.model, wan.price_version)] == expected
    assert wan.currency == "CNY" and wan.unit == "output_second"


def test_multi_provider_entries_are_frozen_to_five_seconds_of_text_to_video():
    for duration, reason in ((5, None), (10, "VIDEO_UNSUPPORTED_SPEC")):
        preview = routing.preview(spec_request(duration=duration), keys_configured=KEYS)
        kling, fal = by_provider(preview, "kling"), by_provider(preview, "fal")
        assert (kling.available, kling.reason) == (reason is None, reason)
        assert (fal.available, fal.reason) == (reason is None, reason)
        assert kling.model == "kling-3.0-turbo"
        assert fal.model == "fal-ai/kling-video/v3/standard/text-to-video"
        if reason is None:
            assert (kling.amount, kling.price_version, kling.resource_units) == (
                "0.56",
                KLING_PRICE_VERSION,
                4,
            )
            assert (fal.amount, fal.price_version, fal.resource_units) == ("0.42", FAL_PRICE_VERSION, None)
        else:
            assert kling.amount is None and fal.amount is None
            assert kling.quote_fingerprint is None


@pytest.mark.parametrize("mode", ["i2v", "flf2v"])
def test_image_modes_are_unsupported_by_the_text_only_providers(mode):
    preview = routing.preview(spec_request(mode=mode), keys_configured=KEYS)
    for provider in ("kling", "fal"):
        candidate = by_provider(preview, provider)
        assert (candidate.available, candidate.reason) == (False, "VIDEO_UNSUPPORTED_MODE")
        assert candidate.model is not None and candidate.amount is None


def test_a_missing_key_blocks_registration_but_leaves_the_price_readable():
    preview = routing.preview(spec_request(), keys_configured={"wan": True, "kling": False, "fal": False})
    kling, fal = by_provider(preview, "kling"), by_provider(preview, "fal")
    assert (kling.reason, kling.key_configured, kling.amount) == ("VIDEO_KEY_MISSING", False, "0.56")
    assert (fal.reason, fal.key_configured, fal.amount) == ("VIDEO_KEY_MISSING", False, "0.42")
    assert preview.recommended == "wan"


def test_a_stale_price_is_never_recommended():
    stale = tuple(
        entry.model_copy(
            update={"price": entry.price.model_copy(update={"price_version": "wan27-old-price"})}
        )
        for entry in wan_shot_capabilities()
    )
    preview = routing.preview(
        spec_request(),
        capabilities=(*stale, *multi_provider_capabilities()),
        keys_configured=KEYS,
    )
    wan = by_provider(preview, "wan")
    assert (wan.available, wan.reason) == (False, "VIDEO_PRICE_UNKNOWN")
    assert wan.amount is None and wan.quote_fingerprint is None
    assert preview.recommended == "fal"  # 0.42 USD is the smaller verified USD quote


def test_an_explicitly_named_provider_without_an_entry_is_reported():
    request = routing.make_request(mode="t2v", duration_seconds=5, provider="kling")
    preview = routing.preview(
        request,
        capabilities=(*wan_shot_capabilities(), fal_capabilities()),
        keys_configured=KEYS,
    )
    assert [item.provider for item in preview.candidates] == ["wan", "fal", "kling"]
    kling = by_provider(preview, "kling")
    assert (kling.available, kling.reason, kling.model) == (False, "VIDEO_PROVIDER_UNAVAILABLE", None)
    assert any("没有已发布条目" in note for note in preview.notes)


def test_require_available_fails_when_nothing_can_be_registered():
    with pytest.raises(AppError) as raised:
        routing.preview(spec_request(duration=10), keys_configured=KEYS, require_available=True)
    assert raised.value.code == "ROUTE_UNAVAILABLE"


def test_video_mode_off_keeps_the_comparison_readable():
    preview = routing.preview(spec_request(), keys_configured=KEYS, video_mode="mock")
    assert {item.reason for item in preview.candidates} == {"VIDEO_MODE_OFF"}
    assert preview.recommended is None
    assert by_provider(preview, "wan").amount == "3.00"
    assert by_provider(preview, "kling").amount == "0.56"
    assert any("当前视频模式为 mock" in note for note in preview.notes)


def test_raising_the_wan_ceiling_makes_a_ten_second_shot_available(tmp_path):
    ceilings = routing.effective_job_ceilings(live_config(tmp_path, video_max_job_cost="10.00"))
    assert ceilings["wan"].amount == "10.00"
    preview = routing.preview(spec_request(duration=10), keys_configured=KEYS, job_ceilings=ceilings)
    wan = by_provider(preview, "wan")
    assert (wan.available, wan.amount) == (True, "6.00")
    assert preview.recommended == "wan"


def test_effective_ceilings_match_the_registration_path(tmp_path, store):
    for changes in (
        {},
        {"video_max_job_cost": "8.50", "kling_max_job_cost": "0.50", "kling_max_job_units": 2},
        {"fal_max_job_cost": "0.01"},
    ):
        config = live_config(tmp_path, **changes)
        service = JobService(store, [], config=config)
        currencies = {"wan": "CNY", "kling": "USD", "fal": "USD"}
        ceilings = routing.effective_job_ceilings(config)
        for provider in ("wan", "kling", "fal"):
            amount, _run_amount, units, _run_units = service.budget_ceiling(provider)
            assert ceilings[provider].amount == format(amount, ".2f")
            assert ceilings[provider].currency == currencies[provider]
            assert ceilings[provider].units == units


def test_route_preview_is_deterministic_and_order_independent():
    request = spec_request(mode="i2v")
    first = routing.preview(request, keys_configured=KEYS)
    second = routing.preview(request, keys_configured=KEYS)
    assert first.model_dump_json() == second.model_dump_json()
    reversed_table = tuple(reversed((*wan_shot_capabilities(), *multi_provider_capabilities())))
    third = routing.preview(request, capabilities=reversed_table, keys_configured=KEYS)
    assert third.model_dump_json() == first.model_dump_json()
    # The two fingerprints never contain time and both cover their own content.
    assert first.fingerprint == third.fingerprint
    assert all(len(item.quote_fingerprint) == 64 for item in first.candidates if item.available)
    assert (
        first.fingerprint
        != routing.preview(spec_request(mode="i2v", scope="episode"), keys_configured=KEYS).fingerprint
    )


def test_candidate_contract_rejects_a_partial_quote():
    with pytest.raises(ValueError):
        routing.RouteCandidate(
            provider="wan",
            model=WAN_MODEL,
            capabilities_version="wan27-shot-beijing-v2",
            price_version=WAN_PRICE_VERSION,
            currency="CNY",
            unit="output_second",
            quantity="5",
            amount=None,
            available=False,
            reason="VIDEO_COST_LIMIT",
            key_configured=True,
        )


def test_storyboard_preview_sums_each_shots_frozen_quote(tmp_path, store):
    registry, _, _, _ = mock_tools(store)
    saved = save_storyboard(
        registry,
        store,
        [shot("镜头一"), shot("镜头二"), shot("镜头三")],
    )
    config = live_config(tmp_path)

    def preview(shot_ids=(), scope="shot"):
        request = routing.make_request(
            scope=scope,
            storyboard_id=saved["storyboardId"],
            shot_ids=shot_ids,
        )
        return routing.preview_for(config, store.snapshot(), request)

    whole = preview()
    assert whole.request.storyboard_id == saved["storyboardId"]
    assert whole.request.storyboard_version == 1
    assert len(whole.request.shot_ids) == 3
    wan, fal, kling = whole.candidates
    assert (wan.amount, wan.quantity) == ("9.00", "15")
    assert (fal.amount, fal.quantity) == ("1.26", "15")
    assert (kling.amount, kling.quantity, kling.resource_units) == ("1.68", "15", 12)
    assert whole.recommended == "wan"
    assert not any("跨多个已核实条目" in note for note in whole.notes)

    one = preview(shot_ids=(saved["shotIds"][0],))
    assert (one.request.shot_ids, one.request.storyboard_version) == ((saved["shotIds"][0],), 1)
    assert by_provider(one, "wan").amount == "3.00"
    assert by_provider(one, "kling").amount == "0.56"


def test_a_mixed_mode_storyboard_sums_quotes_and_notes_the_mixed_entry(tmp_path, store):
    registry, _, _, _ = mock_tools(store)
    asset = insert_image(store)
    saved = save_storyboard(
        registry,
        store,
        [
            shot("文生镜头"),
            shot("图生镜头", mode="i2v", referenceAssetIds=[asset]),
        ],
    )
    config = live_config(tmp_path)
    request = routing.make_request(storyboard_id=saved["storyboardId"])
    preview = routing.preview_for(config, store.snapshot(), request)
    wan = by_provider(preview, "wan")
    assert (wan.available, wan.amount, wan.quantity) == (True, "6.00", "10")
    assert wan.model == WAN_MODEL  # the first shot's entry, stated in the note
    assert any("跨多个已核实条目" in note for note in preview.notes)
    assert by_provider(preview, "kling").reason == "VIDEO_UNSUPPORTED_MODE"


def test_storyboard_preview_rejects_unknown_shots_and_versions(tmp_path, store):
    registry, _, _, _ = mock_tools(store)
    saved = save_storyboard(registry, store, [shot("唯一镜头")])
    config = live_config(tmp_path)
    for request in (
        routing.make_request(storyboard_id=saved["storyboardId"], shot_ids=("not-a-shot",)),
        routing.make_request(storyboard_id=saved["storyboardId"], storyboard_version=9),
        routing.make_request(storyboard_id="no-such-storyboard"),
    ):
        with pytest.raises(AppError) as raised:
            routing.preview_for(config, store.snapshot(), request)
        assert raised.value.code == "ROUTE_UNAVAILABLE"


def test_preview_never_registers_a_job_or_writes_state(tmp_path, store):
    registry, _, _, _ = mock_tools(store)
    saved = save_storyboard(registry, store, [shot("镜头一"), shot("镜头二")])
    config = live_config(tmp_path)
    state_file = store.home / "state.json"
    before_bytes = state_file.read_bytes()
    before = json.dumps(store.snapshot(), sort_keys=True)
    request = routing.make_request(scope="episode", storyboard_id=saved["storyboardId"])
    routing.preview_for(config, store.snapshot(), request)
    assert json.dumps(store.snapshot(), sort_keys=True) == before
    assert state_file.read_bytes() == before_bytes
    assert not store.snapshot()["jobs"]


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"storyboard_id": "board", "mode": "t2v"}, "INVALID_ARGUMENTS"),
        ({}, "INVALID_ARGUMENTS"),
        ({"mode": "t2v", "duration_seconds": 5, "shot_ids": ["shot"]}, "INVALID_ARGUMENTS"),
        ({"mode": "t2v", "duration_seconds": 16}, "ROUTE_UNAVAILABLE"),
        ({"mode": "t2v", "duration_seconds": 5, "scope": "season"}, "ROUTE_UNAVAILABLE"),
        ({"mode": "sora", "duration_seconds": 5}, "ROUTE_UNAVAILABLE"),
        ({"mode": "i2v", "duration_seconds": 5, "aspect_ratio": "16:9"}, "ROUTE_UNAVAILABLE"),
        ({"mode": "t2v", "duration_seconds": 5, "aspect_ratio": "9:16"}, "ROUTE_UNAVAILABLE"),
        ({"mode": "t2v", "duration_seconds": 5, "provider": "sora"}, "ROUTE_UNAVAILABLE"),
    ],
)
def test_make_request_maps_bad_input_to_the_frozen_codes(kwargs, code):
    with pytest.raises(AppError) as raised:
        routing.make_request(**kwargs)
    assert raised.value.code == code


def test_a_text_to_video_spec_defaults_to_the_frozen_sixteen_by_nine_frame():
    assert routing.make_request(mode="t2v", duration_seconds=5).aspect_ratio == "16:9"
    assert routing.make_request(mode="i2v", duration_seconds=5).aspect_ratio is None


def test_provider_verification_is_a_static_label():
    for provider in ("wan", "kling", "fal"):
        assert routing.provider_verification(provider) == "documented-no-real-sample"


def test_frozen_defaults_match_the_saved_settings(tmp_path):
    """The preview's defaults are the same published table and ceilings as the app."""
    config = live_config(tmp_path)
    assert routing.DEFAULT_JOB_CEILINGS["wan"].amount == config.video_max_job_cost
    assert routing.DEFAULT_JOB_CEILINGS["kling"].units == 4
    assert Decimal(routing.DEFAULT_JOB_CEILINGS["fal"].amount) == Decimal("0.50")
    preview = routing.preview(spec_request(), keys_configured=KEYS)
    assert [item.provider for item in preview.candidates] == ["wan", "fal", "kling"]


def test_a_preview_echoes_its_request_fields():
    request = spec_request(mode="flf2v", duration=8, scope="batch")
    preview = routing.preview(request, keys_configured=KEYS)
    dumped = preview.model_dump(mode="json", by_alias=True)
    assert dumped["contract"] == "provider-route-v1"
    assert dumped["request"] == {
        "scope": "batch",
        "mode": "flf2v",
        "durationSeconds": 8,
        "aspectRatio": None,
        "storyboardId": None,
        "storyboardVersion": None,
        "shotIds": [],
        "provider": None,
    }
    assert dumped["videoMode"] == "live"
    assert dumped["scope"] == "batch"


def test_a_tightened_fal_ceiling_blocks_its_quote(tmp_path):
    config = live_config(tmp_path, fal_max_job_cost="0.10")
    request = spec_request()
    preview = routing.preview_for(config, {}, request)
    fal = by_provider(preview, "fal")
    assert (fal.available, fal.reason, fal.amount) == (False, "VIDEO_COST_LIMIT", "0.42")
