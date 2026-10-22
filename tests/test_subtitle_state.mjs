import { test } from "node:test";
import assert from "node:assert/strict";

import {
  hasSubtitle,
  latestSubtitle,
  renderModeLabel,
  subtitleCueLabel,
  subtitleDownloadUrl,
  subtitleErrorLabel,
  subtitleExportBody,
  subtitleSaveBody,
  subtitleSummary,
  subtitleTimelineLabel,
  subtitleVersions,
} from "../web/subtitle-state.js";

const TRACK_ID = "11111111-1111-4111-8111-111111111111";
const TRACK = {
  trackId: TRACK_ID,
  projectId: "coffee",
  scope: "storyboard",
  storyboardId: "22222222-2222-4222-8222-222222222222",
  revision: 1,
  currentVersion: 2,
  versionCount: 2,
  versions: [
    {
      version: 1,
      cueCount: 2,
      timelineSource: "film",
      timeline: [
        { durationSeconds: 5.021333 },
        { durationSeconds: 5.021333 },
      ],
    },
    {
      version: 2,
      cueCount: 3,
      timelineSource: "film",
      timeline: [
        { durationSeconds: 5.021333 },
        { durationSeconds: 5.021333 },
      ],
    },
  ],
};

test("subtitle versions keep server labels and mark the current one", () => {
  assert.equal(hasSubtitle(TRACK), true);
  assert.equal(hasSubtitle({ trackId: null }), false);
  const versions = subtitleVersions(TRACK);
  assert.deepEqual(
    versions.map((item) => [item.label, item.current, item.cueLabel]),
    [
      ["v2", true, "3 条"],
      ["v1", false, "2 条"],
    ],
  );
  assert.equal(latestSubtitle(TRACK).label, "v2");
});

test("summary names the frozen timeline source", () => {
  assert.equal(subtitleSummary(null), "还没有字幕");
  assert.equal(subtitleSummary(TRACK), "字幕 v2 · 3 条 · 成片时间线");
  assert.equal(
    subtitleSummary({ ...TRACK, versions: [{ ...TRACK.versions[0], timelineSource: "storyboard" }] }),
    "字幕 v1 · 2 条 · 分镜时间线",
  );
});

test("the save body carries one storyboard request token", () => {
  const board = {
    storyboardId: "22222222-2222-4222-8222-222222222222",
    revision: 4,
  };
  assert.deepEqual(subtitleSaveBody(board, "save-1"), {
    clientRequestId: "save-1",
    scope: "storyboard",
    storyboardId: board.storyboardId,
  });
  assert.equal(subtitleSaveBody({}, "save-1"), null);
  assert.equal(subtitleSaveBody(board, ""), null);
});

test("export bodies pin the current subtitle version and profile", () => {
  assert.deepEqual(subtitleExportBody(TRACK, "soft", "export-1"), {
    clientRequestId: "export-1",
    trackId: TRACK_ID,
    trackVersion: 2,
    mode: "soft",
    profile: "720p-h264-aac-v1",
  });
  assert.equal(subtitleExportBody(TRACK, "sidecar", "export-1"), null);
  assert.equal(subtitleExportBody({ trackId: null }, "soft", "export-1"), null);
});

test("download links stay on the controlled subtitle API", () => {
  assert.equal(subtitleDownloadUrl(TRACK, { version: 2 }), `/api/subtitles/${TRACK_ID}/2/srt`);
  assert.equal(subtitleDownloadUrl(TRACK, 1, "ass"), `/api/subtitles/${TRACK_ID}/1/ass`);
  assert.equal(subtitleDownloadUrl(TRACK, { version: 0 }), null);
  assert.equal(
    subtitleDownloadUrl({ ...TRACK, trackId: "not-a-uuid" }, { version: 1 }),
    null,
  );
});

test("labels explain the mode, cue count and timeline", () => {
  assert.equal(renderModeLabel("soft"), "软字幕");
  assert.equal(renderModeLabel("burn"), "烧录字幕");
  assert.equal(renderModeLabel("both"), "软字幕 + 烧录");
  assert.equal(renderModeLabel("none"), "");
  assert.equal(subtitleTimelineLabel(TRACK.versions[0]), "2 段 · 10.04 秒");
  assert.equal(subtitleCueLabel(TRACK.versions[1]), "3 条字幕 · 2 段 · 10.04 秒");
  assert.equal(subtitleCueLabel(null), "");
});

test("error labels are actionable and never leak raw details", () => {
  assert.equal(subtitleErrorLabel({ code: "FONT_MISSING", message: "x" }), "包内字幕字体缺失或校验失败");
  assert.equal(subtitleErrorLabel({ code: "SOMETHING", message: "具体原因" }), "具体原因");
  assert.equal(subtitleErrorLabel(null), "");
});
