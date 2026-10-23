import { test } from "node:test";
import assert from "node:assert/strict";

import {
  SEASON_EPISODE_LIMIT,
  seasonEpisodeRows,
  seasonMeta,
  seasonMetaText,
  seasonMembers,
  seasonPickerOptions,
  seasonSaveBody,
  seasonValidation,
} from "../web/season-state.js";

const FIRST = "11111111-1111-4111-8111-111111111111";
const SECOND = "22222222-2222-4222-8222-222222222222";
const THIRD = "33333333-3333-4333-8333-333333333333";
const FOURTH = "44444444-4444-4444-8444-444444444444";

const VIEW = {
  seasonId: FIRST,
  season: {
    title: "雨夜第一季",
    currentVersion: 2,
    revision: 3,
    episodeIds: [SECOND, FIRST],
    createdAt: "2030-01-01T00:00:00+00:00",
  },
  episodes: [
    {
      episodeId: SECOND,
      title: "第二集",
      currentVersion: 1,
      revision: 0,
      sceneCount: 4,
      shotCount: 12,
      targetDurationSeconds: 60,
    },
    {
      episodeId: FIRST,
      title: "第一集",
      currentVersion: 2,
      revision: 1,
      sceneCount: 5,
      shotCount: 15,
      targetDurationSeconds: 75,
    },
  ],
  seasons: [
    {
      seasonId: FIRST,
      title: "雨夜第一季",
      currentVersion: 2,
      revision: 3,
      episodeIds: [SECOND, FIRST],
      episodeCount: 2,
    },
    {
      seasonId: THIRD,
      title: "第二季",
      currentVersion: 1,
      revision: 0,
      episodeIds: [THIRD],
      episodeCount: 1,
    },
  ],
  batch: null,
};

test("seasonMeta projects the frozen season version and its ordered members", () => {
  const meta = seasonMeta(VIEW);
  assert.equal(meta.seasonId, FIRST);
  assert.equal(meta.title, "雨夜第一季");
  assert.equal(meta.currentVersion, 2);
  assert.equal(meta.revision, 3);
  assert.equal(meta.episodeCount, 2);
  assert.deepEqual(meta.episodeIds, [SECOND, FIRST]);
  assert.equal(seasonMeta(null), null);
  assert.equal(seasonMetaText(meta), "雨夜第一季 · 当前版本 2 · revision 3 · 2 集");
  assert.equal(seasonMetaText(null), "尚未保存任何季");
});

test("seasonMembers keeps episode order, not episode creation order", () => {
  const members = seasonMembers(VIEW);
  assert.deepEqual(
    members.map((member) => [member.order, member.episodeId, member.title]),
    [
      [1, SECOND, "第二集"],
      [2, FIRST, "第一集"],
    ],
  );
  assert.equal(members[0].shotCount, 12);
  assert.deepEqual(seasonMembers({ episodes: null }), []);
});

test("seasonPickerOptions maps every saved season", () => {
  assert.deepEqual(seasonPickerOptions(VIEW), [
    { value: FIRST, label: "雨夜第一季 · v2 · 2 集", episodeIds: [SECOND, FIRST] },
    { value: THIRD, label: "第二季 · v1 · 1 集", episodeIds: [THIRD] },
  ]);
  assert.deepEqual(seasonPickerOptions(null), []);
});

test("seasonEpisodeRows disables episodes owned by another season", () => {
  const episodes = [
    { episodeId: FIRST, title: "第一集", shotCount: 15, sceneCount: 5, targetDurationSeconds: 75 },
    { episodeId: THIRD, title: "第二季第一集", shotCount: 10, sceneCount: 3, targetDurationSeconds: 60 },
    { episodeId: FOURTH, title: "未归属集", shotCount: 10, sceneCount: 3, targetDurationSeconds: 60 },
  ];
  const { rows, missing } = seasonEpisodeRows(episodes, VIEW.seasons, FIRST, [FIRST, FOURTH]);
  assert.deepEqual(missing, []);
  assert.deepEqual(
    rows.map((row) => [row.episodeId, row.order, row.selected, row.disabled, row.ownerTitle]),
    [
      [FIRST, 1, true, false, null],
      [THIRD, 0, false, true, "第二季"],
      [FOURTH, 2, true, false, null],
    ],
  );
});

test("seasonEpisodeRows reports selected members that no longer exist", () => {
  const { rows, missing } = seasonEpisodeRows([], VIEW.seasons, FIRST, [SECOND]);
  assert.deepEqual(rows, []);
  assert.deepEqual(missing, [SECOND]);
});

test("seasonValidation enforces the frozen title and 1-4 member limits", () => {
  assert.deepEqual(seasonValidation(" 雨夜第一季 ", [FIRST]), {
    ok: true,
    error: null,
    title: "雨夜第一季",
    episodeIds: [FIRST],
  });
  assert.equal(seasonValidation("", [FIRST]).error, "季标题不能为空。");
  assert.equal(seasonValidation("x".repeat(121), [FIRST]).error, "季标题不能超过 120 个字符。");
  assert.equal(seasonValidation("第一季", []).error, "请至少选择 1 集。");
  assert.equal(
    seasonValidation("第一季", [FIRST, SECOND, THIRD, FOURTH, FIRST]).error,
    `一季最多 ${SEASON_EPISODE_LIMIT} 集。`,
  );
  assert.equal(seasonValidation("第一季", [FIRST, FIRST]).error, "同一集不能重复选择。");
});

test("seasonSaveBody trims the title and only sends a revision when editing", () => {
  assert.deepEqual(
    seasonSaveBody({
      clientRequestId: "request-1",
      title: " 雨夜第一季 ",
      episodeIds: [FIRST],
    }),
    { clientRequestId: "request-1", title: "雨夜第一季", episodeIds: [FIRST] },
  );
  assert.deepEqual(
    seasonSaveBody({
      clientRequestId: "request-2",
      title: "雨夜第一季",
      episodeIds: [FIRST, SECOND],
      seasonId: FIRST,
      expectedRevision: 3,
    }),
    {
      clientRequestId: "request-2",
      title: "雨夜第一季",
      episodeIds: [FIRST, SECOND],
      seasonId: FIRST,
      expectedRevision: 3,
    },
  );
});
