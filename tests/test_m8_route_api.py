"""M8 E1: the local HTTP face of the route preview.

The endpoint is a read-only local action: no session, no Run, no CSRF token, no
provider request and no Job. It must return the same frozen comparison as the CLI
and the pure function.
"""

import json
from contextlib import asynccontextmanager

import httpx
from shot_support import mock_tools, save_storyboard, shot

from vagent.config import Config
from vagent.web import create_app


@asynccontextmanager
async def client_for(tmp_path):
    config = Config(
        home=tmp_path / "web",
        api_key=None,
        video_mode="live",
        video_api_key="wan-test-secret",
        kling_api_key="kling-test-secret",
        fal_api_key="fal-test-secret",
    )
    app = create_app(config)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:3210"
        ) as client:
            yield client, app.state.service


async def test_the_route_state_module_is_served_to_the_panel(tmp_path):
    async with client_for(tmp_path) as (client, _):
        page = await client.get("/")
        assert page.status_code == 200
        for name in ("app.js", "route-state.js"):
            asset = await client.get(f"/{name}")
            assert asset.status_code == 200, name
            assert "javascript" in asset.headers["content-type"]
        script = (await client.get("/route-state.js")).text
        assert "routeCandidateRows" in script and "provider-route-v1" in script


async def test_route_preview_compares_all_three_providers(tmp_path):
    async with client_for(tmp_path) as (client, service):
        response = await client.get("/api/route/preview?mode=t2v&durationSeconds=5")
        assert response.status_code == 200, response.text
        view = response.json()
        assert view["contract"] == "provider-route-v1"
        assert view["scope"] == "shot" and view["videoMode"] == "live"
        assert [item["provider"] for item in view["candidates"]] == ["wan", "fal", "kling"]
        assert [item["amount"] for item in view["candidates"]] == ["3.00", "0.42", "0.56"]
        assert view["recommended"] == "wan"
        assert all(item["verification"] == "documented-no-real-sample" for item in view["candidates"])
        assert all(item["sourceUrl"].startswith("https://") for item in view["candidates"])
        assert all(
            item["checkedAt"] == "2026-10-16" for item in view["candidates"] if item["provider"] == "wan"
        )
        assert any("不换汇" in note for note in view["notes"])
        assert not service.store.snapshot()["jobs"]


async def test_route_preview_episode_scope_filters_usd(tmp_path):
    async with client_for(tmp_path) as (client, _):
        view = (await client.get("/api/route/preview?mode=t2v&durationSeconds=5&scope=episode")).json()
        assert view["scope"] == "episode"
        assert view["recommended"] == "wan"
        reasons = {item["provider"]: item["reason"] for item in view["candidates"]}
        assert reasons["wan"] is None
        assert reasons["kling"] == reasons["fal"] == "EPISODE_COST_LIMIT"


async def test_route_preview_never_requires_a_session_or_a_token(tmp_path):
    async with client_for(tmp_path) as (client, service):
        state_file = service.store.home / "state.json"
        before = state_file.read_bytes()
        response = await client.get(
            "/api/route/preview?mode=i2v&durationSeconds=5&provider=kling",
            headers={"X-CSRF-Token": "not-a-real-token"},
        )
        assert response.status_code == 200, response.text
        kling = next(item for item in response.json()["candidates"] if item["provider"] == "kling")
        assert (kling["available"], kling["reason"]) == (False, "VIDEO_UNSUPPORTED_MODE")
        assert state_file.read_bytes() == before


async def test_route_preview_errors_use_the_frozen_codes(tmp_path):
    async with client_for(tmp_path) as (client, _):
        unavailable = await client.get("/api/route/preview?mode=t2v&durationSeconds=10&requireAvailable=true")
        assert unavailable.status_code == 400
        assert unavailable.json()["error"]["code"] == "ROUTE_UNAVAILABLE"
        too_long = await client.get("/api/route/preview?mode=t2v&durationSeconds=99")
        assert too_long.json()["error"]["code"] == "ROUTE_UNAVAILABLE"
        missing_duration = await client.get("/api/route/preview?mode=t2v")
        assert missing_duration.json()["error"]["code"] == "INVALID_ARGUMENTS"
        unknown_board = await client.get("/api/route/preview?storyboardId=no-such-board")
        assert unknown_board.json()["error"]["code"] == "ROUTE_UNAVAILABLE"


async def test_route_preview_reads_one_storyboard_version(tmp_path):
    async with client_for(tmp_path) as (client, service):
        registry, _, _, _ = mock_tools(service.store)
        saved = save_storyboard(registry, service.store, [shot("镜头一"), shot("镜头二")])
        view = (await client.get(f"/api/route/preview?storyboardId={saved['storyboardId']}&version=1")).json()
        assert view["request"]["storyboardId"] == saved["storyboardId"]
        assert view["request"]["storyboardVersion"] == 1
        assert view["request"]["shotIds"] == saved["shotIds"]
        assert view["request"]["mode"] is None and view["request"]["durationSeconds"] is None
        wan = next(item for item in view["candidates"] if item["provider"] == "wan")
        assert (wan["amount"], wan["quantity"]) == ("6.00", "10")
        subset = (
            await client.get(
                f"/api/route/preview?storyboardId={saved['storyboardId']}&shotIds={saved['shotIds'][0]}"
            )
        ).json()
        assert subset["request"]["shotIds"] == [saved["shotIds"][0]]
        assert next(item for item in subset["candidates"] if item["provider"] == "wan")["amount"] == "3.00"
        assert json.loads((service.store.home / "state.json").read_bytes())["jobs"] == {}
