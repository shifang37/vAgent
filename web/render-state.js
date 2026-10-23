// M6 E3 render state: pure request/body and summary helpers for the film panel.

const PROFILES = new Set(["720p-h264-aac-v1", "1080p-h264-aac-v1"]);
const MODES = new Set(["soft", "burn", "both"]);

export function renderRequestBody(board, track, requestId, options = {}) {
  if (!board?.storyboardId || !requestId) return null;
  const mode = MODES.has(options.mode) ? options.mode : track ? "both" : "soft";
  const profile = PROFILES.has(options.profile) ? options.profile : "720p-h264-aac-v1";
  const body = {
    clientRequestId: requestId,
    scope: "storyboard",
    storyboardId: board.storyboardId,
    profile,
    subtitleMode: mode,
    confirm: true,
  };
  if (track?.trackId) body.trackId = track.trackId;
  if (Array.isArray(options.transitions) && options.transitions.length) {
    body.transitions = options.transitions;
  }
  if (Array.isArray(options.titleCards) && options.titleCards.length) {
    body.titleCards = options.titleCards;
  }
  if (mode !== "soft" && !track?.trackId) return null;
  return body;
}

export function renderRequestPath(sessionId, board) {
  if (!sessionId || !board?.storyboardId) return null;
  return `/api/renders?projectId=${encodeURIComponent(sessionId)}&storyboardId=${encodeURIComponent(board.storyboardId)}`;
}

export function renderStatusLabel(render) {
  const status = render?.status;
  return (
    {
      running: "渲染中",
      succeeded: "已渲染",
      failed: "渲染失败",
    }[status] || "未渲染"
  );
}

export function renderSummary(render) {
  if (!render) return "还没有整片渲染记录。";
  const profile = render.render?.exportProfile;
  const mode = render.render?.subtitle?.mode || render.subtitleMode || "soft";
  const modeLabel = { soft: "软字幕", burn: "烧录字幕", both: "软字幕+烧录" }[mode] || mode;
  const scenes = render.scenes || [];
  const done = scenes.filter((scene) => scene.status === "succeeded").length;
  const failed = scenes.find((scene) => scene.status === "failed");
  const resolution = profile ? `${profile.width}×${profile.height}` : render.profile;
  const duration = render.render?.measurements?.durationSeconds;
  const parts = [
    `${renderStatusLabel(render)} · ${resolution} · ${modeLabel}`,
    scenes.length ? `${done}/${scenes.length} 场完成` : "",
    duration ? `${duration.toFixed(1)} 秒` : "",
    failed ? `失败：${failed.errorCode || failed.failureStage || "未知"}` : "",
  ];
  return parts.filter(Boolean).join(" · ");
}

export function renderSceneRows(render) {
  return (render?.scenes || []).map((scene, index) => ({
    label: `第 ${index + 1} 场`,
    status: renderStatusLabel({ status: scene.status === "succeeded" ? "succeeded" : scene.status }),
    detail:
      scene.status === "failed"
        ? `${scene.errorCode || "渲染失败"}${scene.failureStage ? ` · ${scene.failureStage}` : ""}`
        : scene.durationSeconds
          ? `${scene.durationSeconds.toFixed(1)} 秒`
          : "",
    tone: scene.status === "failed" ? "attention" : scene.status === "succeeded" ? "succeeded" : "pending",
  }));
}

export function renderSidecarUrl(render) {
  return render?.render?.subtitle?.sidecarUrl || null;
}

export function renderErrorLabel(error) {
  const code = error?.code || "RENDER_FAILED";
  const label = {
    RENDER_PROFILE_UNSUPPORTED: "这个导出档位或源规格不受支持。",
    RENDER_SOURCE_MISSING: "缺少已交付镜头、字幕轨或音频。",
    RENDER_TIMELINE_MISMATCH: "输出时长与冻结时间线不一致。",
    RENDER_TIMEOUT: "本地渲染超时。",
    RENDER_ENCODE_FAILED: "本地编码失败。",
    RENDER_VALIDATION_FAILED: "渲染文件未通过校验。",
    LOUDNESS_OUT_OF_RANGE: "混音响度超出容差。",
    TRANSITION_INVALID: "转场类型、时长或边界非法。",
    TITLE_CARD_LIMIT: "标题卡超过 4 条或时间冲突。",
    SUBTITLE_TRACK_MISSING: "需要字幕轨。",
    CONFIRMATION_REQUIRED: "需要显式确认。",
    FONT_MISSING: "包内字体缺失或校验失败。",
  }[code];
  return label ? `${label}（${code}）` : `${code}：${error?.message || "渲染失败"}`;
}
