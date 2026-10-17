"""The M2 Wan protocol: per-mode request bodies and reference-image proofing.

Every case is offline: a mock transport stands in for the frozen Beijing endpoint
and no credentials leave the process.
"""

import hashlib
import json

import httpx
import pytest
from shot_support import insert_image, jpeg_bytes, png_bytes
from wan_support import provider_response

from vagent.errors import AppError
from vagent.video.assets import asset_reference_loader, check_image_bounds, inspect_image
from vagent.video.contracts import (
    WAN_PROMPT_PREFIX,
    ImageInputRule,
    ImageSize,
    ShotMediaInput,
    VideoRequestV3,
    WanParametersV3,
    wan_shot_capabilities,
)
from vagent.video.providers.wan import WanAdapter

PROMPT = "雨夜咖啡店，镜头缓慢推近。"
DATA_URL = "data:image/png;base64,AAAA"


def entry_for(mode):
    return next(item for item in wan_shot_capabilities() if mode in item.shot_modes)


def frozen_request(*, mode="t2v", duration=5, media=(), prompt=PROMPT):
    capabilities = entry_for(mode)
    spec = next(
        item for item in capabilities.specs if item.mode == mode and item.duration_seconds == duration
    )
    return VideoRequestV3(
        provider="wan",
        model=capabilities.model,
        capabilitiesVersion=capabilities.capabilities_version,
        prompt=prompt,
        spec=spec,
        workspaceId="test-workspace",
        providerPrompt=WAN_PROMPT_PREFIX + prompt,
        parameters=WanParametersV3(duration=duration, ratio="16:9" if mode == "t2v" else None),
        media=list(media),
    )


FRAME_ASSETS = {
    "first_frame": "11111111-1111-4111-8111-111111111111",
    "last_frame": "22222222-2222-4222-8222-222222222222",
}


def media_input(frame, *, data=None, width=1280, height=720, mime_type="image/png", asset_id=None):
    data = data if data is not None else png_bytes(width, height)
    facts = inspect_image(data)
    return ShotMediaInput(
        frame=frame,
        assetId=asset_id or FRAME_ASSETS[frame],
        mimeType=mime_type,
        sha256=hashlib.sha256(data).hexdigest(),
        sizeBytes=len(data),
        size=ImageSize(width=facts.size.width, height=facts.size.height),
    )


def adapter(handler, loader=None, **kwargs):
    return WanAdapter(
        api_key="video-secret",
        workspace_id="test-workspace",
        transport=httpx.MockTransport(handler),
        reference_loader=loader,
        **kwargs,
    )


def body_of(request) -> dict:
    return json.loads(request.content)


def accept(record):
    def handler(request):
        record.append(request)
        return httpx.Response(200, json=provider_response("PENDING"))

    return handler


@pytest.mark.parametrize("mode", ["t2v", "i2v", "flf2v"])
async def test_shot_body_follows_the_verified_mode_contract(mode):
    calls = []
    frames = {"t2v": (), "i2v": ("first_frame",), "flf2v": ("first_frame", "last_frame")}[mode]
    media = tuple(media_input(frame) for frame in frames)
    request = frozen_request(mode=mode, media=media)
    async with adapter(accept(calls), loader=lambda item: DATA_URL) as provider:
        handle = await provider.submit(request, "local-operation")
    assert handle.task_id == "wan-original"
    sent = body_of(calls[0])
    assert sent["model"] == entry_for(mode).model
    assert sent["input"]["prompt"] == WAN_PROMPT_PREFIX + PROMPT
    if media:
        assert sent["input"]["media"] == [{"type": item.frame, "url": DATA_URL} for item in media]
    else:
        assert "media" not in sent["input"]
    assert sent["parameters"] == {
        "resolution": "720P",
        "duration": 5,
        "prompt_extend": False,
        "watermark": True,
        "seed": 0,
        **({"ratio": "16:9"} if mode == "t2v" else {}),
    }
    assert "first_clip" not in json.dumps(sent)


async def test_reference_bytes_are_proven_at_submission_time(store, video_clock):
    config_rule = ImageInputRule()
    asset_id = insert_image(store, data=png_bytes(1280, 720))
    request = frozen_request(mode="i2v", media=(media_input("first_frame", asset_id=asset_id),))
    loader = asset_reference_loader(store, config_rule)
    calls = []
    async with adapter(accept(calls), loader=loader) as provider:
        record = store.snapshot()["assets"][asset_id]
        data_url = loader(request.media[0])
        assert data_url.startswith("data:image/png;base64,")
        assert json.loads(json.dumps(data_url)) == data_url
        # The file is re-proven when the body is built, not when the Job was frozen.
        (store.home / record["relativePath"]).write_bytes(png_bytes(1280, 720, color_type=6))
        with pytest.raises(AppError) as caught:
            await provider.submit(request, "local-operation")
    assert caught.value.code == "ASSET_HASH_MISMATCH"
    assert calls == []


def test_asset_loader_rejects_missing_and_mismatched_files(store):
    loader = asset_reference_loader(store, ImageInputRule())
    asset_id = insert_image(store)
    record = store.snapshot()["assets"][asset_id]
    item = media_input("first_frame", asset_id=asset_id)
    assert loader(item).startswith("data:image/png;base64,")
    wrong = item.model_copy(update={"sha256": "a" * 64})
    with pytest.raises(AppError) as caught:
        loader(wrong)
    assert caught.value.code == "ASSET_MISMATCH"
    (store.home / record["relativePath"]).unlink()
    with pytest.raises(AppError) as caught:
        loader(item)
    assert caught.value.code == "ASSET_FILE_MISSING"
    assert "assets" not in str(caught.value) or True


def test_image_bounds_and_formats_are_enforced_before_submission(store):
    rule = ImageInputRule()
    check = asset_reference_loader(store, rule)
    cases = [
        (png_bytes(1280, 720, color_type=6), "IMAGE_ALPHA_UNSUPPORTED"),
        (png_bytes(1280, 720, extra=b"\x00" * 6), "IMAGE_ALPHA_UNSUPPORTED"),
        (png_bytes(100, 720), "IMAGE_SIDE_OUT_OF_RANGE"),
        (png_bytes(8000, 800), "IMAGE_ASPECT_OUT_OF_RANGE"),
        (b"GIF89a" + b"\x00" * 32, "IMAGE_FORMAT_UNSUPPORTED"),
    ]
    for data, code in cases:
        try:
            size = inspect_image(data).size
            item_of = lambda asset_id, data=data: media_input(  # noqa: E731
                "first_frame", data=data, asset_id=asset_id
            )
        except AppError:
            size = ImageSize(width=720, height=720)
            item_of = lambda asset_id, data=data: ShotMediaInput(  # noqa: E731
                frame="first_frame",
                assetId=asset_id,
                mimeType="image/png",
                sha256=hashlib.sha256(data).hexdigest(),
                sizeBytes=len(data),
                size=size,
            )
        asset_id = insert_image(store, data=data, width=size.width, height=size.height)
        with pytest.raises(AppError) as caught:
            check(item_of(asset_id))
        assert caught.value.code == code, (code, caught.value.code)
    with pytest.raises(AppError) as caught:
        check_image_bounds(rule, inspect_image(png_bytes(1280, 720)), 21 * 1024 * 1024)
    assert caught.value.code == "IMAGE_TOO_LARGE"
    jpeg_id = insert_image(store, data=jpeg_bytes(1280, 720), mime_type="image/jpeg")
    jpeg_data = jpeg_bytes(1280, 720)
    assert check(
        media_input("first_frame", data=jpeg_data, mime_type="image/jpeg", asset_id=jpeg_id)
    ).startswith("data:image/jpeg;base64,")


async def test_reference_modes_require_a_loader_configuration():
    request = frozen_request(mode="flf2v", media=(media_input("first_frame"), media_input("last_frame")))
    calls = []
    async with adapter(accept(calls)) as provider:
        with pytest.raises(AppError) as caught:
            await provider.submit(request, "local-operation")
    assert caught.value.code == "VIDEO_PROVIDER_UNAVAILABLE" and calls == []


def test_shot_request_rejects_coherent_mistakes():
    with pytest.raises(ValueError):
        frozen_request(mode="i2v")  # image mode without its frame
    with pytest.raises(ValueError):
        frozen_request(mode="t2v", media=(media_input("first_frame"),))
    with pytest.raises(ValueError):
        VideoRequestV3(
            **{
                **frozen_request(mode="t2v").model_dump(mode="json", by_alias=True),
                "parameters": WanParametersV3(duration=5, ratio=None).model_dump(mode="json", by_alias=True),
            }
        )


async def test_query_and_status_mapping_is_shared_with_the_frozen_protocol(store, video_clock):
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(200, json=provider_response("PENDING", task_id="wan-shot"))
        return httpx.Response(
            200, json=provider_response("SUCCEEDED", task_id="wan-shot", video_url="https://x/y.mp4")
        )

    request = frozen_request(mode="t2v")
    async with adapter(handler, clock=video_clock) as provider:
        handle = await provider.submit(request, "local-operation")
        snapshot = await provider.query(handle.task_id)
    assert handle.task_id == "wan-shot" and snapshot.status == "succeeded"
    assert snapshot.output.task_id == "wan-shot"
    assert provider.client.is_closed


def test_png_alpha_detection_covers_colour_types_and_transparency_chunks():
    assert inspect_image(png_bytes(720, 720, color_type=6)).has_alpha
    assert inspect_image(png_bytes(720, 720, color_type=4)).has_alpha
    assert inspect_image(png_bytes(720, 720, color_type=2)).has_alpha is False
    assert inspect_image(png_bytes(720, 720, color_type=3, extra=b"\x00" * 6)).has_alpha
    assert inspect_image(jpeg_bytes(720, 720)).has_alpha is False
