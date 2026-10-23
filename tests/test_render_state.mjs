import { test } from "node:test";
import assert from "node:assert/strict";

import {
  renderErrorLabel,
  renderRequestBody,
  renderRequestPath,
  renderSceneRows,
  renderSidecarUrl,
  renderStatusLabel,
  renderSummary,
} from "../web/render-state.js";

const BOARD = {
  storyboardId: "22222222-2222-4222-8222-222222222222",
  revision: 3,
};
const TRACK = { trackId: "11111111-1111-4111-8111-111111111111", currentVersion: 1 };
const RENDER = {
  renderId: "33333333-3333-4333-8333-333333333333",
  scope: "episode",
  status: "succeeded",
  profile: "720p-h264-aac-v1",
  subtitleMode: "both",
  filmId: "44444444-4444-4444-8444-444444444444",
  filmVersion: 2,
  render: {
    exportProfile: { width: 1280, height: 720 },
    subtitle: { mode: "both", sidecarUrl: "/api/films/44444444-4444-4444-8444-444444444444/versions/2/subtitles.srt" },
    measurements: { durationSeconds: 89.6 },
  },
  scenes: [
    { status: "succeeded", durationSeconds: 10 },
    { status: "failed", errorCode: "RENDER_SOURCE_MISSING", failureStage: "source" },
  ],
};

test("a render body pins the storyboard, track and safe defaults", () => {
  const body = renderRequestBody(BOARD, TRACK, "render-1");
  assert.deepEqual(body, {
    clientRequestId: "render-1",
    scope: "storyboard",
    storyboardId: BOARD.storyboardId,
    profile: "720p-h264-aac-v1",
    subtitleMode: "both",
    confirm: true,
    trackId: TRACK.trackId,
  });
  assert.equal(renderRequestBody({}, TRACK, "render-1"), null);
  assert.equal(renderRequestBody(BOARD, null, "render-1").subtitleMode, "soft");
  assert.equal(renderRequestBody(BOARD, null, "render-1", { mode: "burn" }), null);
  assert.equal(renderRequestBody(BOARD, TRACK, ""), null);
  assert.equal(renderRequestBody(BOARD, TRACK, "render-2", { profile: "2160p" }).profile, "720p-h264-aac-v1");
});

test("the progress query is scoped to one project and storyboard", () => {
  assert.equal(
    renderRequestPath("coffee", BOARD),
    `/api/renders?projectId=coffee&storyboardId=${BOARD.storyboardId}`,
  );
  assert.equal(renderRequestPath("", BOARD), null);
  assert.equal(renderRequestPath("coffee", {}), null);
});

test("summary reports status, geometry, mode, scenes and failure", () => {
  assert.equal(renderStatusLabel(null), "未渲染");
  assert.equal(renderStatusLabel(RENDER), "已渲染");
  assert.equal(renderStatusLabel({ status: "running" }), "渲染中");
  assert.equal(renderSummary(null), "还没有整片渲染记录。");
  assert.equal(
    renderSummary(RENDER),
    "已渲染 · 1280×720 · 软字幕+烧录 · 1/2 场完成 · 89.6 秒 · 失败：RENDER_SOURCE_MISSING",
  );
});

test("scene rows surface failures and successful durations", () => {
  const rows = renderSceneRows(RENDER);
  assert.equal(rows[0].label, "第 1 场");
  assert.equal(rows[0].detail, "10.0 秒");
  assert.equal(rows[0].tone, "succeeded");
  assert.equal(rows[1].status, "渲染失败");
  assert.equal(rows[1].detail, "RENDER_SOURCE_MISSING · source");
  assert.equal(rows[1].tone, "attention");
  assert.deepEqual(renderSceneRows(null), []);
});

test("sidecar link and error labels stay controlled", () => {
  assert.equal(renderSidecarUrl(RENDER), RENDER.render.subtitle.sidecarUrl);
  assert.equal(renderSidecarUrl({}), null);
  assert.equal(renderErrorLabel({ code: "TRANSITION_INVALID" }), "转场类型、时长或边界非法。（TRANSITION_INVALID）");
  assert.equal(renderErrorLabel({ code: "UNKNOWN", message: "具体原因" }), "UNKNOWN：具体原因");
});
