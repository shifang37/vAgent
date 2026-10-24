import { test } from "node:test";
import assert from "node:assert/strict";

import {
  ROUTE_CONTRACT,
  routeAmountText,
  routeCandidateRows,
  routeErrorText,
  routeNotes,
  routePreviewPath,
  routeProviderLabel,
  routeProviderSwitchBody,
  routeReasonText,
  routeRecommendedLabel,
  routeRequestSummary,
  routeScopeLabel,
  routeSwitchable,
} from "../web/route-state.js";

const PREVIEW = {
  contract: ROUTE_CONTRACT,
  scope: "shot",
  request: {
    scope: "shot",
    mode: "t2v",
    durationSeconds: 5,
    aspectRatio: "16:9",
    storyboardId: null,
    storyboardVersion: null,
    shotIds: [],
    provider: null,
  },
  videoMode: "live",
  candidates: [
    {
      provider: "wan",
      model: "wan2.7-t2v-2026-06-12",
      capabilitiesVersion: "wan27-shot-beijing-v2",
      priceVersion: "wan27-beijing-720p-20261010",
      currency: "CNY",
      unit: "output_second",
      unitPrice: "0.60",
      quantity: "5",
      amount: "3.00",
      resourceUnits: null,
      available: true,
      reason: null,
      keyConfigured: true,
      verification: "documented-no-real-sample",
      sourceUrl: "https://help.aliyun.com/zh/model-studio/model-pricing#ba6f7744d5e0o",
      checkedAt: "2026-10-16",
      quoteFingerprint: "a".repeat(64),
    },
    {
      provider: "fal",
      model: "fal-ai/kling-video/v3/standard/text-to-video",
      capabilitiesVersion: "multi-provider-v3",
      priceVersion: "fal-kling30-standard-silent-20261021",
      currency: "USD",
      unit: "output_second",
      unitPrice: "0.084",
      quantity: "5",
      amount: "0.42",
      resourceUnits: null,
      available: true,
      reason: null,
      keyConfigured: true,
      verification: "documented-no-real-sample",
      sourceUrl: "https://fal.ai/models/fal-ai/kling-video/v3/standard/text-to-video",
      checkedAt: "2026-10-21",
      quoteFingerprint: "b".repeat(64),
    },
    {
      provider: "kling",
      model: "kling-3.0-turbo",
      capabilitiesVersion: "multi-provider-v3",
      priceVersion: "kling30-turbo-720p-20261021",
      currency: "USD",
      unit: "output_second",
      unitPrice: "0.112",
      quantity: "5",
      amount: "0.56",
      resourceUnits: 4,
      available: false,
      reason: "VIDEO_KEY_MISSING",
      keyConfigured: false,
      verification: "documented-no-real-sample",
      sourceUrl: "https://kling.ai/document-api/pricing/base/video.md",
      checkedAt: "2026-10-21",
      quoteFingerprint: "c".repeat(64),
    },
  ],
  recommended: "wan",
  notes: ["不换汇：CNY 与 USD 分别计账、分别展示，不做汇率比较或跨币种合计。"],
  fingerprint: "d".repeat(64),
};

test("the preview path keeps camelCase query names and only sent fields", () => {
  assert.equal(
    routePreviewPath({ storyboardId: "board-1", version: 2, shotIds: ["a", "b"], scope: "episode" }),
    "/api/route/preview?storyboardId=board-1&version=2&shotIds=a%2Cb&scope=episode",
  );
  assert.equal(
    routePreviewPath({ mode: "i2v", durationSeconds: 5, aspectRatio: "16:9" }),
    "/api/route/preview?mode=i2v&durationSeconds=5&aspectRatio=16%3A9&scope=shot",
  );
  assert.equal(routePreviewPath(), "/api/route/preview?scope=shot");
});

test("candidate rows keep the server order and never merge currencies", () => {
  const rows = routeCandidateRows(PREVIEW);
  assert.deepEqual(rows.map((row) => row.provider), ["wan", "fal", "kling"]);
  assert.deepEqual(rows.map((row) => row.amountText), ["3.00 CNY", "0.42 USD", "0.56 USD · 4 VIDEO_UNIT"]);
  assert.deepEqual(rows.map((row) => row.recommended), [true, false, false]);
  assert.equal(rows[2].available, false);
  assert.equal(rows[2].reasonText, "未配置该供应商的 Key");
  assert.equal(rows[2].keyConfigured, false);
  // There is no combined total anywhere: CNY and USD stay separate rows.
  assert.equal(rows.some((row) => "total" in row), false);
  assert.deepEqual(
    [...new Set(rows.map((row) => row.currency))],
    ["CNY", "USD"],
  );
});

test("summary, recommendation and labels describe the same comparison", () => {
  assert.equal(routeRequestSummary(PREVIEW), "单镜头 · t2v · 5 秒 · 16:9");
  assert.equal(routeRecommendedLabel(PREVIEW), "阿里云百炼万相（wan）");
  assert.equal(routeRecommendedLabel({ ...PREVIEW, recommended: null }), "没有可用候选");
  assert.equal(routeScopeLabel("batch"), "批量");
  assert.equal(routeProviderLabel("fal"), "fal 队列中转（fal）");
  assert.equal(routeProviderLabel("sora"), "sora");
  assert.deepEqual(routeNotes(PREVIEW), PREVIEW.notes);
  const notes = routeNotes(PREVIEW);
  notes.push("mutated");
  assert.equal(PREVIEW.notes.length, 1);
});

test("a storyboard request summarises its version and shot count", () => {
  const view = {
    ...PREVIEW,
    scope: "episode",
    request: {
      ...PREVIEW.request,
      mode: null,
      durationSeconds: null,
      aspectRatio: null,
      storyboardId: "board-1",
      storyboardVersion: 3,
      shotIds: ["shot-a", "shot-b"],
    },
  };
  assert.equal(routeRequestSummary(view), "集级 · 分镜 board-1 v3 · 2 个镜头");
});

test("switching is only offered for an available, non-current provider", () => {
  const rows = routeCandidateRows(PREVIEW);
  assert.equal(routeSwitchable(rows[0], "kling"), true);
  assert.equal(routeSwitchable(rows[0], "wan"), false);
  assert.equal(routeSwitchable(rows[2], "wan"), false);
  assert.deepEqual(routeProviderSwitchBody("fal"), { videoProvider: "fal" });
});

test("reason and error text fall back without inventing a success", () => {
  assert.equal(routeReasonText("VIDEO_COST_LIMIT"), "超过单 Job 金额上限");
  assert.equal(routeReasonText("SOMETHING_NEW"), "不可用（SOMETHING_NEW）");
  assert.equal(routeReasonText(null), null);
  assert.equal(routeErrorText("ROUTE_UNAVAILABLE"), "这个请求现在没有可回答的报价，未做任何登记。");
  assert.equal(routeErrorText("SOMETHING_NEW"), "报价预览失败。");
  assert.equal(routeAmountText({ amount: null }), "—");
});
