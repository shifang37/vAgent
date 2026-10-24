// Pure projections for the read-only provider comparison (provider-route-v1).
//
// The server is the authority: this module only turns one preview document into
// rows and labels. It never computes a price, converts a currency or compares two
// currencies with each other. CNY and USD stay side by side, exactly as returned;
// "wan first" is a display order, not a claim that it is cheaper.

export const ROUTE_CONTRACT = "provider-route-v1";
export const ROUTE_SCOPES = ["shot", "episode", "batch"];
export const ROUTE_PROVIDERS = ["wan", "kling", "fal"];
export const ROUTE_VERIFICATION = "documented-no-real-sample";

export const ROUTE_SCOPE_LABELS = {
  shot: "单镜头",
  episode: "集级",
  batch: "批量",
};

export const ROUTE_PROVIDER_LABELS = {
  wan: "阿里云百炼万相（wan）",
  kling: "Kling 新加坡直连（kling）",
  fal: "fal 队列中转（fal）",
};

const ROUTE_REASONS = {
  VIDEO_MODE_OFF: "当前视频模式不登记真实任务（off/mock）",
  VIDEO_PROVIDER_UNAVAILABLE: "该供应商没有已发布条目",
  VIDEO_KEY_MISSING: "未配置该供应商的 Key",
  VIDEO_UNSUPPORTED_MODE: "不支持这个生成模式",
  VIDEO_UNSUPPORTED_SPEC: "不支持这个时长/画幅组合",
  VIDEO_PRICE_UNKNOWN: "价格缺失或价格版本已过期",
  EPISODE_COST_LIMIT: "集级/批量只接受 CNY",
  VIDEO_COST_LIMIT: "超过单 Job 金额上限",
};

const ROUTE_ERRORS = {
  ROUTE_UNAVAILABLE: "这个请求现在没有可回答的报价，未做任何登记。",
  INVALID_ARGUMENTS: "预览参数不完整或互相冲突，未做任何登记。",
  NOT_FOUND: "当前数据目录中没有这个分镜或版本。",
};

export function routeProviderLabel(provider) {
  return ROUTE_PROVIDER_LABELS[provider] || String(provider || "");
}

export function routeScopeLabel(scope) {
  return ROUTE_SCOPE_LABELS[scope] || String(scope || "");
}

export function routeReasonText(code) {
  if (!code) return null;
  return ROUTE_REASONS[code] || `不可用（${code}）`;
}

export function routeErrorText(code) {
  return ROUTE_ERRORS[code] || "报价预览失败。";
}

export function routePreviewPath({
  storyboardId = null,
  version = null,
  shotIds = [],
  mode = null,
  durationSeconds = null,
  aspectRatio = null,
  scope = "shot",
  provider = null,
} = {}) {
  const params = new URLSearchParams();
  if (storyboardId) params.set("storyboardId", storyboardId);
  if (version != null) params.set("version", String(version));
  if (Array.isArray(shotIds) && shotIds.length) params.set("shotIds", shotIds.join(","));
  if (mode) params.set("mode", mode);
  if (durationSeconds != null) params.set("durationSeconds", String(durationSeconds));
  if (aspectRatio) params.set("aspectRatio", aspectRatio);
  if (scope) params.set("scope", scope);
  if (provider) params.set("provider", provider);
  return `/api/route/preview?${params.toString()}`;
}

export function routeAmountText(row) {
  if (!row?.amount) return "—";
  const units = row.resourceUnits != null ? ` · ${row.resourceUnits} VIDEO_UNIT` : "";
  return `${row.amount} ${row.currency}${units}`;
}

export function routeCandidateRows(preview) {
  const candidates = Array.isArray(preview?.candidates) ? preview.candidates : [];
  return candidates.map((item) => {
    const row = {
      provider: item.provider,
      label: routeProviderLabel(item.provider),
      model: item.model || null,
      capabilitiesVersion: item.capabilitiesVersion || null,
      priceVersion: item.priceVersion || null,
      currency: item.currency || null,
      unitPrice: item.unitPrice || null,
      quantity: item.quantity || null,
      amount: item.amount || null,
      resourceUnits: item.resourceUnits == null ? null : item.resourceUnits,
      available: Boolean(item.available),
      reason: item.reason || null,
      reasonText: item.available ? null : routeReasonText(item.reason),
      keyConfigured: Boolean(item.keyConfigured),
      verification: item.verification || null,
      sourceUrl: item.sourceUrl || null,
      checkedAt: item.checkedAt || null,
      recommended: preview?.recommended === item.provider,
    };
    row.amountText = routeAmountText(row);
    return row;
  });
}

export function routeRecommendedLabel(preview) {
  if (!preview?.recommended) return "没有可用候选";
  return routeProviderLabel(preview.recommended);
}

export function routeRequestSummary(preview) {
  const request = preview?.request || {};
  const scope = routeScopeLabel(preview?.scope || request.scope);
  const target = request.storyboardId
    ? `分镜 ${request.storyboardId} v${request.storyboardVersion ?? "?"} · ${
        (request.shotIds || []).length
      } 个镜头`
    : `${request.mode || "按规格"} · ${request.durationSeconds ?? "?"} 秒 · ${
        request.aspectRatio || "输入帧画幅"
      }`;
  return `${scope} · ${target}`;
}

export function routeNotes(preview) {
  return Array.isArray(preview?.notes) ? [...preview.notes] : [];
}

// Switching is only a write into the existing settings path; it never edits a Job
// and never changes a candidate row in this preview.
export function routeSwitchable(row, currentProvider) {
  return Boolean(row?.available) && row.provider !== currentProvider;
}

export function routeProviderSwitchBody(provider) {
  return { videoProvider: provider };
}
