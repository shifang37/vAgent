import { test } from "node:test";
import assert from "node:assert/strict";

import {
  episodeBudget,
  episodeMeta,
  episodePlayback,
  episodeProgress,
  episodeRender,
  episodeRenderSummary,
  episodeSubtitleToggleLabel,
  filmStatusLabel,
  parseSubtitleSrt,
  sceneFilmPath,
  sceneGeneration,
  sceneSummary,
  shotStatusLabel,
  subtitleCueAt,
} from "../web/episode-state.js";

const EPISODE = {
  episodeId: "11111111-1111-4111-8111-111111111111",
  episode: {
    title: "冰箱上的便条",
    currentVersion: 1,
    revision: 1,
    styleId: null,
    targetDurationSeconds: 60,
    shotBudget: { shotCount: 4, targetDurationSeconds: 60 },
  },
  scenes: [
    {
      sceneId: "scene-1",
      storyboardId: "22222222-2222-4222-8222-222222222222",
      shotCount: 2,
      durationSeconds: 10,
      film: {
        filmId: "film-1",
        currentVersion: 1,
        latest: {
          mediaAvailable: true,
          mediaUrl: "/api/media/33333333-3333-4333-8333-333333333333/content",
          outputMediaId: "33333333-3333-4333-8333-333333333333",
        },
      },
    },
    {
      sceneId: "scene-2",
      storyboardId: null,
      shotCount: 2,
      durationSeconds: 10,
      film: null,
    },
  ],
  generation: {
    counts: { planned: 4, registered: 3, delivered: 2, failed: 1, pending: 0, unregistered: 1 },
    budget: {
      shotCount: 4,
      targetDurationSeconds: 60,
      jobCap: 20,
      usedJobs: 3,
      remainingJobs: 17,
      currency: "CNY",
      usedAmount: "9.00",
      episodeCapAmount: "60.00",
      remainingAmount: "51.00",
      foreignCurrencyJobs: [],
    },
    shots: [
      { sceneId: "scene-1", status: "delivered", jobId: "job-1" },
      { sceneId: "scene-1", status: "delivered", jobId: "job-2" },
      { sceneId: "scene-2", status: "failed", jobId: "job-3" },
      { sceneId: "scene-2", status: "unregistered", jobId: null },
    ],
  },
  consistency: {
    characters: [
      {
        name: "母亲",
        characterId: "character-1",
        shotCount: 2,
        consistent: true,
        reviews: [{ score: 5 }],
      },
    ],
    reviews: [{ score: 5 }],
  },
};

test("sceneGeneration aggregates one row per scene", () => {
  const rows = sceneGeneration(EPISODE);
  assert.deepEqual(rows.get("scene-1"), {
    total: 2,
    delivered: 2,
    pending: 0,
    failed: 0,
    unregistered: 0,
    jobs: [
      { sceneId: "scene-1", status: "delivered", jobId: "job-1" },
      { sceneId: "scene-1", status: "delivered", jobId: "job-2" },
    ],
  });
  assert.equal(rows.get("scene-2").failed, 1);
  assert.equal(rows.get("scene-2").unregistered, 1);
  assert.equal(rows.get("scene-2").jobs.length, 1);
});

test("episode budget and progress keep the frozen caps", () => {
  assert.deepEqual(episodeProgress(EPISODE), {
    planned: 4,
    delivered: 2,
    registered: 3,
    failed: 1,
    pending: 0,
    unregistered: 1,
    complete: false,
  });
  const budget = episodeBudget(EPISODE);
  assert.equal(budget.jobCap, 20);
  assert.equal(budget.usedJobs, 3);
  assert.equal(budget.remainingAmount, "51.00");
  assert.equal(budget.currency, "CNY");
});

test("empty or partial episodes degrade to zeroes instead of throwing", () => {
  assert.deepEqual(episodeProgress(null), {
    planned: 0,
    delivered: 0,
    registered: 0,
    failed: 0,
    pending: 0,
    unregistered: 0,
    complete: false,
  });
  assert.equal(episodeBudget(null).usedAmount, "0.00");
  assert.equal(episodeMeta(null), null);
  assert.deepEqual([...sceneGeneration(null).keys()], []);
});

test("scene rows, film labels and links stay user-facing", () => {
  const rows = sceneGeneration(EPISODE);
  assert.equal(
    sceneSummary(EPISODE.scenes[0], rows.get("scene-1")),
    "2 镜 · 10 秒 · 2 已交付",
  );
  assert.equal(
    sceneSummary(EPISODE.scenes[1], rows.get("scene-2")),
    "2 镜 · 10 秒 · 1 需处理 · 1 未登记",
  );
  assert.equal(filmStatusLabel(EPISODE.scenes[0].film), "v1 · 可播放");
  assert.equal(filmStatusLabel(null), "尚未拼接");
  assert.equal(
    sceneFilmPath(EPISODE.scenes[0], "coffee"),
    "/api/films/22222222-2222-4222-8222-222222222222?projectId=coffee",
  );
  assert.equal(sceneFilmPath(EPISODE.scenes[1], "coffee"), null);
});

test("shot status labels and episode meta are stable", () => {
  assert.equal(shotStatusLabel("delivered"), "已交付");
  assert.equal(shotStatusLabel("weird"), "weird");
  const meta = episodeMeta(EPISODE);
  assert.equal(meta.title, "冰箱上的便条");
  assert.equal(meta.sceneCount, 2);
  assert.equal(meta.targetDurationSeconds, 60);
});

const RENDERED = {
  renderId: "render-1",
  status: "succeeded",
  filmId: "film-episode-1",
  filmVersion: 1,
  mediaUrl: "/api/media/44444444-4444-4444-8444-444444444444/content",
  scenes: [
    { status: "succeeded" },
    { status: "succeeded" },
  ],
  render: {
    subtitle: {
      mode: "soft",
      sidecarUrl: "/api/films/film-episode-1/versions/1/subtitles.srt",
    },
    measurements: {
      durationSeconds: 89.166666,
      video: { width: 1280, height: 720 },
    },
    audio: {
      loudness: {
        output: {
          verification: { integratedLufs: -16.0, truePeakDb: -14.2 },
        },
      },
    },
  },
};

test("episode render projection keeps playback and diagnostics separate", () => {
  const render = episodeRender({ render: RENDERED });
  assert.equal(render.status, "succeeded");
  assert.equal(render.filmId, "film-episode-1");
  assert.equal(render.mediaUrl, RENDERED.mediaUrl);
  assert.equal(render.sidecarUrl, RENDERED.render.subtitle.sidecarUrl);
  assert.equal(render.durationSeconds, 89.166666);
  assert.equal(render.width, 1280);
  assert.equal(render.height, 720);
  assert.equal(render.loudnessLufs, -16.0);
  assert.equal(render.sceneCount, 2);
  assert.equal(render.failedScenes, 0);
  assert.deepEqual(episodePlayback({ render: RENDERED }), render);

  const failed = episodeRender({
    render: {
      renderId: "render-2",
      status: "failed",
      failureStage: "source",
      errorCode: "RENDER_SOURCE_MISSING",
      scenes: [{ status: "succeeded" }, { status: "failed" }],
    },
  });
  assert.equal(failed.status, "failed");
  assert.equal(failed.mediaUrl, null);
  assert.equal(failed.failedScenes, 1);
  assert.equal(
    episodeRenderSummary(failed),
    "渲染失败 · 1 场失败 · RENDER_SOURCE_MISSING",
  );
  assert.equal(episodeRender(null), null);
  assert.equal(episodePlayback(null), null);
  assert.equal(episodeRenderSummary(null), "整集尚未渲染");
});

test("episode render summary names duration, picture and loudness", () => {
  assert.equal(
    episodeRenderSummary(episodeRender({ render: RENDERED })),
    "89.2 秒 · 1280×720 · -16.0 LUFS · 软字幕",
  );
  assert.equal(episodeSubtitleToggleLabel(true), "字幕：开");
  assert.equal(episodeSubtitleToggleLabel(false), "字幕：关");
});

const SRT = `1
00:00:09,126 --> 00:00:14,189
粥在锅里

2
00:00:14,189 --> 00:00:19,252
，火关小
，记得吃。

3
bad block without timing
`;

test("sidecar SRT parses into exact playback cues and skips bad blocks", () => {
  const cues = parseSubtitleSrt(SRT);
  assert.equal(cues.length, 2);
  assert.deepEqual(cues[0], { start: 9.126, end: 14.189, text: "粥在锅里" });
  assert.deepEqual(cues[1], {
    start: 14.189,
    end: 19.252,
    text: "，火关小\n，记得吃。",
  });
  assert.equal(subtitleCueAt(cues, 9.0), null);
  assert.equal(subtitleCueAt(cues, 9.126).text, "粥在锅里");
  assert.equal(subtitleCueAt(cues, 14.189).text, "，火关小\n，记得吃。");
  assert.equal(subtitleCueAt(cues, 19.252), null);
  assert.equal(subtitleCueAt(cues, Number.NaN), null);
  assert.deepEqual(parseSubtitleSrt(""), []);
  assert.deepEqual(parseSubtitleSrt(" \r\n "), []);
});
