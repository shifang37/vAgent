"""Storyboard domain and tool contracts: versions, conflicts, scope and replay."""

from uuid import uuid4

import pytest

from vagent.errors import AppError
from vagent.tools import create_project_tools
from vagent.video.jobs import JobService
from vagent.video.providers.mock import MockVideoAdapter
from vagent.video.tools import register_video_tools


def mock_registry(store):
    store.ensure_session("coffee")
    return register_video_tools(create_project_tools(), JobService(store, [MockVideoAdapter(store)]))


def execute(registry, store, name, args, key="tool-call-1", project_id="coffee"):
    return registry.execute(name, args, store=store, project_id=project_id, operation_key=key)


def shot(**changes):
    return {
        "description": "雨夜咖啡店的窗外",
        "framing": "wide",
        "durationSeconds": 5,
        "mode": "t2v",
        **changes,
    }


def save(registry, store, args, key="tool-call-1", project_id="coffee"):
    result = execute(registry, store, "storyboard_save", args, key=key, project_id=project_id)
    assert result["ok"], result
    return result["data"]


def insert_asset(store, *, project_id, sha256=None, mime_type="image/png"):
    from vagent.video.storyboards import ASSET_EXTENSIONS, Asset, ImageDimensions

    asset_id = str(uuid4())
    asset = Asset(
        id=asset_id,
        project_id=project_id,
        relative_path=f"assets/{project_id}/{asset_id}.{ASSET_EXTENSIONS[mime_type]}",
        mime_type=mime_type,
        size_bytes=1024,
        sha256=sha256 or uuid4().hex * 2,
        dimensions=ImageDimensions(width=1280, height=720),
        created_at="2030-01-01T00:00:00+00:00",
    )
    store.transaction(
        lambda draft: draft["assets"].__setitem__(asset_id, asset.model_dump(mode="json", by_alias=True))
    )
    return asset_id


def test_save_creates_stable_ids_and_append_only_versions(store):
    registry = mock_registry(store)
    first = save(
        registry,
        store,
        {
            "title": "雨夜咖啡分镜",
            "shots": [
                shot(description="远景：雨夜街道"),
                shot(description="中景：推门入店", framing="medium", cameraMovement="缓慢推近"),
                shot(description="特写：拉花", framing="close", durationSeconds=3),
            ],
        },
    )
    assert first["version"] == 1 and len(first["shotIds"]) == 3 and len(set(first["shotIds"])) == 3
    read = execute(registry, store, "storyboard_read", {}, key="read-1")["data"]
    assert read["storyboardId"] == first["storyboardId"] and read["currentVersion"] == 1
    assert read["versionCount"] == 1 and read["revision"] == 0
    assert [item["order"] for item in read["shots"]] == [1, 2, 3]
    assert read["shots"][1]["cameraMovement"] == "缓慢推近"
    assert read["shots"][2]["durationSeconds"] == 3
    assert all(item["versions"] == [] for item in read["shots"])
    updated = save(
        registry,
        store,
        {
            "storyboardId": first["storyboardId"],
            "expectedVersion": 1,
            "title": "雨夜咖啡分镜",
            "shots": [
                {"id": first["shotIds"][0], **shot(description="远景：雨夜街道与霓虹")},
                {"id": first["shotIds"][1], **shot(description="中景：推门入店", framing="medium")},
            ],
        },
        key="tool-call-2",
    )
    assert updated["version"] == 2 and updated["shotIds"] == first["shotIds"][:2]
    state = store.snapshot()
    record = state["storyboards"][first["storyboardId"]]
    assert record["currentVersion"] == 2 and record["revision"] == 1
    assert [item["description"] for item in record["versions"][0]["shots"]] == [
        "远景：雨夜街道",
        "中景：推门入店",
        "特写：拉花",
    ]
    read = execute(registry, store, "storyboard_read", {"storyboardId": first["storyboardId"]}, key="read-2")[
        "data"
    ]
    assert read["currentVersion"] == 2 and read["versionCount"] == 2
    assert [item["id"] for item in read["shots"]] == first["shotIds"][:2]


def test_stale_expected_version_is_rejected_without_side_effects(store):
    registry = mock_registry(store)
    first = save(registry, store, {"title": "分镜", "shots": [shot()]})
    save(
        registry,
        store,
        {
            "storyboardId": first["storyboardId"],
            "expectedVersion": 1,
            "title": "分镜",
            "shots": [shot(description="改过的镜头")],
        },
        key="update-1",
    )
    before = store.snapshot()
    stale = execute(
        registry,
        store,
        "storyboard_save",
        {
            "storyboardId": first["storyboardId"],
            "expectedVersion": 1,
            "title": "分镜",
            "shots": [shot(description="再来一版")],
        },
        key="update-2",
    )
    assert stale["error"]["code"] == "VERSION_CONFLICT"
    assert store.snapshot()["storyboards"] == before["storyboards"]
    assert len(store.snapshot()["storyboards"][first["storyboardId"]]["versions"]) == 2


def test_replayed_save_returns_the_same_version_without_appending(store):
    registry = mock_registry(store)
    args = {"title": "分镜", "shots": [shot()]}
    first = save(registry, store, args, key="same-call")
    replay = execute(registry, store, "storyboard_save", args, key="same-call")
    assert replay == {"ok": True, "data": first}
    record = next(iter(store.snapshot()["storyboards"].values()))
    assert len(record["versions"]) == 1
    conflict = execute(registry, store, "storyboard_save", {**args, "title": "改名"}, key="same-call")
    assert conflict["error"]["code"] == "OPERATION_CONFLICT"
    assert len(store.snapshot()["storyboards"][record["id"]]["versions"]) == 1


def test_unknown_and_duplicate_shot_ids_are_rejected(store):
    registry = mock_registry(store)
    first = save(registry, store, {"title": "分镜", "shots": [shot(), shot(description="第二个镜头")]})
    unknown = execute(
        registry,
        store,
        "storyboard_save",
        {
            "storyboardId": first["storyboardId"],
            "expectedVersion": 1,
            "title": "分镜",
            "shots": [{"id": str(uuid4()), **shot()}],
        },
        key="update-unknown",
    )
    assert unknown["error"]["code"] == "SHOT_NOT_FOUND"
    duplicate = execute(
        registry,
        store,
        "storyboard_save",
        {
            "storyboardId": first["storyboardId"],
            "expectedVersion": 1,
            "title": "分镜",
            "shots": [
                {"id": first["shotIds"][0], **shot()},
                {"id": first["shotIds"][0], **shot(description="重复 ID")},
            ],
        },
        key="update-duplicate",
    )
    assert duplicate["error"]["code"] == "SHOT_DUPLICATE"


def test_cross_project_storyboards_are_rejected(store):
    registry = mock_registry(store)
    store.ensure_session("other")
    other = save(
        registry,
        store,
        {"title": "他项目分镜", "shots": [shot()]},
        key="other-create",
        project_id="other",
    )
    empty = execute(registry, store, "storyboard_read", {}, key="empty-read")
    assert empty["error"]["code"] == "NOT_FOUND"
    cross_read = execute(
        registry, store, "storyboard_read", {"storyboardId": other["storyboardId"]}, key="cross-read"
    )
    assert cross_read["error"]["code"] == "NOT_FOUND"
    cross_write = execute(
        registry,
        store,
        "storyboard_save",
        {
            "storyboardId": other["storyboardId"],
            "expectedVersion": 1,
            "title": "越界修改",
            "shots": [shot()],
        },
        key="cross-write",
    )
    assert cross_write["error"]["code"] == "NOT_FOUND"
    # The other project still reads its own storyboard.
    own = execute(
        registry,
        store,
        "storyboard_read",
        {"storyboardId": other["storyboardId"]},
        key="own-read",
        project_id="other",
    )
    assert own["ok"] and own["data"]["title"] == "他项目分镜"


def test_source_refs_are_scoped_and_frozen(store):
    registry = mock_registry(store)
    tools = create_project_tools()
    source = execute(tools, store, "artifact_save", {"kind": "brief", "title": "来源", "content": "第一版"})[
        "data"
    ]["artifactId"]
    missing = execute(
        registry,
        store,
        "storyboard_save",
        {"title": "分镜", "shots": [shot()], "sourceRefs": [{"artifactId": source, "version": 9}]},
        key="missing-version",
    )
    assert missing["error"]["code"] == "SOURCE_VERSION_NOT_FOUND"
    store.ensure_session("other")
    foreign_source = execute(
        tools,
        store,
        "artifact_save",
        {"kind": "brief", "title": "别家", "content": "外部"},
        key="other-source",
        project_id="other",
    )["data"]["artifactId"]
    foreign = execute(
        registry,
        store,
        "storyboard_save",
        {"title": "分镜", "shots": [shot()], "sourceRefs": [{"artifactId": foreign_source, "version": 1}]},
        key="foreign-source",
    )
    assert foreign["error"]["code"] == "SOURCE_NOT_FOUND"
    saved = save(
        registry,
        store,
        {"title": "分镜", "shots": [shot()], "sourceRefs": [{"artifactId": source, "version": 1}]},
        key="with-source",
    )
    read = execute(
        registry, store, "storyboard_read", {"storyboardId": saved["storyboardId"]}, key="read-source"
    )["data"]
    assert read["sourceRefs"] == [{"artifactId": source, "version": 1}]
    # An omitted sourceRefs on update keeps the validated original references.
    save(
        registry,
        store,
        {
            "storyboardId": saved["storyboardId"],
            "expectedVersion": 1,
            "title": "分镜",
            "shots": [shot(description="第二版")],
        },
        key="keep-source",
    )
    read = execute(
        registry, store, "storyboard_read", {"storyboardId": saved["storyboardId"]}, key="read-again"
    )["data"]
    assert read["sourceRefs"] == [{"artifactId": source, "version": 1}]


def _many_shots(count):
    return [shot(description=f"镜头 {index}") for index in range(count)]


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"shots": []}, "INVALID_ARGUMENTS"),
        ({"shots": _many_shots(65)}, "INVALID_ARGUMENTS"),
        ({"shots": [shot(durationSeconds=16)]}, "INVALID_ARGUMENTS"),
        ({"shots": [shot(durationSeconds=1)]}, "INVALID_ARGUMENTS"),
        ({"shots": [shot(description="x" * 2001)]}, "INVALID_ARGUMENTS"),
        ({"shots": [shot(framing="zoom")]}, "INVALID_ARGUMENTS"),
        ({"shots": [shot(cameraMovement="x" * 201)]}, "INVALID_ARGUMENTS"),
        ({"shots": [shot(mode="i2v")]}, "INVALID_ARGUMENTS"),
        ({"shots": [shot(referenceAssetIds=[str(uuid4())])]}, "INVALID_ARGUMENTS"),
        ({"shots": [shot(mode="flf2v", referenceAssetIds=[str(uuid4()), str(uuid4())])]}, "ASSET_NOT_FOUND"),
        ({"title": ""}, "INVALID_ARGUMENTS"),
        ({"expectedVersion": 1}, "INVALID_ARGUMENTS"),
        ({"storyboardId": str(uuid4())}, "INVALID_ARGUMENTS"),
    ],
)
def test_invalid_saves_never_create_storyboards(store, change, code):
    registry = mock_registry(store)
    args = {"title": "分镜", "shots": [shot()], **change}
    result = execute(registry, store, "storyboard_save", args, key="invalid")
    assert result["error"]["code"] == code
    assert not store.snapshot()["storyboards"]


def test_reference_assets_are_resolved_inside_the_data_directory(store):
    registry = mock_registry(store)
    store.ensure_session("other")
    first_asset = insert_asset(store, project_id="other")
    last_asset = insert_asset(store, project_id="other")
    saved = save(
        registry,
        store,
        {
            "title": "i2v 分镜",
            "shots": [
                shot(description="首帧起手", mode="i2v", referenceAssetIds=[first_asset]),
                shot(
                    description="首尾帧定格",
                    mode="flf2v",
                    referenceAssetIds=[first_asset, last_asset],
                ),
            ],
        },
        key="asset-save",
    )
    read = execute(
        registry, store, "storyboard_read", {"storyboardId": saved["storyboardId"]}, key="asset-read"
    )["data"]
    assert read["shots"][0]["referenceAssetIds"] == [first_asset]
    assert read["shots"][1]["referenceAssetIds"] == [first_asset, last_asset]
    assert read["shots"][1]["extendSource"] is None
    missing = execute(
        registry,
        store,
        "storyboard_save",
        {"title": "i2v", "shots": [shot(mode="i2v", referenceAssetIds=[str(uuid4())])]},
        key="asset-missing",
    )
    assert missing["error"]["code"] == "ASSET_NOT_FOUND"


def test_store_commits_reject_history_and_asset_rewrites(store):
    registry = mock_registry(store)
    saved = save(registry, store, {"title": "分镜", "shots": [shot()]})
    with pytest.raises(AppError) as error:
        store.transaction(lambda draft: draft["storyboards"].pop(saved["storyboardId"]))
    assert error.value.code == "STORYBOARD_IMMUTABLE"

    def tamper(draft):
        draft["storyboards"][saved["storyboardId"]]["versions"][0]["shots"][0]["description"] = "篡改"

    with pytest.raises(AppError) as error:
        store.transaction(tamper)
    assert error.value.code == "STORYBOARD_IMMUTABLE"

    asset_id = insert_asset(store, project_id="coffee", sha256="ab" * 32)

    def edit_asset(draft):
        draft["assets"][asset_id]["sizeBytes"] = 2048

    with pytest.raises(AppError) as error:
        store.transaction(edit_asset)
    assert error.value.code == "ASSET_IMMUTABLE"

    from vagent.video.storyboards import Asset, ImageDimensions

    duplicate_id = str(uuid4())
    duplicate = Asset(
        id=duplicate_id,
        project_id="coffee",
        relative_path=f"assets/coffee/{duplicate_id}.png",
        mime_type="image/png",
        size_bytes=512,
        sha256="ab" * 32,
        dimensions=ImageDimensions(width=1, height=1),
        created_at="2030-01-01T00:00:00+00:00",
    )
    with pytest.raises(AppError) as error:
        store.transaction(
            lambda draft: draft["assets"].__setitem__(
                duplicate_id, duplicate.model_dump(mode="json", by_alias=True)
            )
        )
    assert error.value.code == "ASSET_CONFLICT"


def test_project_content_limits_do_not_constrain_shot_descriptions(store):
    registry = mock_registry(store)
    store.transaction(
        lambda draft: draft["projects"]["coffee"].setdefault("contentLimits", {}).update({"all": 5})
    )
    description = "这是一段远超过五个非空白字符的镜头描述。"
    saved = save(registry, store, {"title": "分镜", "shots": [shot(description=description)]})
    read = execute(
        registry, store, "storyboard_read", {"storyboardId": saved["storyboardId"]}, key="limits-read"
    )["data"]
    assert read["shots"][0]["description"] == description


def test_read_only_registry_hides_storyboard_writes(store):
    registry = mock_registry(store).read_only()
    names = {spec["function"]["name"] for spec in registry.specs()}
    assert "storyboard_read" in names and "storyboard_save" not in names
    result = execute(registry, store, "storyboard_save", {"title": "x", "shots": [shot()]})
    assert result["error"]["code"] == "UNKNOWN_TOOL"
