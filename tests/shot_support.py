"""Shared M2 fixtures: storyboard saves, real reference images and shot registration."""

import hashlib
import struct
from uuid import uuid4

from conftest import VideoClock, video_run

from vagent.tools import create_project_tools
from vagent.video.assets import ImageFacts, data_url
from vagent.video.jobs import JobService
from vagent.video.providers.mock import MockVideoAdapter
from vagent.video.storyboards import ASSET_EXTENSIONS, Asset, ImageDimensions
from vagent.video.tools import register_video_tools
from vagent.waiting import ToolExecutionContext


def chunk(kind, payload):
    return struct.pack(">I", len(payload)) + kind + payload + b"\x00\x00\x00\x00"


def png_bytes(width, height, color_type=2, extra=b""):
    """A structurally valid PNG header; no pixel data is needed by the inspector."""
    ihdr = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    body = chunk(b"IHDR", ihdr)
    if extra:
        body += chunk(b"tRNS", extra)
    return b"\x89PNG\r\n\x1a\n" + body + chunk(b"IEND", b"")


def jpeg_bytes(width, height):
    """SOI plus one baseline SOF0 frame header with a single component."""
    sof = struct.pack(">BHHB", 8, height, width, 1) + b"\x00\x00\x00"
    return b"\xff\xd8" + struct.pack(">BBH", 0xFF, 0xC0, len(sof) + 2) + sof + b"\xff\xd9"


def insert_image(
    store,
    *,
    project_id="coffee",
    width=1280,
    height=720,
    color_type=2,
    mime_type="image/png",
    data=None,
    sha256=None,
    size_bytes=None,
):
    """Write one real reference file plus its immutable asset record."""
    store.ensure_session(project_id)
    asset_id = str(uuid4())
    if data is None:
        data = png_bytes(width, height, color_type) if mime_type == "image/png" else jpeg_bytes(width, height)
    directory = store.home / "assets" / project_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{asset_id}.{ASSET_EXTENSIONS[mime_type]}").write_bytes(data)
    asset = Asset(
        id=asset_id,
        project_id=project_id,
        relative_path=f"assets/{project_id}/{asset_id}.{ASSET_EXTENSIONS[mime_type]}",
        mime_type=mime_type,
        size_bytes=len(data) if size_bytes is None else size_bytes,
        sha256=hashlib.sha256(data).hexdigest() if sha256 is None else sha256,
        dimensions=ImageDimensions(width=width, height=height),
        created_at="2030-01-01T00:00:00+00:00",
    )
    store.transaction(
        lambda draft: draft["assets"].__setitem__(asset_id, asset.model_dump(mode="json", by_alias=True))
    )
    return asset_id


def shot(description="雨夜咖啡店窗外", **changes):
    return {"description": description, "framing": "wide", "durationSeconds": 5, "mode": "t2v", **changes}


def save_storyboard(registry, store, shots, *, project_id="coffee", key="storyboard-save", title="分镜"):
    result = registry.execute(
        "storyboard_save",
        {"title": title, "shots": shots},
        store=store,
        project_id=project_id,
        operation_key=key,
    )
    assert result["ok"], result
    return result["data"]


def revise_storyboard(registry, store, saved, shots, *, project_id="coffee", key="storyboard-revise"):
    result = registry.execute(
        "storyboard_save",
        {
            "storyboardId": saved["storyboardId"],
            "expectedVersion": saved["version"],
            "title": "分镜",
            "shots": shots,
        },
        store=store,
        project_id=project_id,
        operation_key=key,
    )
    assert result["ok"], result
    return result["data"]


def generation_args(saved, shot_ids=None, *, confirm=True, version=None):
    return {
        "storyboardId": saved["storyboardId"],
        "storyboardVersion": saved["version"] if version is None else version,
        "shotIds": list(saved["shotIds"] if shot_ids is None else shot_ids),
        "confirm": confirm,
    }


def redo_args(saved, shot_id, *, revision=1, client_request_id="redo-1", version=None):
    return {
        "storyboardId": saved["storyboardId"],
        "storyboardVersion": saved["version"] if version is None else version,
        "shotId": shot_id,
        "expectedRevision": revision,
        "clientRequestId": client_request_id,
    }


def mock_tools(store, *, clock=None):
    store.ensure_session("coffee")
    context = video_run(store)
    adapter = MockVideoAdapter(store, clock=clock or VideoClock())
    service = JobService(store, [adapter], clock=clock or VideoClock())
    return register_video_tools(create_project_tools(), service), service, adapter, context


def m2_run(store, run_id="m2-run", *, project_id="coffee", mode="mock", read_only=False):
    """A Run recorded by the M2 tool set: batch registration is allowed inside it."""
    context = video_run(store, run_id, project_id=project_id, read_only=read_only)

    def upgrade(draft):
        draft["runs"][run_id].update(
            videoMode=mode,
            executionVersion=3,
            toolFeatures={
                "video": {
                    "mode": mode,
                    "toolsVersion": 4,
                    "rulesVersion": 4,
                    "capabilities": [],
                }
            },
        )

    store.transaction(upgrade)
    return context


def next_context(context, call_id, **changes):
    return ToolExecutionContext.model_validate(
        {**context.model_dump(by_alias=True), "toolCallId": call_id, **changes}
    )


def storyboard_record(store, storyboard_id):
    return store.snapshot()["storyboards"][storyboard_id]


def shot_versions(store, storyboard_id, shot_id):
    record = storyboard_record(store, storyboard_id)
    return [item for item in record["shotVersions"] if item["shotId"] == shot_id]


def jobs_of(store, run_id):
    return [raw for raw in store.snapshot()["jobs"].values() if raw["context"]["runId"] == run_id]


def reference_data_url(facts: ImageFacts, data: bytes) -> str:
    return data_url(facts.mime_type, data)
