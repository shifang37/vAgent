import { test } from "node:test";
import assert from "node:assert/strict";

import {
  TIMELINE_CLIP_LIMIT,
  timelineAudioRows,
  timelineDraft,
  timelineDraftTransitions,
  timelineErrorLabel,
  timelineMeta,
  timelineMetaText,
  timelineMove,
  timelinePreview,
  timelineRenderBody,
  timelineSaveBody,
  timelineSetTransition,
  timelineSubtitleOptions,
  timelineSummary,
  timelineSummaryText,
  timelineTransitionFor,
  timelineTrim,
  timelineValidation,
} from "../web/timeline-state.js";

const SHOT_A = "11111111-1111-4111-8111-111111111111";
const SHOT_B = "22222222-2222-4222-8222-222222222222";
const SHOT_C = "33333333-3333-4333-8333-333333333333";
const TRACK = "44444444-4444-4444-8444-444444444444";

function clip(shotId, overrides = {}) {
  return {
    shotId,
    shotVersionId: `v-${shotId}`,
    storyboardId: `board-${shotId}`,
    sceneId: `scene-${shotId}`,
    inMs: 0,
    outMs: 5000,
    sourceDurationMs: 5000,
    mediaUrl: `/api/media/${shotId}/content`,
    title: `镜头 ${shotId.slice(0, 4)}`,
    ...overrides,
  };
}

test("a new draft starts from every versioned shot", () => {
  const editor = {
    shots: [
      { ...clip(SHOT_A), durationMs: 5000, mediaAvailable: true },
      { ...clip(SHOT_B), durationMs: 5000, mediaAvailable: true },
      { ...clip(SHOT_C), durationMs: 5000, shotVersionId: null },
    ],
    timeline: null,
  };
  const draft = timelineDraft(editor);
  assert.deepEqual(draft.map((item) => item.shotId), [SHOT_A, SHOT_B]);
  assert.deepEqual(draft.map((item) => item.outMs), [5000, 5000]);
  assert.equal(timelineDraftTransitions(editor).length, 0);
  assert.equal(timelineMeta(editor.timeline), null);
});

test("an existing timeline draft keeps its windows and transitions", () => {
  const editor = {
    shots: [clip(SHOT_A), clip(SHOT_B)],
    timeline: {
      timelineId: "timeline-1",
      episodeId: "episode-1",
      currentVersion: 2,
      revision: 3,
      stale: false,
      latest: {
        version: 2,
        clips: [clip(SHOT_A, { inMs: 1000, outMs: 4000 }), clip(SHOT_B)],
        transitions: [
          { afterShotId: SHOT_A, type: "crossfade", durationMs: 400 },
          { afterShotId: SHOT_B, type: "cut", durationMs: 0 },
        ],
      },
    },
  };
  const draft = timelineDraft(editor);
  assert.equal(draft[0].inMs, 1000);
  assert.equal(draft[0].outMs, 4000);
  assert.deepEqual(timelineDraftTransitions(editor), [
    { afterShotId: SHOT_A, type: "crossfade", durationMs: 400 },
  ]);
  const meta = timelineMeta(editor.timeline);
  assert.equal(meta.version, 2);
  assert.match(timelineMetaText(meta), /v2/);
});

test("summary subtracts transitions from the trimmed timeline", () => {
  const clips = [clip(SHOT_A, { inMs: 1000 }), clip(SHOT_B)];
  const summary = timelineSummary(clips, [{ afterShotId: SHOT_A, type: "fade", durationMs: 500 }]);
  assert.equal(summary.rawMs, 9000);
  assert.equal(summary.transitionMs, 500);
  assert.equal(summary.effectiveMs, 8500);
  assert.match(timelineSummaryText(summary), /有效 8.5 秒/);
  assert.match(timelineSummaryText({ clipCount: 0, effectiveMs: 0 }), /尚无镜头/);
});

test("validation accepts a plain cut list", () => {
  const result = timelineValidation([clip(SHOT_A), clip(SHOT_B), clip(SHOT_C)], []);
  assert.equal(result.ok, true);
  assert.equal(result.summary.effectiveMs, 15000);
});

test("validation rejects duplicate, short and out-of-range clips", () => {
  assert.equal(timelineValidation([], []).ok, false);
  assert.equal(timelineValidation([clip(SHOT_A), clip(SHOT_A)], []).ok, false);
  assert.equal(timelineValidation([clip(SHOT_A, { outMs: 999 })], []).ok, false);
  assert.equal(timelineValidation([clip(SHOT_A, { inMs: 500 }), clip(SHOT_B, { outMs: 6000 })], []).ok, false);
  assert.equal(timelineValidation([clip(SHOT_A, { inMs: 2000, outMs: 1000 })], []).ok, false);
  const many = Array.from({ length: TIMELINE_CLIP_LIMIT + 1 }, (_, index) =>
    clip(`shot-${index}`),
  );
  assert.equal(timelineValidation(many, []).ok, false);
});

test("validation enforces transition boundaries and the one-third rule", () => {
  const clips = [clip(SHOT_A), clip(SHOT_B)];
  assert.equal(
    timelineValidation(clips, [{ afterShotId: SHOT_A, type: "crossfade", durationMs: 100 }]).ok,
    true,
  );
  assert.equal(
    timelineValidation(clips, [{ afterShotId: SHOT_B, type: "crossfade", durationMs: 100 }]).ok,
    false,
  );
  assert.equal(
    timelineValidation(clips, [{ afterShotId: SHOT_A, type: "crossfade", durationMs: 50 }]).ok,
    false,
  );
  assert.equal(
    timelineValidation(clips, [{ afterShotId: SHOT_A, type: "crossfade", durationMs: 2000 }]).ok,
    false,
  );
  assert.equal(
    timelineValidation(
      [clip(SHOT_A, { outMs: 2000 }), clip(SHOT_B, { outMs: 2000 })],
      [{ afterShotId: SHOT_A, type: "fade", durationMs: 800 }],
    ).ok,
    false,
  );
  assert.equal(
    timelineValidation(
      clips,
      [
        { afterShotId: SHOT_A, type: "fade", durationMs: 100 },
        { afterShotId: SHOT_A, type: "crossfade", durationMs: 100 },
      ],
    ).ok,
    false,
  );
  // The exact one-second boundaries are legal: one 1 s clip and a 1 s total.
  assert.equal(
    timelineValidation([clip(SHOT_A, { outMs: 1000 })], []).ok,
    true,
  );
});

test("validation enforces the 1-90 second effective window", () => {
  const long = Array.from({ length: 19 }, (_, index) => clip(`shot-${index}`));
  // 19 x 5 s = 95 s is above the frozen 90 s ceiling.
  assert.equal(timelineValidation(long, []).ok, false);
  const trimmed = Array.from({ length: 18 }, (_, index) => clip(`shot-${index}`, { outMs: 4000 }));
  assert.equal(timelineValidation(trimmed, []).ok, true);
});

test("move and trim return new arrays without mutating the draft", () => {
  const clips = [clip(SHOT_A), clip(SHOT_B), clip(SHOT_C)];
  const moved = timelineMove(clips, 0, 1);
  assert.deepEqual(moved.map((item) => item.shotId), [SHOT_B, SHOT_A, SHOT_C]);
  assert.deepEqual(clips.map((item) => item.shotId), [SHOT_A, SHOT_B, SHOT_C]);
  assert.deepEqual(timelineMove(clips, 0, -1).map((item) => item.shotId), [SHOT_A, SHOT_B, SHOT_C]);

  const trimmed = timelineTrim(clips, 1, 1000, 3500);
  assert.equal(trimmed[1].inMs, 1000);
  assert.equal(trimmed[1].outMs, 3500);
  assert.equal(clips[1].inMs, 0);
  // The out point cannot exceed the source duration.
  assert.equal(timelineTrim(clips, 0, 0, 9000)[0].outMs, 5000);
});

test("transitions are set once per boundary and cut removes them", () => {
  const once = timelineSetTransition([], SHOT_A, "fade", 300);
  assert.deepEqual(once, [{ afterShotId: SHOT_A, type: "fade", durationMs: 300 }]);
  const replaced = timelineSetTransition(once, SHOT_A, "crossfade", 500);
  assert.deepEqual(replaced, [{ afterShotId: SHOT_A, type: "crossfade", durationMs: 500 }]);
  assert.deepEqual(timelineSetTransition(replaced, SHOT_A, "cut", 0), []);
  assert.equal(timelineTransitionFor(replaced, SHOT_A).type, "crossfade");
  assert.equal(timelineTransitionFor(replaced, SHOT_B), null);
});

test("save body normalizes only the frozen fields", () => {
  const created = timelineSaveBody({
    clientRequestId: "save-1",
    episodeId: "episode-1",
    clips: [{ ...clip(SHOT_A), title: "ignored", mediaUrl: "ignored" }],
    transitions: [{ afterShotId: SHOT_A, type: "cut", durationMs: 0 }],
    subtitle: { trackId: TRACK, trackVersion: 2 },
    audio: [{ audioId: "a", audioVersion: 1, kind: "voice", gainDb: 0, startMs: 900 }],
    titleCards: [{ text: "片头", startMs: 0, durationMs: 1000 }],
  });
  assert.equal(created.clientRequestId, "save-1");
  assert.equal(created.episodeId, "episode-1");
  assert.deepEqual(created.clips, [
    {
      shotId: SHOT_A,
      shotVersionId: `v-${SHOT_A}`,
      storyboardId: `board-${SHOT_A}`,
      sceneId: `scene-${SHOT_A}`,
      inMs: 0,
      outMs: 5000,
    },
  ]);
  assert.deepEqual(created.transitions, []);
  assert.deepEqual(created.subtitle, { trackId: TRACK, trackVersion: 2 });
  assert.deepEqual(created.audio, [
    { audioId: "a", audioVersion: 1, kind: "voice", gainDb: 0, startMs: 0 },
  ]);
  assert.deepEqual(created.titleCards, [{ text: "片头", startMs: 0, durationMs: 1000 }]);
  assert.equal("timelineId" in created, false);
  assert.equal("expectedRevision" in created, false);

  const updated = timelineSaveBody({
    clientRequestId: "save-2",
    episodeId: "episode-1",
    timelineId: "timeline-1",
    expectedRevision: 4,
    clips: [clip(SHOT_A)],
  });
  assert.equal(updated.timelineId, "timeline-1");
  assert.equal(updated.expectedRevision, 4);
  assert.deepEqual(updated.audio, []);
});

test("audio rows and subtitle options expose only bounded fields", () => {
  const editor = {
    audio: [
      {
        audioId: "a",
        audioVersion: 2,
        kind: "bgm",
        title: "雨声",
        durationSeconds: 1,
        mediaUrl: "/api/media/a/content",
      },
    ],
    subtitleTracks: [
      {
        trackId: TRACK,
        currentVersion: 2,
        versions: [
          { version: 1, cueCount: 3 },
          { version: 2, cueCount: 5 },
        ],
      },
    ],
  };
  assert.deepEqual(timelineAudioRows(editor), [
    {
      audioId: "a",
      audioVersion: 2,
      kind: "bgm",
      title: "雨声",
      durationSeconds: 1,
      mediaUrl: "/api/media/a/content",
    },
  ]);
  assert.deepEqual(timelineSubtitleOptions(editor), [
    { trackId: TRACK, version: 2, label: "整集字幕 v2", cueCount: 5 },
  ]);
});

test("preview follows edit order and applies transitions", () => {
  const clips = [clip(SHOT_A, { inMs: 1000 }), clip(SHOT_B), clip(SHOT_C, { outMs: 3000 })];
  const transitions = [{ afterShotId: SHOT_A, type: "crossfade", durationMs: 500 }];
  const preview = timelinePreview(clips, transitions);
  assert.equal(preview[0].inMs, 1000);
  assert.equal(preview[0].effectiveStartMs, 0);
  assert.equal(preview[0].effectiveDurationMs, 3500);
  assert.equal(preview[1].effectiveStartMs, 3500);
  assert.equal(preview[1].incomingTransition, "crossfade");
  assert.equal(preview[2].effectiveStartMs, 8500);
  assert.equal(preview[2].outgoingTransition, "cut");
  assert.equal(preview[0].mediaUrl, `/api/media/${SHOT_A}/content`);
});

test("render body and error labels are frozen", () => {
  assert.deepEqual(timelineRenderBody({ clientRequestId: "r", timelineId: "t", timelineVersion: 2 }), {
    clientRequestId: "r",
    scope: "timeline",
    timelineId: "t",
    timelineVersion: 2,
    profile: "720p-h264-aac-v1",
    subtitleMode: "soft",
    confirm: true,
  });
  assert.match(timelineErrorLabel("TIMELINE_SOURCE_MISSING"), /过期/);
  assert.match(timelineErrorLabel("UNKNOWN_CODE"), /失败/);
});
