"""M2 D3 asset library: import validation, dedup, safe paths and verified delivery."""

import hashlib
import json

import pytest
from shot_support import jpeg_bytes, mock_tools, png_bytes

from vagent.errors import AppError
from vagent.video.assets import ASSET_LIST_LIMIT, AssetFiles, AssetService
from vagent.video.contracts import IMAGE_MAX_BYTES

TOOL_FIELDS = {
    "assetId",
    "projectId",
    "kind",
    "mimeType",
    "sizeBytes",
    "dimensions",
    "createdAt",
    "source",
    "assetFormatVersion",
}


@pytest.fixture
def library(store):
    store.ensure_session("coffee")
    store.ensure_session("other")
    return AssetService(store)


def test_import_stores_server_named_files_and_dedupes_per_project(store, library):
    data = png_bytes(1280, 720)
    first = library.import_bytes(data, project_id="coffee")
    assert first["deduplicated"] is False
    assert first["mimeType"] == "image/png" and first["sizeBytes"] == len(data)
    assert first["sha256"] == hashlib.sha256(data).hexdigest()
    assert first["dimensions"] == {"width": 1280, "height": 720}
    record = store.snapshot()["assets"][first["assetId"]]
    assert record["relativePath"] == f"assets/coffee/{first['assetId']}.png"
    assert (store.home / record["relativePath"]).read_bytes() == data
    again = library.import_bytes(data, project_id="coffee")
    assert again["assetId"] == first["assetId"] and again["deduplicated"] is True
    assert len(store.snapshot()["assets"]) == 1
    # The library exists so another project can reuse the same bytes under its own ID.
    other = library.import_bytes(data, project_id="other")
    assert other["assetId"] != first["assetId"] and other["deduplicated"] is False
    assert other["sha256"] == first["sha256"] and len(store.snapshot()["assets"]) == 2
    jpeg = library.import_bytes(jpeg_bytes(1280, 720), project_id="coffee")
    assert jpeg["mimeType"] == "image/jpeg" and jpeg["deduplicated"] is False
    assert store.snapshot()["assets"][jpeg["assetId"]]["relativePath"].endswith(".jpg")


def test_a_duplicate_never_rewrites_the_stored_file(store, library):
    data = png_bytes(1280, 720)
    first = library.import_bytes(data, project_id="coffee")
    path = store.home / store.snapshot()["assets"][first["assetId"]]["relativePath"]
    before = path.stat().st_mtime_ns
    assert library.import_bytes(data, project_id="coffee")["deduplicated"] is True
    assert path.stat().st_mtime_ns == before


@pytest.mark.parametrize(
    ("data", "code"),
    [
        pytest.param(png_bytes(1280, 720, color_type=6), "IMAGE_ALPHA_UNSUPPORTED", id="alpha"),
        pytest.param(png_bytes(1280, 720, extra=b"\x00" * 6), "IMAGE_ALPHA_UNSUPPORTED", id="tRNS"),
        pytest.param(png_bytes(100, 720), "IMAGE_SIDE_OUT_OF_RANGE", id="short-side"),
        pytest.param(png_bytes(8000, 800), "IMAGE_ASPECT_OUT_OF_RANGE", id="aspect"),
        pytest.param(b"GIF89a" + b"\x00" * 32, "IMAGE_FORMAT_UNSUPPORTED", id="gif"),
        pytest.param(b"", "ASSET_EMPTY", id="empty"),
        pytest.param(b"x" * (IMAGE_MAX_BYTES + 1), "IMAGE_TOO_LARGE", id="too-large"),
    ],
)
def test_invalid_images_never_enter_the_library(store, library, data, code):
    with pytest.raises(AppError) as caught:
        library.import_bytes(data, project_id="coffee")
    assert caught.value.code == code
    assert not store.snapshot()["assets"]
    assert not (store.home / "assets" / "coffee").exists()


def test_import_path_reads_one_local_file_and_reports_problems(tmp_path, store, library):
    source = tmp_path / "reference.png"
    source.write_bytes(png_bytes(1280, 720))
    imported = library.import_path(source, project_id="coffee")
    assert imported["mimeType"] == "image/png" and imported["deduplicated"] is False
    assert library.import_path(source, project_id="coffee")["deduplicated"] is True
    with pytest.raises(AppError) as caught:
        library.import_path(tmp_path / "absent.png", project_id="coffee")
    assert caught.value.code == "ASSET_IMPORT_UNREADABLE"
    folder = tmp_path / "folder"
    folder.mkdir()
    with pytest.raises(AppError) as caught:
        library.import_path(folder, project_id="coffee")
    assert caught.value.code == "ASSET_IMPORT_UNREADABLE"
    with pytest.raises(AppError) as caught:
        library.import_bytes(png_bytes(1280, 720), project_id="absent")
    assert caught.value.code == "NOT_FOUND"


def test_availability_reports_missing_and_corrupt_files(store, library):
    asset_id = library.import_bytes(png_bytes(1280, 720), project_id="coffee")["assetId"]
    view = library.view(asset_id)
    assert view["available"] and view["contentUrl"] == f"/api/assets/{asset_id}/content"
    assert view["availability"] == {"status": "available", "reason": None}
    asset, lease = library.open_content(asset_id)
    with lease as file:
        assert file.read() == png_bytes(1280, 720)
    path = store.home / store.snapshot()["assets"][asset_id]["relativePath"]
    damaged = bytearray(png_bytes(1280, 720))
    damaged[-5] ^= 0xFF
    path.write_bytes(bytes(damaged))
    assert library.availability(asset) == ("unavailable", "hash_mismatch")
    with pytest.raises(AppError) as caught:
        library.open_content(asset_id)
    assert caught.value.code == "ASSET_UNAVAILABLE"
    path.unlink()
    assert library.availability(asset) == ("unavailable", "missing")
    assert library.view(asset_id)["contentUrl"] is None
    with pytest.raises(AppError) as caught:
        library.asset("44444444-4444-4444-4444-444444444444")
    assert caught.value.code == "ASSET_NOT_FOUND"


def test_asset_paths_are_server_generated_only(store):
    store.ensure_session("coffee")
    (store.home / "assets" / "coffee").mkdir(parents=True)
    files = AssetFiles(store.home)
    for relative in (
        "assets/coffee/../state.json",
        "assets/coffee/not-a-uuid.png",
        "assets/coffee/abcd.png",
        "media/coffee/44444444-4444-4444-4444-444444444444.mp4",
        "assets/coffee/44444444-4444-4444-4444-444444444444.exe",
        "assets\\coffee\\44444444-4444-4444-4444-444444444444.png",
        "assets/coffee/44444444-4444-4444-4444-444444444444.png.mp4",
    ):
        with pytest.raises(AppError) as caught:
            with files.parent(relative):
                pass
        assert caught.value.code == "ASSET_SOURCE_UNSAFE", relative
    with files.parent("assets/coffee/44444444-4444-4444-4444-444444444444.png"):
        pass
    files.write("assets/coffee/44444444-4444-4444-4444-444444444444.png", b"x")
    with pytest.raises(AppError) as caught:
        files.write("assets/coffee/44444444-4444-4444-4444-444444444444.png", b"y")
    assert caught.value.code == "ASSET_FILE_CONFLICT"
    assert (
        store.home / "assets" / "coffee" / "44444444-4444-4444-4444-444444444444.png"
    ).read_bytes() == b"x"


def test_asset_read_tool_exposes_metadata_without_paths_or_fingerprints(store, video_clock):
    registry, _, _, _ = mock_tools(store, clock=video_clock)
    store.ensure_session("other")
    library = AssetService(store)
    coffee = library.import_bytes(png_bytes(1280, 720), project_id="coffee")
    other = library.import_bytes(jpeg_bytes(1280, 720), project_id="other")
    listing = registry.execute("asset_read", {}, store=store, project_id="coffee", operation_key="asset-read")
    assert listing["ok"], listing
    data = listing["data"]
    assert data["callerProject"] == "coffee" and data["truncated"] is False and data["total"] == 2
    # The library is directory-wide on purpose: another project's images stay reusable.
    assert {item["assetId"] for item in data["assets"]} == {coffee["assetId"], other["assetId"]}
    assert set(data["assets"][0]) == TOOL_FIELDS
    text = json.dumps(data, ensure_ascii=False)
    for secret in ("relativePath", "sha256", "assets/", str(store.home)):
        assert secret not in text
    single = registry.execute(
        "asset_read",
        {"assetId": coffee["assetId"]},
        store=store,
        project_id="coffee",
        operation_key="asset-one",
    )
    assert single["data"]["asset"]["assetId"] == coffee["assetId"]
    filtered = registry.execute(
        "asset_read",
        {"projectId": "other"},
        store=store,
        project_id="coffee",
        operation_key="asset-filter",
    )
    assert [item["assetId"] for item in filtered["data"]["assets"]] == [other["assetId"]]
    absent = registry.execute(
        "asset_read",
        {"assetId": "44444444-4444-4444-4444-444444444444"},
        store=store,
        project_id="coffee",
        operation_key="asset-absent",
    )
    assert absent["error"]["code"] == "ASSET_NOT_FOUND"
    before = store.snapshot()
    assert registry.read_only().execute(
        "asset_read", {}, store=store, project_id="coffee", operation_key="asset-readonly"
    )["ok"]
    after = store.snapshot()
    assert after["assets"] == before["assets"] and after["storyboards"] == before["storyboards"]


def test_asset_listing_is_bounded_and_newest_first(store, video_clock):
    registry, _, _, _ = mock_tools(store, clock=video_clock)
    library = AssetService(store)
    imported = [
        library.import_bytes(png_bytes(240 + index, 240), project_id="coffee")["assetId"]
        for index in range(ASSET_LIST_LIMIT + 1)
    ]
    listing = registry.execute(
        "asset_read", {}, store=store, project_id="coffee", operation_key="asset-page"
    )["data"]
    assert listing["total"] == ASSET_LIST_LIMIT + 1 and listing["truncated"] is True
    assert len(listing["assets"]) == ASSET_LIST_LIMIT
    assert listing["assets"][0]["assetId"] == imported[-1]
    assert library.list(project_id="coffee")[0]["assetId"] == imported[-1]
    assert len(library.list()) == ASSET_LIST_LIMIT + 1


def test_imported_assets_stay_immutable_and_referenceable(store, video_clock):
    registry, _, _, _ = mock_tools(store, clock=video_clock)
    library = AssetService(store)
    asset_id = library.import_bytes(png_bytes(1280, 720), project_id="coffee")["assetId"]
    saved = registry.execute(
        "storyboard_save",
        {
            "title": "i2v 分镜",
            "shots": [
                {
                    "description": "首帧起手",
                    "framing": "wide",
                    "durationSeconds": 5,
                    "mode": "i2v",
                    "referenceAssetIds": [asset_id],
                }
            ],
        },
        store=store,
        project_id="coffee",
        operation_key="asset-storyboard",
    )
    assert saved["ok"], saved
    with pytest.raises(AppError) as caught:
        store.transaction(lambda draft: draft["assets"].pop(asset_id))
    # A referenced asset cannot vanish: the storyboard validator reports it first.
    assert caught.value.code == "ASSET_NOT_FOUND"
    spare = library.import_bytes(jpeg_bytes(1280, 720), project_id="coffee")["assetId"]
    with pytest.raises(AppError) as caught:
        store.transaction(lambda draft: draft["assets"].pop(spare))
    assert caught.value.code == "ASSET_IMMUTABLE"
    assert set(store.snapshot()["assets"]) == {asset_id, spare}
