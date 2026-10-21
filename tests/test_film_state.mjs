import { test } from "node:test";
import assert from "node:assert/strict";

import {
  assembleBody,
  filmDownloadUrl,
  filmErrorLabel,
  filmPlayback,
  filmRequestPath,
  filmSummary,
  filmTimelineLabel,
  filmVersions,
  hasFilm,
} from "../web/film-state.js";

const FILM = {
  filmId: "11111111-1111-4111-8111-111111111111",
  projectId: "coffee",
  storyboardId: "22222222-2222-4222-8222-222222222222",
  revision: 2,
  currentVersion: 2,
  versionCount: 2,
  versions: [
    {
      version: 1,
      outputMediaId: "33333333-3333-4333-8333-333333333333",
      mediaAvailable: true,
      mediaUrl: "/api/media/33333333-3333-4333-8333-333333333333/content",
      durationSeconds: 10.0212890625,
      sizeBytes: 418395,
      sha256: "a".repeat(64),
      inputs: [{ shotId: "s1" }, { shotId: "s2" }],
      timeline: [
        { shotId: "s1", startSeconds: 0, durationSeconds: 5.021333 },
        { shotId: "s2", startSeconds: 5.021333, durationSeconds: 5.021333 },
      ],
    },
    {
      version: 2,
      outputMediaId: "44444444-4444-4444-8444-444444444444",
      mediaAvailable: false,
      mediaUrl: null,
      durationSeconds: 10.0212890625,
      inputs: [{ shotId: "s1" }, { shotId: "s2" }],
      timeline: [],
    },
  ],
};

test("an empty film projection is not a film", () => {
  assert.equal(hasFilm(null), false);
  assert.equal(hasFilm({ filmId: "x", versions: [] }), false);
  assert.deepEqual(filmVersions(null), []);
  assert.equal(filmSummary(null), "还没有成片");
});

test("versions keep the server order and newest first for display", () => {
  const rows = filmVersions(FILM);
  assert.deepEqual(
    rows.map((row) => row.label),
    ["v2", "v1"],
  );
  assert.equal(rows[0].current, true);
  assert.equal(rows[1].current, false);
  assert.equal(rows[1].shots, 2);
  assert.equal(rows[1].durationLabel, "10.02 秒");
});

test("the player only exposes an available version", () => {
  const playback = filmPlayback(FILM);
  assert.equal(playback.url, "/api/media/33333333-3333-4333-8333-333333333333/content");
  assert.equal(playback.version.label, "v1");
  const unavailable = { ...FILM, versions: [FILM.versions[1]] };
  assert.equal(filmPlayback(unavailable).url, null);
});

test("the summary states the current version, shots and availability", () => {
  assert.equal(filmSummary(FILM), "成片 v2 · 2 个镜头 · 10.02 秒 · 本地文件不可用");
});

test("download links stay on the controlled media route", () => {
  assert.equal(
    filmDownloadUrl(FILM.versions[0]),
    "/api/media/33333333-3333-4333-8333-333333333333/content?download=1",
  );
  assert.equal(filmDownloadUrl(FILM.versions[1]), null);
  assert.equal(filmDownloadUrl({ mediaAvailable: true, mediaUrl: "https://evil.example/x.mp4" }), null);
});

test("the assemble body is built from one storyboard snapshot", () => {
  const board = { storyboardId: "board-1", currentVersion: 1, revision: 3 };
  assert.deepEqual(assembleBody(board, "request-1"), {
    clientRequestId: "request-1",
    storyboardId: "board-1",
    expectedRevision: 3,
  });
  assert.equal(assembleBody({ storyboardId: "board-1", currentVersion: 1 }, "request-1"), null);
  assert.equal(assembleBody(board, ""), null);
  assert.equal(assembleBody(null, "request-1"), null);
});

test("the film request path encodes the storyboard and project", () => {
  assert.equal(
    filmRequestPath({ storyboardId: "22222222-2222-4222-8222-222222222222" }, "coffee"),
    "/api/films/22222222-2222-4222-8222-222222222222?projectId=coffee",
  );
  assert.equal(filmRequestPath({ storyboardId: "not a uuid" }, "coffee"), null);
});

test("timeline labels sum the recorded rational offsets", () => {
  assert.equal(filmTimelineLabel(FILM.versions[0]), "2 段 · 10.04 秒时间线");
  assert.equal(filmTimelineLabel(FILM.versions[1]), "");
});

test("assembly errors are explained without leaking server text", () => {
  assert.equal(filmErrorLabel({ code: "VERSION_CONFLICT" }), "分镜已变化，请刷新后重试");
  assert.equal(filmErrorLabel({ code: "FILM_INPUT_NOT_READY" }), "还有镜头的最新版本未交付");
  assert.equal(filmErrorLabel({ code: "OTHER", message: "something" }), "something");
  assert.equal(filmErrorLabel(null), "");
});
