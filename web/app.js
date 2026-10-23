import { mediaContentPath, mergeJobs, upsertJob } from "./job-state.js";
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
} from "./film-state.js";
import {
  assetContentPath,
  batchSummary,
  estimateQuery,
  groupJobsByShot,
  jobMediaUrl,
  playList,
  revisionToken,
  shotState,
  shotThumbnail,
  shotVersions,
} from "./storyboard-state.js";
import {
  latestSubtitle,
  renderModeLabel,
  subtitleDownloadUrl,
  subtitleErrorLabel,
  subtitleExportBody,
  subtitleSaveBody,
  subtitleSummary,
  subtitleVersions,
} from "./subtitle-state.js";
import {
  renderErrorLabel,
  renderRequestBody,
  renderRequestPath,
  renderSceneRows,
  renderSidecarUrl,
  renderStatusLabel,
  renderSummary,
} from "./render-state.js";

import {
  seasonEpisodeRows,
  seasonMeta,
  seasonMetaText,
  seasonMembers,
  seasonPickerOptions,
  seasonSaveBody,
  seasonValidation,
} from "./season-state.js";

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
  sceneGeneration,
  sceneSummary,
  shotStatusLabel,
  subtitleCueAt,
} from "./episode-state.js";

const $ = (selector) => document.querySelector(selector);
const paths = {
  "plus-square":
    '<rect x="4" y="4" width="16" height="16" rx="5"/><path d="M12 8v8m-4-4h8"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  episode: '<path d="M4 6h16M4 12h16M4 18h10"/>',
  folder:
    '<path d="M3 7V5a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7h18"/>',
  layers: '<path d="m12 3 9 5-9 5-9-5 9-5Zm-9 9 9 5 9-5M3 16l9 5 9-5"/>',
  search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
  help: '<circle cx="12" cy="12" r="9"/><path d="M9.5 9a2.5 2.5 0 0 1 5 0c0 2-2.5 2-2.5 4m0 3h.01"/>',
  settings:
    '<path d="m9 3-.6 2.3-2 .9L4 5.6 2 9l1.7 1.7v2.6L2 15l2 3.4 2.4-.6 2 .9L9 21h4l.6-2.3 2-.9 2.4.6 2-3.4-1.7-1.7v-2.6L20 9l-2-3.4-2.4.6-2-.9L13 3Z"/><circle cx="11" cy="12" r="3"/>',
  close: '<path d="m6 6 12 12M6 18 18 6"/>',
  chevron: '<path d="m7 10 5 5 5-5"/>',
  sliders:
    '<path d="M4 7h5m4 0h7M4 17h9m4 0h3"/><circle cx="11" cy="7" r="2"/><circle cx="15" cy="17" r="2"/>',
  clip: '<path d="m9 12 5-5a3 3 0 0 1 4 4l-7 7a5 5 0 0 1-7-7l7-7m-3 9 6-6"/>',
  spark:
    '<path d="m12 3 2.4 6.6L21 12l-6.6 2.4L12 21l-2.4-6.6L3 12l6.6-2.4L12 3Z"/>',
  arrow: '<path d="M12 19V5m-6 6 6-6 6 6"/>',
  film: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 4v16M17 4v16M3 9h4m-4 6h4m10-6h4m-4 6h4"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5"/>',
  box: '<path d="m12 3 9 5v9l-9 5-9-5V8l9-5Zm0 10 9-5M12 13 3 8m9 5v9M7 5.8l10 5.5"/>',
  book: '<path d="M12 5C8 2 3 4 3 4v15s5-2 9 1c4-3 9-1 9-1V4s-5-2-9 1v15"/>',
  file: '<path d="M14 3H5v18h14V8l-5-5Zm0 0v5h5M8 12h8m-8 4h6"/>',
  download: '<path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5"/>',
  clapper:
    '<path d="M3 9h18v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V9Zm2-5h14a2 2 0 0 1 2 2v3H3V6a2 2 0 0 1 2-2Zm2.4 0 3 5m3.2-5 3 5"/>',
  image:
    '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="8.5" cy="9" r="1.5"/><path d="m4 18 5-5 4 4 3-2.5 4 3.5"/>',
  chart: '<path d="M5 20V10m5 10V4m5 16v-7m5 7V8"/>',
  season:
    '<path d="M4 5h16v14H4zM4 9h16M9 9v10M13 12h4m-4 3h4"/>',
};
function icons(root = document) {
  root.querySelectorAll("[data-icon]").forEach((el) => {
    el.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true">${paths[el.dataset.icon] || paths.file}</svg>`;
  });
}
const escapeHTML = (value) =>
  String(value).replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const templates = {
  brand:
    "为一家独立咖啡店创作一支 30 秒品牌短片。面向城市上班族，暖色调、自然光，突出「在忙碌中留一点时间给自己」。请读取相关 Skill，保存项目需求和创作方案。",
  life: "策划一支 60 秒周末生活 Vlog：清晨散步、逛花店、在家做饭。风格安静真实，请设计叙事节奏和镜头，并保存方案。",
  product:
    "为便携蓝牙音箱策划 30 秒种草视频，面向喜欢户外的年轻人，突出便携与陪伴感。请保存受众与风格，设计开场和镜头脚本。",
  story:
    "为「雨天收到多年未见的朋友寄来的明信片」写 30 秒分镜，温柔克制。读取 shot-description Skill，如有 MCP 时长工具则校验镜头总时长，保存分镜。",
};
const statusNames = {
  running: "正在执行",
  waiting_external: "等待外部结果",
  completed: "已完成",
  cancelled: "已停止",
  failed: "执行失败",
  interrupted: "已中断",
};
const artifactNames = { all: "全部产物", brief: "方案", script: "脚本", storyboard: "分镜" };
const jobStatusNames = {
  pending_submit: "已登记", submitting: "正在提交", queued: "排队中",
  running: "模拟生成中", succeeded: "模拟完成", failed: "任务失败", unknown: "提交待核实",
};
const retryingJobs = new Set();
function contentLimitsText(project) {
  return Object.entries(project?.contentLimits || {})
    .map(([kind, maximum]) => `${artifactNames[kind] || kind}：${maximum} 个非空白字符`)
    .join("；") || "未设置";
}
function contentCheckText(version) {
  const checked = version.contentCheck;
  if (!checked) return "此历史版本未记录字数校验。";
  return `正文 ${checked.characters} 个非空白字符${checked.maxCharacters == null ? "，未设置上限" : ` / 上限 ${checked.maxCharacters}，保存前已校验`}。含标点、英文、数字和 Markdown 标记，空白不计。`;
}
let health = null,
  csrf = null,
  sessions = [],
  activeId = null,
  snapshot = null;
let reference = null,
  stream = null,
  submitting = false,
  selectedArtifact = null,
  pendingRequest = null;
let viewGeneration = 0;
let dialogGeneration = 0,
  configSubmitting = false;
let panelMode = null,
  storyboardOverride = null,
  assets = [],
  assetsLoaded = false,
  usageReport = null,
  assetsScopeOnly = false,
  uploadingImage = false;
let shotSelection = new Set(),
  shotBusy = new Set(),
  pendingBatch = null,
  sequence = { items: null, index: -1 };
// M7 E1 season panel: a read view plus one local edit buffer. Saving only writes
// season membership; it never registers a Job or calls a model.
let seasonView = null,
  seasonList = [],
  seasonEditing = null,
  seasonSelection = [],
  seasonDraftTitle = "",
  seasonBusy = false,
  seasonError = null;
const IMAGE_TYPES = ["image/png", "image/jpeg", "image/webp"];
const MAX_IMAGE_BYTES = 20 * 1024 * 1024;
const currentRun = () => snapshot?.run;
const isRunning = () => ["running", "waiting_external"].includes(currentRun()?.status);
function toast(message) {
  $("#toast").textContent = message;
  $("#toast").hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => ($("#toast").hidden = true), 4500);
}
async function readJson(response) {
  let value;
  try {
    value = await response.json();
  } catch {
    throw new Error("无法连接 Agent 服务，请使用 vagent web 启动。");
  }
  if (!response.ok)
    throw Object.assign(new Error(value.error?.message || "请求失败"), {
      code: value.error?.code,
    });
  return value;
}
async function api(path, options = {}) {
  const response = await fetch(path, {
    cache: "no-store",
    ...options,
    headers: {
      ...(options.method
        ? { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" }
        : {}),
      ...options.headers,
    },
  });
  return readJson(response);
}
const post = (path, body = {}) =>
  api(path, { method: "POST", body: JSON.stringify(body) });
async function apiText(path) {
  const response = await fetch(path, { cache: "no-store" });
  if (!response.ok) throw new Error("字幕暂时无法读取。");
  return response.text();
}
// An upload must not set Content-Type: the browser adds the multipart boundary.
const upload = (path, form) =>
  fetch(path, {
    method: "POST",
    cache: "no-store",
    body: form,
    headers: { "X-CSRF-Token": csrf || "" },
  }).then(readJson);
function rememberSelection() {
  try {
    if (activeId) sessionStorage.setItem("vagent.active-session", activeId);
    else sessionStorage.removeItem("vagent.active-session");
  } catch {}
}
function updateSend() {
  $("#send").disabled =
    submitting || (!isRunning() && (!health || !$("#prompt").value.trim()));
  $("#send").setAttribute(
    "aria-label",
    isRunning() ? "停止 Agent" : "发送创作需求",
  );
  $("#send").innerHTML = isRunning()
    ? '<span aria-hidden="true">■</span>'
    : `<svg viewBox="0 0 24 24" aria-hidden="true">${paths.arrow}</svg>`;
  $("#mode").disabled = isRunning() || submitting;
  $("#prompt").disabled = submitting;
}
async function refreshSessions() {
  sessions = (await api("/api/sessions")).sessions;
  renderSessions();
}
function renderSessions() {
  const needle = $("#search").value.trim().toLowerCase();
  const list = sessions.filter(
    (s) => s.title.toLowerCase().includes(needle) || s.id.includes(needle),
  );
  $("#sessions").innerHTML = list.length
    ? list
        .map(
          (s) =>
            `<button class="session ${s.id === activeId ? "selected" : ""}" data-session="${escapeHTML(s.id)}">${escapeHTML(s.title)}</button>`,
        )
        .join("")
    : '<p class="empty">没有匹配的会话。<br>从一个想法开始吧。</p>';
}
function newChat() {
  if (submitting) {
    toast("正在提交，请稍候。");
    return;
  }
  viewGeneration++;
  stream?.close();
  stream = null;
  activeId = null;
  snapshot = null;
  reference = null;
  pendingRequest = null;
  resetStoryboardUi();
  $("#attachment").hidden = true;
  $("#prompt").value = "";
  $("#artifact-panel").hidden = true;
  rememberSelection();
  render();
  $("#prompt").focus();
}
function connectEvents(id, generation) {
  stream?.close();
  const source = new EventSource(`/api/events?sessionId=${encodeURIComponent(id)}`);
  stream = source;
  source.addEventListener("snapshot", (event) => {
    if (generation !== viewGeneration || stream !== source) return;
    try {
      const next = JSON.parse(event.data);
      const runChanged = next.run?.status !== snapshot?.run?.status;
      applySnapshot(next);
      renderConnection();
      render();
      if (runChanged) refreshSessions().catch((error) => toast(error.message));
    } catch {
      toast("执行状态读取失败，请刷新页面。");
    }
  });
  source.addEventListener("job.updated", (event) => {
    if (generation !== viewGeneration || stream !== source) return;
    try {
      const update = JSON.parse(event.data);
      if (update.sessionId !== id || snapshot?.id !== id) return;
      const jobs = upsertJob(snapshot.jobs || [], update.job, id);
      if (jobs === snapshot.jobs) return;
      snapshot.jobs = jobs;
      renderJobs();
      // The shot panel shows the same Jobs, so it has to follow live updates too.
      // Playback keeps its player: it re-renders when the sequence stops instead.
      if (sequence.index < 0) renderStoryboardPanel();
    } catch {
      connectEvents(id, generation);
    }
  });
  for (const kind of ["run.waiting", "run.resumed", "run.resume_blocked", "run.completed"]) {
    source.addEventListener(kind, (event) => {
      if (generation !== viewGeneration || stream !== source) return;
      try {
        const update = JSON.parse(event.data);
        if (update.sessionId !== id || update.snapshot?.id !== id) return;
        applySnapshot(update.snapshot);
        renderConnection();
        render();
        if (kind === "run.completed") refreshSessions().catch((error) => toast(error.message));
      } catch {
        connectEvents(id, generation);
      }
    });
  }
  source.addEventListener("assistant.delta", (event) => {
    if (generation !== viewGeneration || stream !== source) return;
    try {
      const delta = JSON.parse(event.data);
      const run = currentRun();
      if (run?.status !== "running" || run.id !== delta.runId || delta.step < run.modelSteps) return;
      const previous = snapshot.draft;
      const sameStep = previous?.runId === delta.runId && previous.step === delta.step;
      if (sameStep && delta.sequence <= previous.sequence) return;
      if (delta.step !== run.modelSteps || delta.sequence !== (sameStep ? previous.sequence + 1 : 1)) {
        connectEvents(id, generation); // Reconnect starts with a complete current snapshot.
        return;
      }
      snapshot.draft = { ...delta, text: (sameStep ? previous.text : "") + delta.text };
      renderDraft();
    } catch {
      connectEvents(id, generation);
    }
  });
  source.onerror = () => {
    if (generation === viewGeneration && stream === source)
      $("#connection-label").textContent = "连接中断 · 正在重连";
  };
}
function applySnapshot(next) {
  next.jobs = mergeJobs(snapshot?.id === next.id ? snapshot.jobs || [] : [], next.jobs || [], next.id);
  snapshot = next;
  pruneShotSelection();
}
function pruneShotSelection() {
  const known = new Set((snapshot?.storyboard?.shots || []).map((shot) => shot.id));
  for (const id of [...shotSelection]) if (!known.has(id)) shotSelection.delete(id);
}
function resetStoryboardUi() {
  shotSelection = new Set();
  shotBusy = new Set();
  pendingBatch = null;
  sequence = { items: null, index: -1 };
  panelMode = null;
  storyboardOverride = null;
  $("#storyboard-panel").hidden = true;
  $("#artifact-panel").hidden = true;
  $("#episode-panel").hidden = true;
  $("#season-panel").hidden = true;
  seasonView = null;
  seasonList = [];
  seasonEditing = null;
  seasonSelection = [];
  seasonDraftTitle = "";
  seasonError = null;
}
async function selectSession(id) {
  const generation = ++viewGeneration;
  stream?.close();
  const next = await api(`/api/sessions/${encodeURIComponent(id)}`);
  if (generation !== viewGeneration) return;
  activeId = id;
  applySnapshot(next);
  resetStoryboardUi();
  // A session that already has a storyboard opens its shot panel; the user can close it.
  if (next.storyboard) panelMode = "storyboard";
  pendingRequest = null;
  reference = null;
  $("#attachment").hidden = true;
  $("#prompt").value = "";
  $("#artifact-panel").hidden = true;
  rememberSelection();
  render();
  connectEvents(id, generation);
}
function artifactCard(artifact) {
  const last = artifact.versions.at(-1);
  return `<button class="artifact-card" data-artifact="${escapeHTML(artifact.id)}"><span data-icon="file"></span><span><strong>${escapeHTML(last.title)}</strong><small>${escapeHTML(artifact.kind)} · v${last.version} · 已保存</small></span><span>↗</span></button>`;
}
function jobCard(job, expanded) {
  const request = job.request, spec = request.spec;
  const live = job.mode === "live";
  const statusNames = live ? {
    pending_submit: "待提交", submitting: "正在提交", queued: job.lastProviderStatus ? "云端排队中" : "已受理，等待查询",
    running: "云端生成中", downloading: job.download?.phase === "writing" ? "正在下载" : "等待下载", download_failed: "下载失败",
    succeeded: "已完成", failed: "任务失败", unknown: "提交待核实",
  } : jobStatusNames;
  const paused = job.queryState === "paused", retrying = job.queryState === "retrying";
  const repairing = job.download?.repair && ["pending", "writing", "prepared"].includes(job.download.phase);
  const state = job.runtimeBlock ? "需要处理" : paused ? "查询已暂停" : retrying ? "查询重试中"
    : repairing ? "正在修复下载" : live && job.status === "succeeded" && !job.mediaAvailable ? "媒体不可用"
    : statusNames[job.status] || job.status;
  const tone = paused || retrying || job.status === "unknown" || (live && job.status === "succeeded" && !job.mediaAvailable) ? "attention" : job.status;
  const sources = (request.sourceRefs || []).map((ref) => {
    const artifact = snapshot?.artifacts.find((item) => item.id === ref.artifactId);
    const title = artifact?.versions.find((item) => item.version === ref.version)?.title || ref.artifactId;
    return `<button data-artifact="${escapeHTML(ref.artifactId)}" data-version="${ref.version}">${escapeHTML(title)} · v${ref.version}</button>`;
  }).join("、") || "直接使用提示词";
  const notice = paused || retrying
    ? `最近确认状态：${statusNames[job.status] || job.status}。${paused ? "恢复查询后可继续获取结果。" : "正在重试状态查询。"}`
    : job.status === "unknown" ? "提交结果尚未确认，需要核实。任务不会自动重新提交。"
    : live && job.status === "downloading" ? "云端已生成，正在保存和校验本地媒体。完成后可播放、拖动和下载。"
    : job.status === "succeeded" ? (live ? (job.mediaAvailable ? "本地媒体已交付。" : "本地媒体当前不可用。") : "模拟流程已完成，仅提供结果描述，无可播放或下载的视频。")
    : ["pending_submit", "submitting", "queued", "running"].includes(job.status) ? "服务运行期间持续推进，关闭页面不影响任务。" : "";
  const mediaPath = mediaContentPath(job);
  const downloadError = job.download?.error;
  return `<article class="job-card" data-job-id="${escapeHTML(job.jobId)}">
    <div class="job-heading"><strong><span data-icon="film"></span>${live ? "真实视频任务" : "模拟视频任务"}</strong><span class="job-state ${escapeHTML(tone)}">${escapeHTML(state)}</span></div>
    <p class="job-id">Job <code>${escapeHTML(job.jobId)}</code></p>
    <div class="job-spec"><span class="simulation-label">${live ? "真实生成" : "模拟 · 无真实媒体"}</span><span>${escapeHTML(request.model)} · ${spec.durationSeconds} 秒 · ${escapeHTML(spec.resolution)} · ${escapeHTML(spec.aspectRatio)}</span></div>
    ${job.cost ? `<p class="job-notice">生成估算：${escapeHTML(job.cost.estimate.amount)} ${escapeHTML(job.cost.estimate.currency)} · 实际费用：${job.cost.actual.status === "reported" ? escapeHTML(job.cost.actual.amount) + " CNY" : "未知，待账单核实"}</p>` : ""}
    <p class="job-sources">来源：${sources}</p>
    <details class="job-request" ${expanded ? "open" : ""}><summary>生成提示词</summary><p>${escapeHTML(request.prompt)}</p></details>
    ${job.result && (!live || job.mediaAvailable) ? `<p class="job-result">${escapeHTML(job.result.summary)}</p>` : ""}
    ${mediaPath ? `<div class="job-media" data-media-id="${escapeHTML(job.mediaRefs[0].mediaId)}"><video controls preload="metadata" playsinline src="${mediaPath}" aria-label="生成的视频"></video><div class="media-controls"><small class="media-state" role="status">正在读取视频信息…</small><a class="secondary" href="${mediaPath}?download=1" download="${escapeHTML(job.mediaRefs[0].mediaId)}.mp4">下载 MP4</a></div></div>` : ""}
    ${notice ? `<p class="job-notice">${escapeHTML(notice)}</p>` : ""}
    ${job.download ? `<p class="job-notice">累计下载 ${job.download.attempts} 次${job.download.nextAttemptAt ? ` · 下次尝试 ${escapeHTML(new Date(job.download.nextAttemptAt).toLocaleString())}` : ""}${repairing ? " · 沿用原媒体 ID，校验原文件哈希" : ""}</p>` : ""}
    ${job.error ? `<p class="job-error">${escapeHTML({ submit: "提交", query: "查询", generate: "生成", download: "下载" }[job.error.stage] || job.error.stage)} · ${escapeHTML(job.error.code)}<br>${escapeHTML(job.error.message)}</p>` : ""}
    ${job.runtimeBlock ? `<p class="job-error">${escapeHTML(job.runtimeBlock.code)} · ${escapeHTML(job.runtimeBlock.message)}</p>` : ""}
    ${downloadError && downloadError.code !== job.error?.code ? `<p class="job-error">下载 · ${escapeHTML(downloadError.code)}<br>${escapeHTML(downloadError.message)}</p>` : ""}
    <div class="job-footer"><small>更新于 ${escapeHTML(new Date(job.updatedAt).toLocaleString())}</small>${job.canRetryQuery ? `<button class="secondary" data-retry-job="${escapeHTML(job.jobId)}" ${retryingJobs.has(job.jobId) ? "disabled" : ""}>${retryingJobs.has(job.jobId) ? "正在恢复…" : "恢复查询"}</button>` : ""}${job.canRetryDownload ? `<button class="secondary" data-retry-download="${escapeHTML(job.jobId)}" ${retryingJobs.has(job.jobId) ? "disabled" : ""}>${retryingJobs.has(job.jobId) ? "正在恢复…" : "重试原下载"}</button>` : ""}</div>
  </article>`;
}
function renderJobs() {
  const list = $("#job-list");
  if (!list) return;
  const expanded = new Set([...list.querySelectorAll(".job-card")]
    .filter((card) => card.querySelector("details")?.open).map((card) => card.dataset.jobId));
  const conversation = $("#conversation");
  const followLatest = conversation.scrollHeight - conversation.scrollTop - conversation.clientHeight < 80;
  const retained = new Set();
  for (const [index, job] of (snapshot?.jobs || []).entries()) {
    const template = document.createElement("template");
    template.innerHTML = jobCard(job, expanded.has(job.jobId));
    const next = template.content.firstElementChild;
    let card = [...list.children].find((node) => node.dataset.jobId === job.jobId);
    if (!card) card = next;
    else {
      const media = card.querySelector(".job-media"), incoming = next.querySelector(".job-media");
      if (media && incoming && media.dataset.mediaId === incoming.dataset.mediaId) {
        // Keep the playing video connected to the DOM while other Job fields change.
        for (const node of [...card.children]) if (node !== media) node.remove();
        while (next.firstElementChild !== incoming) media.before(next.firstElementChild);
        incoming.remove();
        card.append(...next.children);
      } else card.replaceChildren(...next.children);
    }
    if (list.children[index] !== card) list.insertBefore(card, list.children[index] || null);
    retained.add(card);
    bindMedia(card, job);
  }
  for (const node of [...list.children]) if (!retained.has(node)) node.remove();
  list.hidden = !snapshot?.jobs?.length;
  icons(list);
  if (followLatest) conversation.scrollTop = conversation.scrollHeight;
}
function bindMedia(card, job) {
  const video = card.querySelector("video");
  if (!video || video.dataset.bound) return;
  video.dataset.bound = "true";
  const status = card.querySelector(".media-state");
  video.addEventListener("loadedmetadata", () => {
    status.textContent = `${video.videoWidth} × ${video.videoHeight} · ${video.duration.toFixed(1)} 秒`;
  });
  video.addEventListener("error", async () => {
    status.textContent = "视频暂时无法播放，正在核对本地文件…";
    try {
      const current = await api(`/api/jobs/${encodeURIComponent(job.jobId)}`);
      if (snapshot && current.sessionId === activeId) {
        snapshot.jobs = upsertJob(snapshot.jobs || [], current, activeId);
        if (!current.mediaAvailable) { renderJobs(); return; }
      }
      status.textContent = "浏览器无法播放此文件，可下载 MP4 检查。";
    } catch { status.textContent = "无法读取本地视频，请检查服务连接。"; }
  });
}
function draftHTML() {
  return snapshot?.draft?.text
    ? `<div id="assistant-draft" class="message draft" aria-busy="true"><div class="agent-name"><img src="./mark.svg" alt="" />vagent <span class="demo-label">正在回复 · 草稿</span></div><span class="draft-text">${escapeHTML(snapshot.draft.text)}</span></div>`
    : "";
}
function renderDraft() {
  const conversation = $("#conversation");
  const followLatest = conversation.scrollHeight - conversation.scrollTop - conversation.clientHeight < 80;
  const existing = $("#assistant-draft .draft-text");
  if (existing) existing.textContent = snapshot.draft.text;
  else {
    const after = conversation.querySelector(".artifact-card, .job-list, .run-details");
    if (after) after.insertAdjacentHTML("beforebegin", draftHTML());
    else conversation.insertAdjacentHTML("beforeend", draftHTML());
  }
  if (followLatest) conversation.scrollTop = conversation.scrollHeight;
}
function render() {
  const conversation = $("#conversation");
  const followLatest =
    conversation.scrollHeight -
      conversation.scrollTop -
      conversation.clientHeight <
    80;
  const chatting = Boolean(snapshot?.messages.length || snapshot?.run || snapshot?.jobs?.length);
  document.body.classList.toggle("chat-open", chatting);
  $("#welcome").hidden = chatting;
  $("#inspiration").hidden = chatting;
  $("#conversation").hidden = !chatting;
  $("#workspace").classList.toggle("chat", chatting);
  $("#page-title").textContent =
    sessions.find((s) => s.id === activeId)?.title ||
    (activeId ? "创作会话" : "新建创作");
  $("#project-label").textContent = activeId
    ? `项目 ${activeId.slice(0, 8)}`
    : "新项目";
  const detailsOpen = $("#conversation .run-details")?.open;
  const oldJobs = $("#job-list");
  const transcript =
    (snapshot?.messages || [])
      .map(
        (m) =>
          `<div class="message ${m.role === "user" ? "user" : ""}">${m.role === "assistant" ? '<div class="agent-name"><img src="./mark.svg" alt="" />vagent <span class="demo-label">Agent 回复</span></div>' : ""}${escapeHTML(m.text)}</div>`,
      )
      .join("") + draftHTML() + (snapshot?.artifacts || []).map(artifactCard).join("");
  if (oldJobs) {
    // Reconnect snapshots and Run completion must not detach a playing video.
    for (const node of [...conversation.children]) if (node !== oldJobs) node.remove();
    oldJobs.insertAdjacentHTML("beforebegin", transcript);
  } else {
    conversation.innerHTML = transcript + '<section id="job-list" class="job-list" aria-label="视频任务" hidden></section>';
  }
  renderJobs();
  const run = currentRun();
  if (run) {
    const events = (run.events || []).filter(
      (e) => e.type === "tool.completed",
    );
    $("#conversation").insertAdjacentHTML(
      "beforeend",
      `<details class="run-details" ${detailsOpen ? "open" : ""}><summary>工具执行 · ${events.length} 次结果</summary><p>${events.map((e) => `${e.ok ? "✓" : "×"} ${escapeHTML(e.name)}${e.errorCode ? ` · ${escapeHTML(e.errorCode)}` : ""}`).join("<br>") || "暂未调用工具"}</p></details>`,
    );
    $("#run-status").hidden = false;
    $("#run-status").innerHTML =
      `<span class="run-state ${run.status}">${statusNames[run.status] || run.status}</span><span>模型 ${run.modelSteps} 步 · 工具 ${run.toolCalls} 次</span><button data-action="observe">查看编排</button>${run.resumable && (!isRunning() || run.waitResumeError) ? '<button data-action="resume">从检查点继续</button>' : ""}${run.errorCode ? `<p>${escapeHTML(run.errorCode)} · ${escapeHTML(run.answer)}</p>` : ""}${run.status === "waiting_external" ? `<p class="wait-notice">Agent 正在等待${snapshot.wait?.resource?.kind === "job" ? `任务 ${escapeHTML(snapshot.wait.resource.id)}` : "外部结果"}。等待期间不调用模型；停止 Agent 后，Job 仍会继续。${snapshot.wait?.deadlineAt ? `本次等待截止：${escapeHTML(new Date(snapshot.wait.deadlineAt).toLocaleString())}。` : ""}</p>` : ""}${run.waitResumeError ? `<p>${escapeHTML(run.waitResumeError.code)} · ${escapeHTML(run.waitResumeError.message)}</p>` : ""}`;
  } else $("#run-status").hidden = true;
  icons($("#conversation"));
  if (followLatest) conversation.scrollTop = conversation.scrollHeight;
  renderSessions();
  updateSend();
  renderObserve();
  renderStoryboardPanel();
  renderEpisodePanel();
  renderSeasonPanel();
}
function metric(label, value) {
  return `<div class="metric"><small>${label}</small><strong>${value ?? "—"}</strong></div>`;
}
function renderObserve() {
  if ($("#observe-panel").hidden) return;
  const run = currentRun(),
    project = snapshot?.project;
  const usage = run?.usage;
  const context = (run?.events || [])
    .filter((e) => e.type === "context.prepared")
    .at(-1);
  const value = (v) =>
    v === null || v === undefined ? "未知" : v.toLocaleString();
  $("#observe-panel").innerHTML =
    `<div class="panel-heading"><strong>编排观测</strong><button class="icon-button" data-action="close-observe" aria-label="关闭编排观测" data-icon="close"></button></div>
    <p class="meta">${run ? `Run ${escapeHTML(run.id)}` : "发送需求后显示真实执行数据"}</p>
    <h3>上下文管理</h3><div class="metrics">${metric("输入字节", value(run?.contextBytes))}${metric("预算字节", value(health?.contextBudgetBytes))}${metric("裁剪历史消息", value(run?.droppedMessages))}${metric("当前输入消息", value(context?.messageCount))}</div>
    <p class="meta">按完整轮次裁剪；持久记忆保留，原始历史不删除。</p>
    <h3>项目记忆 <small>revision ${project?.revision ?? "—"}</small></h3>
    <dl class="memory">${["goal", "audience", "style", "constraints"].map((key, i) => `<dt>${["目标", "受众", "风格", "约束"][i]}</dt><dd>${escapeHTML(Array.isArray(project?.[key]) ? project[key].join("；") || "暂无" : project?.[key] || "暂无")}</dd>`).join("")}<dt>正文上限</dt><dd>${escapeHTML(contentLimitsText(project))}</dd></dl>
    <h3>执行计划</h3><ol class="plan-list">${(project?.plan || []).map((p) => `<li>${escapeHTML(p.text)} <small>${escapeHTML(p.status)}</small></li>`).join("") || "<li>暂无计划</li>"}</ol>
    <h3>Skills · 按需读取</h3>${(health?.skills || []).map((s) => `<div class="capability"><strong>${escapeHTML(s.name)}</strong><small>${escapeHTML(s.description)}<br>版本 ${escapeHTML(s.version)}</small></div>`).join("")}${(health?.skillDiagnostics || []).map((d) => `<p class="meta">用户 skill 未加载：${escapeHTML(d.code)} · ${escapeHTML(d.name)}</p>`).join("")}<p class="meta">默认在内置根之后合并 VAGENT_HOME/skills；显式 VAGENT_SKILLS_DIR 仍整体替换，坏文件只诊断、不使应用启动失败。</p>
    <h3>MCP 服务</h3>${(health?.mcp || []).map((s) => `<div class="capability"><strong>${escapeHTML(s.name)} · 已连接</strong><small>${escapeHTML(s.transport)} / ${escapeHTML(s.protocolVersion)}<br>${s.tools.map(escapeHTML).join("<br>")}</small></div>`).join("") || '<p class="meta">未启用 MCP 服务</p>'}
    <h3>工具目录</h3>${(health?.tools || []).map((t) => `<div class="tool-line"><span>${escapeHTML(t.name)}</span><small>${t.effect === "read" ? "读取" : "写入"} · ${escapeHTML(t.source)}</small></div>`).join("")}
    <h3>模型用量</h3><div class="metrics">${metric("已知输入 Token", value(usage?.observedInputTokens))}${metric("已知输出 Token", value(usage?.observedOutputTokens))}${metric("缓存命中 Token", value(usage?.cacheHitTokens))}${metric("缓存命中率", usage?.cacheHitRate == null ? "未知" : `${(usage.cacheHitRate * 100).toFixed(1)}%`)}</div>
    <h3>执行时间</h3><div class="metrics">${metric("活动时间（秒）", run?.activeSeconds == null ? "—" : run.activeSeconds.toFixed(1))}${metric("外部等待（秒）", run ? ((run.externalWaitSeconds || 0) + (run.externalWaitStartedAt ? Math.max(0, (Date.now() - Date.parse(run.externalWaitStartedAt)) / 1000) : 0)).toFixed(1) : "—")}</div>
    <p class="meta">${usage?.tokenUsageComplete ? "本次调用 Token 用量完整" : "未知用量不计作零消耗"}。Redis ${health?.answerCacheEnabled ? "可用于只读任务" : "回答缓存未启用"}。</p>
    ${Array.isArray(run?.toolTrace) ? `<h3>工具输入与结果</h3>${run.toolTrace.map((t) => `<details class="trace-item"><summary>${escapeHTML(t.name)}</summary><small>输入</small><pre>${escapeHTML(JSON.stringify(t.arguments, null, 2))}</pre><small>结果</small><pre>${escapeHTML(t.result || "等待工具完成")}</pre></details>`).join("") || '<p class="meta">暂无工具调用</p>'}` : ""}
    <h3>执行事件</h3><div class="event-list">${(run?.events || []).map((e) => `<div><small>#${e.sequence}</small> ${escapeHTML(e.type)} ${escapeHTML(e.name || "")}${e.ok === false ? " ×" : ""}</div>`).join("") || "暂无事件"}</div>`;
  icons($("#observe-panel"));
}
const storyboard = () => storyboardOverride || snapshot?.storyboard || null;
const episode = () => snapshot?.episode || null;
const shotIdOf = (event) => event.target.closest("[data-shot-id]")?.dataset.shotId || null;

function selectedShots() {
  return (storyboard()?.shots || []).filter((shot) => shotSelection.has(shot.id));
}

function toggleAllShots() {
  const shots = storyboard()?.shots || [];
  const all = shots.length > 0 && shotSelection.size === shots.length;
  shotSelection = all ? new Set() : new Set(shots.map((shot) => shot.id));
  renderStoryboardPanel();
}

async function loadAssets() {
  assets = (await api("/api/assets")).assets;
  assetsLoaded = true;
  return assets;
}

function panelHeading(title, action) {
  return `<div class="panel-heading"><strong>${title}</strong><button class="icon-button" data-action="${action}" aria-label="关闭面板" data-icon="close"></button></div>`;
}

async function refreshSession(id) {
  if (!id) return null;
  const next = await api(`/api/sessions/${encodeURIComponent(id)}`);
  if (snapshot?.id === id) {
    applySnapshot(next);
    if (storyboardOverride?.storyboardId) {
      try {
        storyboardOverride = await api(
          `/api/storyboards/${encodeURIComponent(storyboardOverride.storyboardId)}?projectId=${encodeURIComponent(id)}`,
        );
      } catch {
        storyboardOverride = null;
      }
    }
    render();
  }
  return next;
}

function openStoryboardPanel() {
  panelMode = "storyboard";
  $("#artifact-panel").hidden = true;
  $("#observe-panel").hidden = true;
  $("#episode-panel").hidden = true;
  $("#season-panel").hidden = true;
  $("#storyboard-panel").hidden = false;
  renderStoryboardPanel();
}

function openInspector(mode) {
  panelMode = mode;
  $("#storyboard-panel").hidden = true;
  $("#episode-panel").hidden = true;
  $("#season-panel").hidden = true;
  $("#observe-panel").hidden = true;
  $("#artifact-panel").hidden = false;
}

function openEpisodePanel() {
  panelMode = "episode";
  $("#artifact-panel").hidden = true;
  $("#observe-panel").hidden = true;
  $("#storyboard-panel").hidden = true;
  $("#season-panel").hidden = true;
  $("#episode-panel").hidden = false;
  renderEpisodePanel();
}

async function openSceneStoryboard(storyboardId) {
  if (!storyboardId || !activeId) return;
  try {
    storyboardOverride = await api(
      `/api/storyboards/${encodeURIComponent(storyboardId)}?projectId=${encodeURIComponent(activeId)}`,
    );
  } catch (error) {
    toast(error.message);
    return;
  }
  panelMode = "storyboard";
  $("#artifact-panel").hidden = true;
  $("#observe-panel").hidden = true;
  $("#episode-panel").hidden = true;
  $("#season-panel").hidden = true;
  $("#storyboard-panel").hidden = false;
  filmLoadedKey = null;
  filmState = null;
  filmError = null;
  subtitleLoadedKey = null;
  subtitleState = null;
  subtitleError = null;
  renderStoryboardPanel();
}

async function showAssets() {
  openInspector("assets");
  $("#artifact-panel").innerHTML = `${panelHeading("素材库", "close-artifact")}<p class="meta">正在读取素材库…</p>`;
  try {
    await loadAssets();
  } catch (error) {
    toast(error.message);
  }
  renderAssetPanel();
  renderStoryboardPanel();
}

async function showUsage() {
  const query = activeId ? `?projectId=${encodeURIComponent(activeId)}` : "";
  let report;
  try {
    report = await api(`/api/usage${query}`);
  } catch (error) {
    toast(error.message);
    return;
  }
  usageReport = report;
  openInspector("usage");
  renderUsagePanel();
}

// M6 E4 episode film: the rendered whole-episode MP4 and its sidecar SRT. The
// overlay is deliberately client-side so the subtitle switch works even where the
// browser does not render the embedded mov_text track.
let episodeFilmKey = null;
let episodeFilmCues = [];
let episodeSubtitleOn = true;
let episodeSubtitleBusy = false;
let episodeSubtitleError = null;

async function loadEpisodeFilm(view) {
  const render = episodeRender(view);
  const key = render
    ? `${render.renderId || render.filmId}:${render.filmVersion}:${render.sidecarUrl || ""}`
    : null;
  if (!key || key === episodeFilmKey) return;
  episodeFilmKey = key;
  episodeFilmCues = [];
  episodeSubtitleError = null;
  if (!render.sidecarUrl) {
    if (panelMode === "episode") renderEpisodePanel();
    return;
  }
  episodeSubtitleBusy = true;
  try {
    const text = await apiText(render.sidecarUrl);
    if (episodeFilmKey !== key) return;
    episodeFilmCues = parseSubtitleSrt(text);
  } catch (error) {
    if (episodeFilmKey !== key) return;
    episodeFilmCues = [];
    episodeSubtitleError = { code: "SUBTITLE_SIDECAR", message: error.message };
  } finally {
    if (episodeFilmKey === key) {
      episodeSubtitleBusy = false;
      if (panelMode === "episode") renderEpisodePanel();
    }
  }
}

function applyEpisodeSubtitles(video) {
  const overlay = $("#episode-subtitle-overlay");
  const button = $('[data-action="toggle-episode-subtitles"]');
  if (button) {
    button.textContent = episodeSubtitleToggleLabel(episodeSubtitleOn);
    button.setAttribute("aria-pressed", String(episodeSubtitleOn));
  }
  if (video) {
    for (const track of video.textTracks || []) {
      // The overlay owns subtitle visibility; hide the native track so a browser
      // that supports mov_text cannot draw a second copy.
      track.mode = episodeSubtitleOn ? "hidden" : "disabled";
    }
  }
  if (!overlay) return;
  const cue = episodeSubtitleOn && video ? subtitleCueAt(episodeFilmCues, video.currentTime) : null;
  overlay.textContent = cue?.text || "";
  overlay.hidden = !cue;
}

function bindEpisodeFilm(panel) {
  const video = panel.querySelector("#episode-film-video");
  if (!video) return;
  for (const event of ["loadedmetadata", "timeupdate", "seeked", "play"]) {
    video.addEventListener(event, () => applyEpisodeSubtitles(video));
  }
  applyEpisodeSubtitles(video);
}

function restoreEpisodePlayback(panel, previous) {
  if (!previous) return;
  const video = panel.querySelector("#episode-film-video");
  if (!video || video.getAttribute("src") !== previous.src) return;
  const restore = () => {
    if (previous.time > 0) video.currentTime = previous.time;
    if (!previous.paused) video.play().catch(() => {});
    applyEpisodeSubtitles(video);
  };
  if (video.readyState >= 1) restore();
  else video.addEventListener("loadedmetadata", restore, { once: true });
}

function episodeFilmSection(view) {
  const raw = view?.render;
  const render = episodeRender(view);
  if (!render) {
    return `<section class="episode-film is-empty" aria-label="整集成片">
      <div class="film-head"><strong>整集成片</strong><span class="meta">尚未渲染</span></div>
      <p class="meta">全部场记与镜头交付后，可以通过 CLI 或 Agent 的 <code>film_render</code> 渲染整集；这里只读展示与播放。</p>
    </section>`;
  }
  const rows = renderSceneRows(raw)
    .map(
      (row) =>
        `<li><span>${escapeHTML(row.label)}</span><span class="job-state ${escapeHTML(row.tone)}">${escapeHTML(row.status)}</span><span class="meta">${escapeHTML(row.detail)}</span></li>`,
    )
    .join("");
  const summary = escapeHTML(episodeRenderSummary(render));
  if (!render.mediaUrl) {
    return `<section class="episode-film is-empty" aria-label="整集成片">
      <div class="film-head"><strong>整集成片</strong><span class="meta">${summary}</span></div>
      ${rows ? `<ul class="film-versions">${rows}</ul>` : ""}
      ${render.errorCode ? `<p class="meta film-error">${escapeHTML(render.errorCode)}</p>` : ""}
    </section>`;
  }
  return `<section class="episode-film" aria-label="整集成片">
    <div class="film-head"><strong>整集成片</strong><span class="meta">${summary}</span></div>
    <div class="episode-player">
      <video id="episode-film-video" class="episode-film-video" src="${escapeHTML(render.mediaUrl)}" controls preload="metadata" playsinline aria-label="整集成片播放"></video>
      <div id="episode-subtitle-overlay" class="episode-subtitles" aria-live="off" hidden></div>
    </div>
    <div class="film-actions">
      <button class="secondary" data-action="toggle-episode-subtitles" aria-pressed="${episodeSubtitleOn}" ${render.sidecarUrl ? "" : "disabled"}>${escapeHTML(episodeSubtitleToggleLabel(episodeSubtitleOn))}</button>
      ${render.sidecarUrl ? `<a class="secondary" href="${escapeHTML(render.sidecarUrl)}" download="episode-${escapeHTML(render.filmId || "film")}.srt">下载旁挂 SRT</a>` : ""}
      ${episodeSubtitleBusy ? '<span class="meta">正在读取字幕…</span>' : ""}
    </div>
    ${rows ? `<ul class="film-versions">${rows}</ul>` : ""}
    ${episodeSubtitleError ? `<p class="meta film-error">${escapeHTML(episodeSubtitleError.message)}</p>` : ""}
  </section>`;
}

function renderEpisodePanel() {
  const panel = $("#episode-panel");
  if (panelMode !== "episode") {
    panel.hidden = true;
    return;
  }
  panel.hidden = false;
  const view = episode();
  if (!view || !view.episode) {
    panel.innerHTML = `${panelHeading("分集", "close-episode")}<p class="empty">当前项目还没有分集。让 Agent 用 episode_plan 与 episode_save 保存分集后，这里会显示场记、预算、生成进度与逐场成片。</p>`;
    icons(panel);
    return;
  }
  const meta = episodeMeta(view);
  const budget = episodeBudget(view);
  const progress = episodeProgress(view);
  const rows = sceneGeneration(view);
  const scenes = view.scenes || [];
  const characters = view.consistency?.characters || [];
  const existingVideo = panel.querySelector("#episode-film-video");
  const previousPlayback = existingVideo
    ? {
        src: existingVideo.getAttribute("src"),
        time: existingVideo.currentTime,
        paused: existingVideo.paused,
      }
    : null;
  void loadEpisodeFilm(view);
  const scenesHTML = scenes
    .map((scene, index) => {
      const row = rows.get(scene.sceneId);
      const film = scene.film;
      const latest = film?.latest;
      const playable = Boolean(latest?.mediaAvailable && latest?.mediaUrl);
      return `<article class="episode-scene" data-scene-id="${escapeHTML(scene.sceneId)}">
        <div class="episode-scene-head">
          <strong>${index + 1}. ${escapeHTML(scene.heading)}</strong>
          <span>${scene.shotCount} 镜 · ${scene.durationSeconds} 秒</span>
        </div>
        <p class="meta">${escapeHTML(scene.synopsis || "")}</p>
        <p class="meta">${escapeHTML(sceneSummary(scene, row))}</p>
        <p class="meta">分镜：${scene.storyboardId ? "已绑定" : "未绑定"} · 成片：${escapeHTML(filmStatusLabel(film))}</p>
        <div class="episode-actions">
          ${scene.storyboardId ? `<button class="secondary" data-action="open-scene-storyboard" data-storyboard-id="${escapeHTML(scene.storyboardId)}">查看分镜与镜头</button>` : ""}
          ${playable ? `<a class="primary" href="${escapeHTML(latest.mediaUrl)}" target="_blank" rel="noopener">播放成片 v${film.currentVersion}</a>` : ""}
          ${playable ? `<a class="secondary" href="${escapeHTML(latest.mediaUrl)}?download=1" download="${escapeHTML(latest.outputMediaId)}.mp4">下载</a>` : ""}
        </div>
      </article>`;
    })
    .join("");
  const charactersHTML = characters.length
    ? characters
        .map((character) => {
          const reviews = character.reviews || [];
          const score = reviews.length ? ` · 人工评分 ${reviews.at(-1).score}/5` : "";
          return `<li><strong>${escapeHTML(character.name || character.characterId)}</strong>：${character.shotCount} 个镜头 · ${
            character.consistent ? "同一素材" : "素材不一致"
          }${score}</li>`;
        })
        .join("")
    : '<li class="meta">这个分集还没有引用角色圣经。</li>';
  panel.innerHTML = `${panelHeading("分集", "close-episode")}
    <p class="meta">${escapeHTML(meta.title)} · 当前版本 ${meta.currentVersion} · revision ${meta.revision} · ${meta.sceneCount} 场 / ${meta.shotCount} 镜 / ${meta.targetDurationSeconds} 秒</p>
    <div class="episode-budget">
      <span>镜头交付 <strong>${progress.delivered}/${progress.planned}</strong></span>
      <span>集级任务 <strong>${budget.usedJobs}/${budget.jobCap}</strong></span>
      <span>集级估算 <strong>${escapeHTML(budget.usedAmount)} / ${escapeHTML(budget.episodeCapAmount)} ${escapeHTML(budget.currency)}</strong></span>
      <span>剩余 <strong>${escapeHTML(budget.remainingAmount)} ${escapeHTML(budget.currency)}</strong></span>
    </div>
    ${
      budget.foreignCurrencyJobs.length
        ? `<p class="meta">有 ${budget.foreignCurrencyJobs.length} 个任务使用其他币种报价，不进入集级 CNY 额度。</p>`
        : ""
    }
    ${episodeFilmSection(view)}
    <div class="episode-scenes">${scenesHTML || '<p class="empty">分集还没有冻结的场记。</p>'}</div>
    <h3>角色一致性</h3>
    <ul class="episode-consistency">${charactersHTML}</ul>
    <p class="meta">分集、场记与角色圣经由 Agent 的 episode_* / bible_* 工具保存；整集登记与逐镜头重做通过对话显式确认或 CLI 执行，这里只读展示。</p>`;
  icons(panel);
  bindEpisodeFilm(panel);
  restoreEpisodePlayback(panel, previousPlayback);
}

// M7 E1: the season panel edits only membership and title. Saving never touches
// episodes, scenes, shots, budgets, Jobs or films, and every save appends a version.
function seasonProjectEpisodes() {
  return snapshot?.episode?.episodes || [];
}

function resetSeasonEdit(view) {
  const meta = seasonMeta(view);
  seasonEditing = meta ? { seasonId: meta.seasonId, revision: meta.revision } : null;
  seasonSelection = meta ? [...meta.episodeIds] : [];
  seasonDraftTitle = meta ? meta.title : "";
  seasonError = null;
}

function openSeasonPanel() {
  panelMode = "season";
  $("#artifact-panel").hidden = true;
  $("#observe-panel").hidden = true;
  $("#storyboard-panel").hidden = true;
  $("#episode-panel").hidden = true;
  $("#season-panel").hidden = false;
  seasonView = snapshot?.season || null;
  seasonList = seasonView?.seasons || [];
  resetSeasonEdit(seasonView);
  renderSeasonPanel();
}

async function loadSeason(seasonId) {
  if (!activeId || !seasonId) return;
  seasonBusy = true;
  seasonError = null;
  renderSeasonPanel();
  try {
    seasonView = await api(
      `/api/seasons/${encodeURIComponent(seasonId)}?projectId=${encodeURIComponent(activeId)}`,
    );
    seasonList = seasonView.seasons || [];
    resetSeasonEdit(seasonView);
  } catch (error) {
    seasonError = { code: error.code, message: error.message };
  } finally {
    seasonBusy = false;
    if (panelMode === "season") renderSeasonPanel();
  }
}

function toggleSeasonEpisode(episodeId) {
  const index = seasonSelection.indexOf(episodeId);
  if (index >= 0) {
    seasonSelection.splice(index, 1);
    seasonError = null;
  } else if (seasonSelection.length >= 4) {
    seasonError = { code: "SEASON_LIMIT", message: "一季最多 4 集；先取消一集再加入。" };
  } else {
    seasonSelection.push(episodeId);
    seasonError = null;
  }
  renderSeasonPanel();
}

function startNewSeason() {
  seasonEditing = null;
  seasonSelection = [];
  seasonDraftTitle = "";
  seasonError = null;
  renderSeasonPanel();
}

async function saveSeason() {
  if (!activeId || seasonBusy) return;
  const title = $("#season-title")?.value ?? seasonDraftTitle;
  const check = seasonValidation(title, seasonSelection);
  if (!check.ok) {
    seasonError = { code: "SEASON_INVALID", message: check.error };
    renderSeasonPanel();
    return;
  }
  seasonBusy = true;
  seasonError = null;
  renderSeasonPanel();
  try {
    const body = seasonSaveBody({
      clientRequestId: crypto.randomUUID(),
      title: check.title,
      episodeIds: check.episodeIds,
      seasonId: seasonEditing?.seasonId,
      expectedRevision: seasonEditing?.revision,
    });
    const result = await post(
      `/api/sessions/${encodeURIComponent(activeId)}/seasons/save`,
      body,
    );
    await refreshSession(activeId);
    seasonView = await api(
      `/api/seasons/${encodeURIComponent(result.seasonId)}?projectId=${encodeURIComponent(activeId)}`,
    );
    seasonList = seasonView.seasons || [];
    resetSeasonEdit(seasonView);
    toast(result.replayed ? "季内容未变化，已复用当前版本" : `季已保存为 v${result.version}`);
  } catch (error) {
    seasonError = { code: error.code, message: error.message };
  } finally {
    seasonBusy = false;
    if (panelMode === "season") renderSeasonPanel();
  }
}

function renderSeasonPanel() {
  const panel = $("#season-panel");
  if (panelMode !== "season") {
    panel.hidden = true;
    return;
  }
  panel.hidden = false;
  const meta = seasonMeta(seasonView);
  const members = seasonMembers(seasonView);
  const episodes = seasonProjectEpisodes();
  const currentSeasonId = seasonEditing?.seasonId || null;
  const { rows, missing } = seasonEpisodeRows(
    episodes,
    seasonList,
    currentSeasonId,
    seasonSelection,
  );
  const picker = seasonPickerOptions(seasonView);
  const pickerHTML = `<select id="season-picker" aria-label="选择季">
    <option value="">新建季</option>
    ${picker
      .map(
        (option) =>
          `<option value="${escapeHTML(option.value)}" ${option.value === currentSeasonId ? "selected" : ""}>${escapeHTML(option.label)}</option>`,
      )
      .join("")}
  </select>`;
  if (!episodes.length) {
    panel.innerHTML = `${panelHeading("季", "close-season")}
      <p class="empty">当前项目还没有分集。先用 episode_plan 与 episode_save 保存 1–4 集，再把它们按顺序组成一季。</p>`;
    icons(panel);
    return;
  }
  const memberText = members.length
    ? members
        .map((member) => `${member.order}. ${escapeHTML(member.title)}`)
        .join(" → ")
    : "尚未选择成员";
  const rowsHTML = rows
    .map(
      (row) => `<label class="season-episode ${row.disabled ? "is-owned" : ""}">
        <input type="checkbox" data-season-episode="${escapeHTML(row.episodeId)}"
          ${row.selected ? "checked" : ""} ${row.disabled ? "disabled" : ""} />
        <span class="season-order">${row.order || "·"}</span>
        <span class="season-episode-body">
          <strong>${escapeHTML(row.title)}</strong>
          <small>${row.shotCount} 镜 · ${row.sceneCount} 场 · ${row.targetDurationSeconds} 秒</small>
          ${row.ownerTitle ? `<small class="season-owner">属于另一季「${escapeHTML(row.ownerTitle)}」：先在原季移除</small>` : ""}
        </span>
      </label>`,
    )
    .join("");
  panel.innerHTML = `${panelHeading("季", "close-season")}
    <div class="season-toolbar">
      ${pickerHTML}
      <button class="secondary" data-action="season-new" ${seasonBusy ? "disabled" : ""}>新建季</button>
      <button class="secondary" data-action="refresh-season" ${seasonBusy ? "disabled" : ""}>刷新</button>
    </div>
    <p class="meta">${escapeHTML(seasonMetaText(meta))}</p>
    <label class="season-title-field">季标题
      <input id="season-title" maxlength="120" value="${escapeHTML(seasonDraftTitle)}" ${seasonBusy ? "disabled" : ""} />
    </label>
    <p class="meta">成员顺序（点击顺序即季内顺序，最多 4 集）：${memberText}</p>
    ${missing.length ? `<p class="meta season-warning">有 ${missing.length} 个已保存成员在当前项目中找不到，请重新选择。</p>` : ""}
    <div class="season-episodes">${rowsHTML}</div>
    ${seasonError ? `<p class="meta season-error">${escapeHTML(seasonError.code)} · ${escapeHTML(seasonError.message)}</p>` : ""}
    <div class="season-actions">
      <button class="primary" data-action="save-season" ${seasonBusy ? "disabled" : ""}>${seasonBusy ? "正在保存…" : "保存季"}</button>
      ${seasonEditing ? `<button class="secondary" data-action="close-season">取消</button>` : ""}
    </div>
    <p class="meta">季只保存标题与有序成员关系；不改分集、场记、镜头、预算或成片，也不登记任务、不调用模型。保存同一内容会复用当前版本，修改会追加新版本。</p>`;
  icons(panel);
}

function renderStoryboardPanel() {
  const panel = $("#storyboard-panel");
  if (panelMode !== "storyboard") {
    panel.hidden = true;
    return;
  }
  panel.hidden = false;
  const board = storyboard();
  if (!board) {
    panel.innerHTML = `${panelHeading("分镜与镜头", "close-storyboard")}<p class="empty">当前项目还没有结构化分镜。Agent 保存分镜后，这里会按顺序显示镜头、版本与本地媒体。</p>`;
    icons(panel);
    return;
  }
  if (!assetsLoaded) {
    loadAssets()
      .then(() => renderStoryboardPanel())
      .catch((error) => toast(error.message));
  }
  const filmKey = `${board.storyboardId}:${board.revision}`;
  if (filmKey !== filmLoadedKey) {
    filmLoadedKey = filmKey;
    loadFilm(board).catch((error) => toast(error.message));
  }
  if (filmKey !== subtitleLoadedKey) {
    subtitleLoadedKey = filmKey;
    loadSubtitle(board).catch((error) => toast(error.message));
  }
  const grouped = groupJobsByShot(snapshot?.jobs || [], board.shots);
  const playlist = playList(board.shots, snapshot?.jobs || []);
  const playing = sequence.index >= 0 ? sequence.items?.[sequence.index] : null;
  const chosen = selectedShots().length;
  panel.innerHTML = `${panelHeading("分镜与镜头", "close-storyboard")}
    <p class="meta">${escapeHTML(board.title)} · 当前版本 ${board.currentVersion} / 共 ${board.versionCount} 版 · revision ${board.revision} · ${board.shots.length} 个镜头</p>
    <div class="storyboard-toolbar">
      <button class="primary" data-action="batch-selected" ${chosen && !shotBusy.size ? "" : "disabled"}>生成选中镜头 · ${chosen}</button>
      <button class="secondary" data-action="toggle-all-shots">${chosen === board.shots.length ? "清空选择" : "全选镜头"}</button>
      <button class="secondary" data-action="refresh-storyboard">刷新</button>
      ${playlist.items.length ? `<button class="secondary" data-action="${playing ? "stop-sequence" : "play-sequence"}">${playing ? "停止连播" : `连播 ${playlist.items.length} 个镜头`}</button>` : ""}
    </div>
    ${playing ? sequencePlayer(playing, playlist) : ""}
    ${playlist.missing.length ? `<p class="meta">${playlist.missing.length} 个镜头还没有本地媒体，连播会跳过：${playlist.missing.map((item) => escapeHTML(item.label)).join("、")}</p>` : ""}
    ${filmSection(board)}
    ${subtitleSection(board)}
    <div class="shot-list">${board.shots.map((shot) => shotCard(shot, grouped.byShot[shot.id] || [])).join("")}</div>
    ${grouped.ungrouped.length ? `<h3>其他视频任务</h3>${grouped.ungrouped.map((job) => jobCard(job, false)).join("")}` : ""}`;
  icons(panel);
  const video = panel.querySelector("#sequence-video");
  if (video) {
    video.addEventListener("ended", () => advanceSequence());
    video.addEventListener("error", () => {
      toast("这个镜头暂时无法播放，已跳到下一个。");
      advanceSequence();
    });
  }
  bindPanelMedia(panel);
  bindShotThumbnails(panel);
}

// Film state is fetched per storyboard revision; assembling never changes the
// revision, so a successful merge only replaces the film view.
let filmState = null;
let filmLoadedKey = null;
let filmBusy = false;
let filmError = null;
let renderState = null;
let renderBusy = false;
let renderError = null;

async function loadFilm(board) {
  const path = filmRequestPath(board, activeId);
  if (!path) return;
  try {
    const result = await api(path);
    filmState = result.film || null;
    filmError = null;
  } catch (error) {
    filmError = { code: error.code, message: error.message };
  }
  await loadRender(board);
  if (panelMode === "storyboard") renderStoryboardPanel();
}

async function loadRender(board) {
  const path = renderRequestPath(activeId, board);
  if (!path) return;
  try {
    const result = await api(path);
    renderState = result.render || null;
    renderError = null;
  } catch (error) {
    renderError = { code: error.code, message: error.message };
  }
}

function filmSection(board) {
  const film = filmState;
  const playback = filmPlayback(film);
  const versions = filmVersions(film);
  const canAssemble = Boolean(assembleBody(board, "probe"));
  const rows = versions
    .map((version) => {
      const download = filmDownloadUrl(version);
      return `<li><span>${escapeHTML(version.label)}${version.current ? "（当前）" : "（历史）"} · ${version.shots} 个镜头 · ${escapeHTML(filmTimelineLabel(version))}</span>
        <span class="job-state ${version.mediaAvailable ? "succeeded" : "attention"}">${version.mediaAvailable ? "可播放" : "文件不可用"}</span>
        ${download ? `<a class="secondary" href="${escapeHTML(download)}" download="film-${escapeHTML(film.filmId)}-${version.index}.mp4">下载</a>` : ""}</li>`;
    })
    .join("");
  const player = playback.url
    ? `<video id="film-video" class="film-video" src="${escapeHTML(playback.url)}" controls preload="metadata" playsinline aria-label="成片播放"></video>`
    : `<p class="meta">${hasFilm(film) ? "成片文件当前不可用；可以重新拼接生成新版本。" : "还没有成片。所有镜头交付后，可以在这里本地拼接为单一 MP4。"}</p>`;
  const canRender = Boolean(renderRequestBody(board, subtitleState, "probe"));
  const renderRows = renderSceneRows(renderState)
    .map(
      (row) =>
        `<li><span>${escapeHTML(row.label)}</span><span class="job-state ${escapeHTML(row.tone)}">${escapeHTML(row.status)}</span><span class="meta">${escapeHTML(row.detail)}</span></li>`,
    )
    .join("");
  const sidecar = renderSidecarUrl(renderState);
  return `<section class="film-section" aria-label="成片">
    <div class="film-head"><strong>成片</strong><span class="meta">${escapeHTML(filmSummary(film))}</span></div>
    ${player}
    ${rows ? `<ul class="film-versions">${rows}</ul>` : ""}
    <div class="film-actions">
      <button class="primary" data-action="assemble-film" ${filmBusy || !canAssemble ? "disabled" : ""}>${filmBusy ? "正在本地拼接…" : hasFilm(film) ? "重新拼接成片" : "拼接成片"}</button>
      <button class="secondary" data-action="render-film" ${renderBusy || !canRender ? "disabled" : ""}>${renderBusy ? "正在整片渲染…" : "整片渲染（转场/字幕/音频）"}</button>
      <button class="secondary" data-action="refresh-film" ${filmBusy || renderBusy ? "disabled" : ""}>刷新成片</button>
    </div>
    <div class="render-status">
      <p class="meta">${escapeHTML(renderSummary(renderState))}</p>
      ${renderRows ? `<ul class="film-versions">${renderRows}</ul>` : ""}
      ${sidecar ? `<a class="secondary" href="${escapeHTML(sidecar)}" download="film-${escapeHTML(renderState?.filmId || "render")}.srt">下载旁挂 SRT</a>` : ""}
    </div>
    ${filmError ? `<p class="meta film-error">${escapeHTML(filmErrorLabel(filmError))}</p>` : ""}
    ${renderError ? `<p class="meta film-error">${escapeHTML(renderErrorLabel(renderError))}</p>` : ""}
  </section>`;
}

async function renderFilm() {
  const board = storyboard();
  const body = renderRequestBody(board, subtitleState, crypto.randomUUID());
  if (!body) {
    toast("需要先保存分镜与字幕轨。");
    return;
  }
  if (renderBusy) return;
  renderBusy = true;
  renderError = null;
  renderStoryboardPanel();
  try {
    const result = await post(`/api/sessions/${encodeURIComponent(activeId)}/films/render`, body);
    filmState = result.film || filmState;
    renderBusy = false;
    await loadRender(board);
    toast(`整片渲染完成：v${result.filmVersion}。`);
  } catch (error) {
    renderError = { code: error.code, message: error.message };
    toast(renderErrorLabel(renderError));
  } finally {
    renderBusy = false;
    if (panelMode === "storyboard") renderStoryboardPanel();
  }
}

async function assembleFilm() {
  const board = storyboard();
  const body = assembleBody(board, crypto.randomUUID());
  if (!body) {
    toast("还没有可拼接的分镜。");
    return;
  }
  if (filmBusy) return;
  filmBusy = true;
  filmError = null;
  renderStoryboardPanel();
  try {
    filmState = await post(
      `/api/sessions/${encodeURIComponent(activeId)}/films/assemble`,
      body,
    );
    filmLoadedKey = `${board.storyboardId}:${board.revision}`;
    toast("已在本地拼接成片。");
  } catch (error) {
    filmError = { code: error.code, message: error.message };
    toast(filmErrorLabel(filmError));
  } finally {
    filmBusy = false;
    if (panelMode === "storyboard") renderStoryboardPanel();
  }
}

// Subtitle state belongs to the same storyboard snapshot as the film: saving or
// exporting never changes the storyboard revision, so the key stays stable.
let subtitleState = null;
let subtitleLoadedKey = null;
let subtitleBusy = false;
let subtitleError = null;

async function loadSubtitle(board) {
  if (!board?.storyboardId || !activeId) return;
  const query = `?projectId=${encodeURIComponent(activeId)}&storyboardId=${encodeURIComponent(board.storyboardId)}`;
  try {
    const result = await api(`/api/subtitles${query}`);
    subtitleState = result.track || null;
    subtitleError = null;
  } catch (error) {
    subtitleError = { code: error.code, message: error.message };
  }
  if (panelMode === "storyboard") renderStoryboardPanel();
}

function latestRenderLabel(film) {
  const latest = film?.latest;
  if (!latest?.render) return "";
  const mode = renderModeLabel(latest.render.subtitle?.mode);
  return mode ? `${mode}（成片 v${latest.currentVersion || latest.version}）` : "";
}

function subtitleSection(board) {
  const track = subtitleState;
  const versions = subtitleVersions(track);
  const latest = latestSubtitle(track);
  const canSave = Boolean(subtitleSaveBody(board, "probe"));
  const canExport = Boolean(latest && hasFilm(filmState));
  const rows = versions
    .slice(0, 4)
    .map((version) => {
      const srt = subtitleDownloadUrl(track, version, "srt");
      const ass = subtitleDownloadUrl(track, version, "ass");
      const source = version.timelineSource === "film" ? "成片时间线" : "分镜时间线";
      return `<li><span>${escapeHTML(version.label)}${version.current ? "（当前）" : "（历史）"} · ${escapeHTML(version.cueLabel)} · ${source}</span>
        ${srt ? `<a class="secondary" href="${escapeHTML(srt)}" download="subtitles-${escapeHTML(track.trackId)}-${version.index}.srt">SRT</a>` : ""}
        ${ass ? `<a class="secondary" href="${escapeHTML(ass)}" download="subtitles-${escapeHTML(track.trackId)}-${version.index}.ass">ASS</a>` : ""}</li>`;
    })
    .join("");
  const rendered = latestRenderLabel(filmState);
  return `<section class="subtitle-section" aria-label="字幕">
    <div class="film-head"><strong>字幕</strong><span class="meta">${escapeHTML(subtitleSummary(track))}${rendered ? ` · 已导出 ${escapeHTML(rendered)}` : ""}</span></div>
    ${rows ? `<ul class="film-versions">${rows}</ul>` : '<p class="meta">生成会用该场冻结的台词与成片/分镜时间线确定性对齐；人工修正按毫秒提交，不自动改写时间。</p>'}
    <div class="film-actions">
      <button class="primary" data-action="save-subtitle" ${subtitleBusy || !canSave ? "disabled" : ""}>${subtitleBusy ? "正在处理…" : track ? "按当前台词重新生成" : "生成字幕"}</button>
      <button class="secondary" data-action="export-subtitle" data-mode="soft" ${subtitleBusy || !canExport ? "disabled" : ""}>导出软字幕</button>
      <button class="secondary" data-action="export-subtitle" data-mode="burn" ${subtitleBusy || !canExport ? "disabled" : ""}>导出烧录字幕</button>
      <button class="secondary" data-action="refresh-subtitle" ${subtitleBusy ? "disabled" : ""}>刷新字幕</button>
    </div>
    ${subtitleError ? `<p class="meta film-error">${escapeHTML(subtitleErrorLabel(subtitleError))}</p>` : ""}
  </section>`;
}

async function saveSubtitle() {
  const board = storyboard();
  const body = subtitleSaveBody(board, crypto.randomUUID());
  if (!body) {
    toast("还没有可保存字幕的分镜。");
    return;
  }
  if (subtitleBusy) return;
  subtitleBusy = true;
  subtitleError = null;
  renderStoryboardPanel();
  try {
    subtitleState = await post(
      `/api/sessions/${encodeURIComponent(activeId)}/subtitles/save`,
      body,
    );
    toast("字幕已保存。");
  } catch (error) {
    subtitleError = { code: error.code, message: error.message };
    toast(subtitleErrorLabel(subtitleError));
  } finally {
    subtitleBusy = false;
    if (panelMode === "storyboard") renderStoryboardPanel();
  }
}

async function exportSubtitle(mode) {
  const board = storyboard();
  const body = subtitleExportBody(subtitleState, mode, crypto.randomUUID());
  if (!body) {
    toast("需要先保存字幕，并有一次成片。");
    return;
  }
  if (subtitleBusy) return;
  subtitleBusy = true;
  subtitleError = null;
  renderStoryboardPanel();
  try {
    filmState = await post(
      `/api/sessions/${encodeURIComponent(activeId)}/subtitles/export`,
      body,
    );
    filmLoadedKey = `${board.storyboardId}:${board.revision}`;
    toast(`已导出${renderModeLabel(mode)}成片。`);
  } catch (error) {
    subtitleError = { code: error.code, message: error.message };
    toast(subtitleErrorLabel(subtitleError));
  } finally {
    subtitleBusy = false;
    if (panelMode === "storyboard") renderStoryboardPanel();
  }
}

function shotCard(shot, jobs) {
  const state = shotState(shot, jobs);
  const thumb = shotThumbnail(shot, assets, jobs);
  const versions = shotVersions(shot);
  const selected = shotSelection.has(shot.id);
  const busy = shotBusy.has(shot.id);
  const thumbHTML =
    thumb.kind === "image"
      ? `<img class="shot-thumb" src="${escapeHTML(thumb.url)}" alt="" data-shot-thumb="${escapeHTML(shot.id)}">`
      : `<span class="shot-thumb placeholder" data-icon="film"></span>`;
  const reference = (shot.referenceAssetIds || []).length
    ? `<small>参考图 ${shot.referenceAssetIds.map((id) => escapeHTML(String(id).slice(0, 8))).join("、")}</small>`
    : "";
  const history = versions.length
    ? `<ul class="shot-versions">${versions
        .map((version) => {
          const versionState = shotState({ latest: version }, jobs);
          const mediaUrl = version.mediaUrl || jobMediaUrl(jobs, version.jobId);
          return `<li><span>${escapeHTML(version.label)} · 分镜 v${version.fromVersion}${version.current ? "" : "（历史）"}</span>
            <span class="job-state ${escapeHTML(versionState.tone)}">${escapeHTML(versionState.label)}</span>
            ${
              mediaUrl
                ? `<button data-action="play-shot" data-shot-id="${escapeHTML(shot.id)}" data-media-url="${escapeHTML(mediaUrl)}">播放</button><a class="secondary" href="${escapeHTML(mediaUrl)}?download=1" download="${escapeHTML(version.jobId)}.mp4">下载</a>`
                : ""
            }</li>`;
        })
        .join("")}</ul>`
    : '<p class="meta">还没有登记过任务。</p>';
  return `<article class="shot-card ${selected ? "selected" : ""}" data-shot-id="${escapeHTML(shot.id)}">
    <div class="shot-head"><strong>镜头 ${shot.order} · ${escapeHTML(shot.framing)}</strong><span class="job-state ${escapeHTML(state.tone)}">${escapeHTML(state.label)}</span></div>
    <div class="shot-body">${thumbHTML}<div class="shot-facts">
      <span class="description">${escapeHTML(shot.description)}</span>
      <small>${escapeHTML(shot.mode)} · ${shot.durationSeconds} 秒${shot.cameraMovement ? ` · ${escapeHTML(shot.cameraMovement)}` : ""}</small>
      ${reference}
      <small>shotId ${escapeHTML(shot.id.slice(0, 8))}</small>
    </div></div>
    ${history}
    <div class="shot-actions">
      <label><input type="checkbox" data-select-shot="${escapeHTML(shot.id)}" ${selected ? "checked" : ""}> 选中批量生成</label>
      <button class="secondary" data-action="redo-shot" data-shot-id="${escapeHTML(shot.id)}" ${busy ? "disabled" : ""}>${busy ? "正在登记…" : "重做这个镜头"}</button>
    </div>
    ${jobs.length ? `<div class="shot-jobs">${jobs.map((job) => jobCard(job, false)).join("")}</div>` : ""}
  </article>`;
}

function sequencePlayer(item, playlist) {
  return `<div class="sequence-player">
    <video id="sequence-video" src="${escapeHTML(item.url)}" controls autoplay muted playsinline aria-label="镜头连播"></video>
    <div class="sequence-controls"><span>连播 ${sequence.index + 1}/${playlist.items?.length || 1} · ${escapeHTML(item.label || "镜头")}</span>
      <span><button class="secondary" data-action="sequence-next">下一个</button><button class="secondary" data-action="stop-sequence">停止</button></span></div>
  </div>`;
}

function startSequence(shotId = null, url = null) {
  const playlist = playList(storyboard()?.shots || [], snapshot?.jobs || []);
  if (url && url.startsWith("/api/media/")) {
    const label = playlist.items.find((item) => item.shotId === shotId)?.label || "镜头版本";
    sequence = { items: [{ shotId, order: 0, url, label }], index: 0 };
  } else if (playlist.items.length) {
    const first = shotId
      ? playlist.items.find((item) => item.shotId === shotId) || playlist.items[0]
      : playlist.items[0];
    sequence = { items: playlist.items, index: playlist.items.indexOf(first) };
  } else {
    toast("还没有可播放的本地媒体。");
    return;
  }
  panelMode = "storyboard";
  $("#artifact-panel").hidden = true;
  $("#storyboard-panel").hidden = false;
  renderStoryboardPanel();
}

function advanceSequence() {
  const list = sequence.items || [];
  const index = sequence.index + 1;
  if (index >= list.length) {
    sequence = { items: null, index: -1 };
    toast("连播结束。");
    renderStoryboardPanel();
    return;
  }
  sequence = { items: list, index };
  renderStoryboardPanel();
}

function stopSequence() {
  sequence = { items: null, index: -1 };
  renderStoryboardPanel();
}

async function confirmBatch() {
  const board = storyboard();
  const chosen = selectedShots();
  if (!board || !chosen.length || shotBusy.size) return;
  const token = { ...revisionToken(board, chosen.map((shot) => shot.id)), projectId: activeId };
  let preview;
  try {
    preview = await api(estimateQuery(token));
  } catch (error) {
    toast(error.message);
    return;
  }
  const summary = batchSummary(preview);
  const generation = openDialog(
    "确认生成镜头",
    `<p>${
      preview.simulated
        ? "当前为模拟视频模式：只登记本地任务，不产生费用，也没有真实媒体。"
        : `当前为真实视频模式：将登记 ${summary.registered} 个新任务${summary.reused ? `，复用 ${summary.reused} 个已有任务` : ""}。总估算 ${escapeHTML(summary.total ?? "—")} ${escapeHTML(summary.currency)}（含复用镜头的历史估算）。`
    }</p>
    <table class="estimate-table"><thead><tr><th>镜头</th><th>模式</th><th>时长</th><th>估算</th></tr></thead><tbody>
      ${summary.rows
        .map(
          (row) =>
            `<tr><td>${escapeHTML(row.label)}</td><td>${escapeHTML(row.mode)}</td><td>${row.durationSeconds} 秒</td><td>${row.reused ? "复用原任务" : escapeHTML(row.amount ?? "—")}</td></tr>`,
        )
        .join("")}
    </tbody></table>
    ${
      summary.limits
        ? `<p class="meta">登记后本次 Run：新任务 ${summary.limits.usedNewJobs} / ${summary.limits.maxNewJobsPerRun}，已用估算 ${escapeHTML(summary.limits.usedAmount)} ${escapeHTML(summary.currency)}，剩余 ${escapeHTML(summary.limits.remainingAmount)}。服务端会再次校验上限，超出则整批不登记。</p>`
        : ""
    }
    <p class="meta">确认后为该批镜头各登记一个任务；同一镜头同一请求复用原任务，不会重复登记。</p>
    <div class="actions"><button class="secondary" data-action="close-dialog">取消</button><button class="primary" data-action="confirm-batch">确认生成 ${summary.registered} 个新任务</button></div>`,
  );
  pendingBatch = { token, generation };
}

async function submitBatch() {
  const pending = pendingBatch;
  if (!pending || pending.generation !== dialogGeneration || shotBusy.has("batch")) return;
  const token = pending.token;
  shotBusy.add("batch");
  pendingBatch = null;
  try {
    const result = await post(`/api/sessions/${encodeURIComponent(activeId)}/shots/generate`, {
      clientRequestId: crypto.randomUUID(),
      storyboardId: token.storyboardId,
      storyboardVersion: token.storyboardVersion,
      shotIds: token.shotIds,
      confirm: true,
    });
    $("#dialog").close();
    shotSelection = new Set();
    toast(`已登记 ${result.shots.length} 个镜头任务。`);
    await refreshSession(activeId);
  } catch (error) {
    toast(error.message);
  } finally {
    shotBusy.delete("batch");
    renderStoryboardPanel();
  }
}

async function redoShot(shotId) {
  const board = storyboard();
  if (!board || !shotId || shotBusy.has(shotId)) return;
  shotBusy.add(shotId);
  renderStoryboardPanel();
  try {
    const result = await post(
      `/api/sessions/${encodeURIComponent(activeId)}/shots/${encodeURIComponent(shotId)}/redo`,
      {
        clientRequestId: crypto.randomUUID(),
        storyboardId: board.storyboardId,
        storyboardVersion: board.currentVersion,
        expectedRevision: board.revision,
      },
    );
    toast(`镜头已重做：新任务 ${result.shots[0].jobId.slice(0, 8)}，其他镜头未受影响。`);
    await refreshSession(activeId);
  } catch (error) {
    if (error.code === "VERSION_CONFLICT") {
      toast("分镜已变化，已重新读取，请再次点击重做。");
      await refreshSession(activeId).catch(() => {});
    } else {
      toast(error.message);
    }
  } finally {
    shotBusy.delete(shotId);
    renderStoryboardPanel();
  }
}

function renderAssetPanel() {
  if (panelMode !== "assets") return;
  const panel = $("#artifact-panel");
  const visible =
    assetsScopeOnly && activeId ? assets.filter((asset) => asset.projectId === activeId) : assets;
  panel.innerHTML = `${panelHeading("素材库", "close-artifact")}
    <p class="meta">库中共 ${assets.length} 张参考图。镜头可以引用同一数据目录内其他项目的素材；导入按项目与 SHA-256 去重，M2 不提供删除。</p>
    <div class="asset-actions">
      <button class="primary" data-action="upload-image" ${uploadingImage || !activeId ? "disabled" : ""}>${uploadingImage ? "正在导入…" : "导入参考图"}</button>
      <label class="chip"><input type="checkbox" id="assets-scope" ${assetsScopeOnly ? "checked" : ""} ${activeId ? "" : "disabled"}> 只看当前项目</label>
    </div>
    <div class="asset-grid">${
      visible.length
        ? visible.map(assetTile).join("")
        : '<p class="empty">还没有素材。导入一张 PNG / JPEG / WebP 参考图后即可被镜头引用。</p>'
    }</div>`;
  icons(panel);
  bindAssetTiles(panel);
}

function assetTile(asset) {
  const url = assetContentPath(asset.assetId);
  const size = `${(asset.sizeBytes / 1024).toFixed(1)} KB`;
  return `<figure class="asset-tile" data-asset-id="${escapeHTML(asset.assetId)}">
    ${url ? `<img src="${escapeHTML(url)}" alt="" loading="lazy" data-asset-image>` : ""}
    <strong>${escapeHTML(asset.assetId.slice(0, 8))} · ${escapeHTML(asset.mimeType.replace("image/", "").toUpperCase())}</strong>
    <span>${asset.dimensions.width}×${asset.dimensions.height} · ${escapeHTML(size)}</span>
    <span>项目 ${escapeHTML(asset.projectId)}</span>
    <span class="missing-note" hidden>本地文件缺失，重新导入同一张图片可恢复。</span>
  </figure>`;
}

function bindAssetTiles(root) {
  for (const image of root.querySelectorAll("[data-asset-image]")) {
    image.addEventListener("error", () => {
      const tile = image.closest(".asset-tile");
      if (!tile) return;
      tile.classList.add("missing");
      const note = tile.querySelector(".missing-note");
      if (note) note.hidden = false;
    });
  }
}

function renderUsagePanel() {
  if (panelMode !== "usage") return;
  const panel = $("#artifact-panel");
  const report = usageReport;
  if (!report) {
    panel.innerHTML = `${panelHeading("用量统计", "close-artifact")}<p class="empty">正在读取本地记录…</p>`;
    return;
  }
  const { jobs, cost, runs } = report;
  const count = (value) => (value === null || value === undefined ? "未知" : value.toLocaleString());
  panel.innerHTML = `${panelHeading("用量统计", "close-artifact")}
    <p class="meta">范围：${report.projectId ? `项目 ${escapeHTML(report.projectId)}` : "整个数据目录"} · 读取于 ${escapeHTML(new Date(report.generatedAt).toLocaleString())}</p>
    <h3>视频任务</h3><div class="metrics">${metric("任务总数", String(jobs.total))}${metric("未结束", String(jobs.open))}${metric("真实 Job", String(jobs.live))}${metric("模拟 Job", String(jobs.mock))}</div>
    <p class="meta">状态：${Object.entries(jobs.byStatus).map(([key, total]) => `${escapeHTML(key)} ${total}`).join(" · ") || "暂无任务"}</p>
    <h3>费用估算 <small>估算不是扣费</small></h3><div class="metrics">
      ${metric("登记估算合计", `${escapeHTML(cost.estimateAmount)} ${escapeHTML(cost.currency)}`)}
      ${metric("已取得账单", `${escapeHTML(cost.actual.reportedAmount)} ${escapeHTML(cost.currency)}`)}
      ${metric("账单未知任务", String(cost.actual.unknownJobs))}
      ${metric("报价版本", String(Object.keys(cost.byPriceVersion).length) || "0")}
    </div>
    <p class="meta">${cost.actual.status === "reported" ? "已按账单记录实际费用。" : "未取得账单的费用保持未知，不按估算冒充实际扣费。"}</p>
    ${Array.isArray(report.costByProvider) && report.costByProvider.length
      ? `<p class="meta">按供应商分桶（不换汇、不互相挪用）：${report.costByProvider
          .map((item) => `${escapeHTML(item.provider)} ${escapeHTML(item.estimated)} ${escapeHTML(item.currency)}${item.units ? ` · ${item.units} VIDEO_UNIT` : ""}（${item.jobs} 个任务）`)
          .join(" · ")}</p>`
      : ""}
    <h3>分镜与素材</h3><div class="metrics">
      ${metric("分镜数", String(report.storyboards.total))}${metric("镜头版本", String(report.storyboards.shotVersions))}
      ${metric("本项目素材", String(report.assets.total))}${metric("素材库总量", String(report.assets.libraryTotal))}
    </div>
    <h3>模型 Token</h3><div class="metrics">
      ${metric("Run 数", String(runs.runs))}${metric("已记录调用", String(runs.recordedCallCount))}
      ${metric("已知输入 Token", count(runs.observedInputTokens))}${metric("已知输出 Token", count(runs.observedOutputTokens))}
      ${metric("缓存命中 Token", count(runs.cacheHitTokens))}${metric("缓存命中率", runs.cacheHitRate == null ? "未知" : `${(runs.cacheHitRate * 100).toFixed(1)}%`)}
    </div>
    <p class="meta">${runs.tokenUsageComplete ? "全部调用都有 Token 明细。" : `部分调用缺少明细${runs.untrackedModelSteps ? `，另有 ${runs.untrackedModelSteps} 个旧模型步未跟踪` : ""}；未知不计作零。`}</p>
    <h3>按日记录</h3><div class="daily-list">${
      report.daily.length
        ? report.daily
            .map(
              (item) =>
                `<div>${escapeHTML(item.date)} · ${item.jobs} 个任务（真实 ${item.live}） · 估算 ${escapeHTML(item.estimated)}</div>`,
            )
            .join("")
        : "暂无记录"
    }</div>`;
  icons(panel);
}

function bindPanelMedia(root) {
  for (const card of root.querySelectorAll(".job-card")) {
    const job = (snapshot?.jobs || []).find((item) => item.jobId === card.dataset.jobId);
    if (job) bindMedia(card, job);
  }
}

function bindShotThumbnails(root) {
  for (const image of root.querySelectorAll("[data-shot-thumb]")) {
    if (image.dataset.bound) continue;
    image.dataset.bound = "true";
    image.addEventListener("error", () => {
      image.classList.add("broken");
      image.alt = "参考图不可用";
    });
  }
}
function openDialog(title, body) {
  dialogGeneration++;
  $("#dialog-content").innerHTML =
    `<div class="dialog-header"><h2>${title}</h2><button class="icon-button" data-action="close-dialog" aria-label="关闭弹窗" data-icon="close"></button></div>${body}`;
  icons($("#dialog"));
  if (!$("#dialog").open) $("#dialog").showModal();
  return dialogGeneration;
}
function renderConnection() {
  $("#model").textContent = health?.model || "未连接";
  $("#connection-label").textContent = health?.modelKind === "injected-test"
    ? "测试模型已连接"
    : !health?.apiKeyConfigured
      ? "待配置 API Key"
      : health.configuration?.validation.status === "verified"
        ? "模型连接已验证"
        : "API Key 已配置 · 待验证";
}
function applyConfiguration(config) {
  if (!health) return;
  health.configuration = config;
  health.apiKeyConfigured = config.apiKeyConfigured;
  if (health.modelKind === "deepseek") health.model = config.model;
  renderConnection();
  renderVideoMode();
}
async function showSettings() {
  if (configSubmitting) return toast("配置请求正在进行，请稍候。");
  const generation = openDialog("Agent 连接与配置", "<p>正在读取配置…</p>");
  const config = await api("/api/config");
  applyConfiguration(config);
  if ($("#dialog").open && generation === dialogGeneration) renderSettings(config);
}
function providerEntry(config, provider) {
  return (config.providers || []).find((item) => item.provider === provider) || null;
}
function videoProbeText(config, provider) {
  const entry = providerEntry(config, provider);
  if (!entry || !entry.keyConfigured) {
    return provider === "wan"
      ? "未配置视频密钥；填写并保存会执行一次零费用鉴权探测。"
      : "未配置该供应商密钥；保存只做本地格式检查，不执行鉴权探测。";
  }
  if (provider !== "wan") {
    return `已保存 ${entry.model}；鉴权/模型权限未验证（该供应商没有已核实的免费只读探测接口）。`;
  }
  const probe = config.videoProbe;
  if (!probe) return "已配置；保存视频 Key 会执行一次零费用只读探测（不创建任务）。";
  const at = new Date(probe.checkedAt).toLocaleString();
  if (probe.status === "authenticated") return `鉴权通过（视频权限未验证）· ${probe.host} · ${at}`;
  if (probe.status === "rejected") return `鉴权未通过：${probe.message}（配置已保存；真实生成可能被拒绝）`;
  return `探测未完成：${probe.message || "网络或协议问题"}（未验证，配置已保存）`;
}
function providerEstimateText(config, provider) {
  const entry = providerEntry(config, provider);
  if (!entry || !entry.estimate) return "当前没有可用报价，不能提交真实生成。";
  const estimate = entry.estimate;
  const amount = (Number(estimate.unitPrice) * 5).toFixed(2);
  const units = entry.resource
    ? `，另计 ${entry.resource.unitsPerSecond} ${entry.resource.unit}/秒`
    : "";
  return `5 秒估算 ${amount} ${estimate.currency}${units}；单价 ${estimate.unitPrice} ${estimate.currency}/秒（价格版本 ${estimate.priceVersion}）。实际费用以账单为准。`;
}
function renderVideoMode() {
  const mode = health?.configuration?.activeVideoMode ?? health?.videoMode;
  $("#video-mode-label").hidden = mode === "off";
  $("#video-mode-label").textContent = mode === "live" ? "真实视频 · 本地 MP4" : "模拟视频 · 无真实媒体";
  $("#video-workflow").innerHTML = mode === "mock"
    ? "模拟视频任务 <small>无真实媒体</small>" : mode === "live"
    ? "真实视频任务 <small>本地 MP4</small>" : "视频生成 <small>未启用</small>";
}
function renderSettings(config, notice = "") {
  const sources = { environment: "环境变量 / .env", provided: "启动配置", local: "本地配置", default: "默认值", unset: "未配置" };
  const modeOptions = [["off", "off · 仅文本创作"], ["mock", "mock · 模拟任务（无费用）"], ["live", "live · 真实视频"]];
  const selectedProvider = config.videoProvider || "wan";
  const providerOptions = [
    ["wan", "阿里云百炼万相（wan）"],
    ["kling", "Kling 新加坡直连（kling）"],
    ["fal", "fal 队列中转（fal）"],
  ];
  const keyFields = { wan: "videoApiKey", kling: "klingApiKey", fal: "falApiKey" };
  const configuredKey = {
    wan: config.videoApiKeyConfigured,
    kling: config.klingApiKeyConfigured,
    fal: config.falApiKeyConfigured,
  }[selectedProvider];
  const advancedFields = [
    ["videoModel", "视频模型（万相）", [["wan2.7-t2v-2026-06-12", "wan2.7-t2v-2026-06-12"]]],
    ["videoRegion", "地域（万相）", [["cn-beijing", "北京 · cn-beijing"]]],
    ["videoWorkspaceId", "万相业务空间 ID（可选）", null],
    ["videoMaxJobCost", "万相单任务上限（CNY）", null],
    ["videoMaxRunCost", "万相每 Run 上限（CNY）", null],
    ["klingMaxJobCost", "Kling 单任务上限（USD）", null],
    ["klingMaxRunCost", "Kling 每 Run 上限（USD）", null],
    ["klingMaxJobUnits", "Kling 单任务上限（VIDEO_UNIT）", null],
    ["klingMaxRunUnits", "Kling 每 Run 上限（VIDEO_UNIT）", null],
    ["falMaxJobCost", "fal 单任务上限（USD）", null],
    ["falMaxRunCost", "fal 每 Run 上限（USD）", null],
  ];
  const numericFields = new Set([
    "videoMaxJobCost", "videoMaxRunCost", "klingMaxJobCost", "klingMaxRunCost",
    "klingMaxJobUnits", "klingMaxRunUnits", "falMaxJobCost", "falMaxRunCost",
  ]);
  const validation = config.validation;
  const status = { unverified: "尚未验证", validating: "正在验证", verified: "连接已验证", failed: "验证失败" }[validation.status];
  const generation = openDialog("Agent 连接与配置", `
    <p>两个 Key 即可使用：DeepSeek 文本 Key 与所选视频供应商的 Key（万相 / Kling 直连 / fal 中转，由能力表逐条列出）。配置保存在本机并立即生效、无需重启——视频配置用于新登记任务，在途与历史任务仍按其原配置恢复。环境变量和 .env 优先，由启动配置提供的字段需在原处修改。</p>
    <form id="settings-form">
      <fieldset><legend>DeepSeek 文本创作</legend>
      <label for="config-key">DeepSeek API Key · ${sources[config.sources.apiKey] || "启动配置"}
        <input id="config-key" type="password" autocomplete="new-password" maxlength="512" spellcheck="false" autocapitalize="off" placeholder="${config.apiKeyConfigured ? "已配置，留空保持" : "输入 API Key"}" ${!config.editable.apiKey || config.busy ? "disabled" : ""}>
      </label>
      <label for="config-model">模型 · ${sources[config.sources.model] || "启动配置"}
        <input id="config-model" value="${escapeHTML(config.model)}" maxlength="100" required spellcheck="false" ${!config.editable.model || config.busy ? "disabled" : ""}>
      </label>
      ${config.editable.apiKey && config.apiKeyConfigured ? `<button type="button" class="secondary" data-config="clear" ${config.busy ? "disabled" : ""}>移除 DeepSeek 密钥</button>` : ""}
      </fieldset>
      <fieldset><legend>视频生成</legend>
      <label for="config-video-provider">视频供应商 · ${sources[config.sources.videoProvider] || "默认值"}
        <select id="config-video-provider" data-video-field="videoProvider" ${!config.editable.videoProvider || config.busy ? "disabled" : ""}>${providerOptions.map(([value, text]) => `<option value="${value}" ${selectedProvider === value ? "selected" : ""}>${text}</option>`).join("")}</select>
      </label>
      <label for="config-video-key">所选供应商 API Key · ${sources[config.sources[keyFields[selectedProvider]]] || "启动配置"}
        <input id="config-video-key" type="password" autocomplete="new-password" maxlength="512" spellcheck="false" autocapitalize="off" placeholder="${configuredKey ? "已配置，留空保持" : "输入该供应商的 API Key"}" ${!config.editable[keyFields[selectedProvider]] || config.busy ? "disabled" : ""}>
      </label>
      <label for="config-video-toggle" class="mode-toggle"><input id="config-video-toggle" type="checkbox" ${config.videoMode === "live" ? "checked" : ""} ${!config.editable.videoMode || config.busy ? "disabled" : ""}>启用真实视频生成（live）</label>
      ${config.videoMode === "mock" ? '<p class="meta">当前为模拟任务（mock）：不调用供应商、无费用；可在高级中切换。</p>' : ""}
      ${config.restartRequired ? `<p>当前运行：${escapeHTML(config.activeVideoMode)} · 已保存：${escapeHTML(config.videoMode)}。修改将在重启服务后生效。</p>` : ""}
      <p id="config-video-probe">视频权限：${escapeHTML(videoProbeText(config, selectedProvider))}</p>
      <details id="config-video-advanced">
        <summary>高级（可选）</summary>
        <div class="video-settings-grid">
        <label for="config-videoMode">视频模式 · ${sources[config.sources.videoMode] || "启动配置"}
          <select id="config-videoMode" data-video-field="videoMode" ${!config.editable.videoMode || config.busy ? "disabled" : ""}>${modeOptions.map(([value, text]) => `<option value="${value}" ${config.videoMode === value ? "selected" : ""}>${text}</option>`).join("")}</select>
        </label>
        ${advancedFields.map(([field, label, options]) => {
          const disabled = !config.editable[field] || config.busy ? "disabled" : "";
          const numeric = numericFields.has(field);
          return `<label for="config-${field}">${label} · ${sources[config.sources[field]] || "默认值"}${options
            ? `<select id="config-${field}" data-video-field="${field}" ${disabled}>${options.map(([value, text]) => `<option value="${value}" ${config[field] === value ? "selected" : ""}>${text}</option>`).join("")}</select>`
            : `<input id="config-${field}" data-video-field="${field}" value="${escapeHTML(config[field] ?? "")}" ${numeric ? `type="number" min="0" ${field.endsWith("Units") ? 'step="1"' : 'step="0.01"'} required` : 'type="text" maxlength="63" spellcheck="false" autocapitalize="off"'} ${disabled}>`}</label>`;
        }).join("")}
        </div>
        <p class="meta">万相业务空间留空时使用共享域名 dashscope.aliyuncs.com；填写后切换到专属域名，清空并保存即可移除。Kling/fal 固定官方端点，不提供自定义基址。</p>
      </details>
      <p id="config-video-estimate">固定规格：5 秒 · 720P · 16:9。${escapeHTML(providerEstimateText(config, selectedProvider))}</p>
      ${config.editable[keyFields[selectedProvider]] && configuredKey ? `<button type="button" class="secondary" data-config="clear-video" ${config.busy ? "disabled" : ""}>移除所选供应商密钥</button>` : ""}
      </fieldset>
      <p id="config-status" role="status">${escapeHTML(notice || (config.busy ? "请等待当前执行或验证结束。" : `${config.apiKeyConfigured ? "密钥已配置" : "密钥未配置"} · ${status}`))}${validation.checkedAt ? `<br>上次验证：${escapeHTML(new Date(validation.checkedAt).toLocaleString())}` : ""}</p>
      <p>保存不调用模型；保存视频 Key 时会执行一次零费用只读探测（不存在任务 ID 的查询，不创建任务、不产生费用）。「保存并验证 DeepSeek」会发起一次简短文本模型请求，可能产生少量费用。验证状态在服务重启后重置。</p>
      <div class="actions config-actions">
        <button type="submit" class="secondary" ${config.busy ? "disabled" : ""}>保存</button>
        <button type="button" class="primary" data-config="validate" ${config.busy ? "disabled" : ""}>保存并验证 DeepSeek</button>
      </div>
    </form>`);
  const form = $("#settings-form");
  const toggle = $("#config-video-toggle");
  const modeSelect = $("#config-videoMode");
  if (toggle && modeSelect) {
    toggle.addEventListener("change", () => { modeSelect.value = toggle.checked ? "live" : "off"; });
    modeSelect.addEventListener("change", () => { toggle.checked = modeSelect.value === "live"; });
  }
  const providerSelect = $("#config-video-provider");
  if (providerSelect) {
    providerSelect.addEventListener("change", () => {
      const provider = providerSelect.value;
      const entry = providerEntry(config, provider);
      const videoInput = $("#config-video-key");
      videoInput.value = "";
      videoInput.placeholder = entry && entry.keyConfigured ? "已配置，留空保持" : "输入该供应商的 API Key";
      // The field is rendered for the startup provider; switching providers must
      // re-evaluate its own editability instead of keeping the previous one's state.
      videoInput.disabled = !config.editable[keyFields[provider]] || config.busy;
      $("#config-video-probe").textContent = "视频权限：" + videoProbeText(config, provider);
      $("#config-video-estimate").textContent = "固定规格：5 秒 · 720P · 16:9。" + providerEstimateText(config, provider);
    });
  }
  const clearFields = { wan: "clearVideoApiKey", kling: "clearKlingApiKey", fal: "clearFalApiKey" };
  async function submitConfiguration(action) {
    if (configSubmitting || config.busy) return;
    if (!["clear", "clear-video"].includes(action) && !form.reportValidity()) return;
    const update = {};
    const input = $("#config-key");
    const key = input.value.trim();
    const videoInput = $("#config-video-key"), videoKey = videoInput.value.trim();
    const providerSelect = $("#config-video-provider");
    const provider = providerSelect ? providerSelect.value : (config.videoProvider || "wan");
    input.value = "";
    videoInput.value = "";
    if (action === "clear") update.clearApiKey = true;
    else if (action === "clear-video") update[clearFields[provider]] = true;
    else {
      if (config.editable.apiKey && key) update.apiKey = key;
      const model = $("#config-model").value.trim();
      if (config.editable.model && model !== config.model) update.model = model;
      if (config.editable[keyFields[provider]] && videoKey) update[keyFields[provider]] = videoKey;
      for (const input of form.querySelectorAll("[data-video-field]")) {
        const field = input.dataset.videoField, value = input.value.trim();
        if (config.editable[field] && value !== String(config[field] ?? "")) update[field] = value;
      }
    }
    configSubmitting = true;
    form.querySelectorAll("input, select, button").forEach((el) => { el.disabled = true; });
    $("#config-status").textContent = action === "validate" ? "正在保存并验证连接…" : "正在保存…";
    let result = config;
    let message;
    try {
      if (Object.keys(update).length)
        result = await api("/api/config", { method: "PATCH", body: JSON.stringify(update) });
      applyConfiguration(result);
      if (action === "validate") result = await post("/api/config/validate");
      message = action === "validate" ? "DeepSeek 连接已验证。" : "配置已保存。";
      if (result.autoEnabledLive) message += "已自动启用真实视频生成（可在设置中关闭）。";
      if (!["clear", "clear-video"].includes(action)) {
        if (provider !== "wan") {
          if (videoKey) message += "已保存该供应商密钥（鉴权与模型权限未验证）。";
        } else if (result.videoProbe) {
          if (result.videoProbe.status === "authenticated") message += "视频鉴权通过（视频权限未验证）。";
          else if (result.videoProbe.status === "rejected") message += "视频鉴权未通过：" + result.videoProbe.message;
          else message += "视频鉴权探测未完成（网络或协议问题）。";
        }
      }
      if (result.restartRequired) message += "部分视频设置将在重启服务后生效。";
    } catch (error) {
      message = error.message;
      try { result = await api("/api/config"); } catch {}
    } finally {
      configSubmitting = false;
    }
    applyConfiguration(result);
    if ($("#dialog").open && generation === dialogGeneration) renderSettings(result, message);
    else toast(message);
  }
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    submitConfiguration("save");
  });
  form.querySelectorAll("[data-config]").forEach((button) => {
    button.addEventListener("click", () => submitConfiguration(button.dataset.config));
  });
}
async function showArtifact(id, version) {
  selectedArtifact = await api(`/api/artifacts/${encodeURIComponent(id)}`);
  const artifact = selectedArtifact;
  const selected = version
    ? artifact.versions.find((v) => v.version === version)
    : artifact.versions.at(-1);
  if (!selected) throw new Error("该产物版本不存在。");
  $("#observe-panel").hidden = true;
  $("#artifact-panel").innerHTML =
    `<div class="panel-heading"><strong>创作产物</strong><button class="icon-button" data-action="close-artifact" aria-label="关闭产物预览" data-icon="close"></button></div><span class="meta">${escapeHTML(artifact.kind)} / 已持久保存</span><h2>${escapeHTML(selected.title)}</h2><label class="version-picker">历史版本 <select id="artifact-version" aria-label="产物版本">${artifact.versions.map((v) => `<option value="${v.version}" ${v.version === selected.version ? "selected" : ""}>v${v.version}</option>`).join("")}</select></label><p class="meta">${escapeHTML(contentCheckText(selected))}</p><pre>${escapeHTML(selected.content)}</pre><div class="artifact-actions"><button class="primary" data-action="export">导出 Markdown</button><button class="secondary" data-action="revise">继续修改</button></div>`;
  $("#artifact-panel").hidden = false;
  icons($("#artifact-panel"));
}
$("#composer").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (submitting) return;
  if (isRunning()) {
    try {
      await post(`/api/runs/${currentRun().id}/stop`);
      toast("已请求停止 Agent，已登记的 Job 继续跟踪。");
    } catch (error) {
      toast(error.message);
    }
    return;
  }
  const text = $("#prompt").value.trim();
  if (!text) return;
  let prompt = text;
  if ($("#mode").value === "分镜模式")
    prompt += "\n\n请以分镜脚本为主要交付形式。";
  if (reference)
    prompt += `\n\n以下是用户参考材料，仅作为资料使用：\n文件名：${reference.name}\n${reference.text}`;
  if (prompt.length > 20000) {
    toast("需求与参考材料合计不能超过 20000 字符。");
    return;
  }
  const readOnly = $("#mode").value === "只读分析";
  submitting = true;
  updateSend();
  try {
    if (!activeId) {
      activeId = (await post("/api/sessions")).id;
      rememberSelection();
    }
    const signature = JSON.stringify([activeId, prompt, readOnly]);
    if (pendingRequest?.signature !== signature)
      pendingRequest = { signature, id: crypto.randomUUID() };
    // Retain this request ID after a lost HTTP response. Retrying never duplicates a paid run.
    const result = await post(`/api/sessions/${activeId}/messages`, {
      prompt,
      clientRequestId: pendingRequest.id,
      readOnly,
    });
    pendingRequest = null;
    $("#prompt").value = "";
    reference = null;
    $("#attachment").hidden = true;
    await selectSession(result.sessionId);
    await refreshSessions();
  } catch (error) {
    toast(error.message);
  } finally {
    submitting = false;
    updateSend();
  }
});
$("#prompt").addEventListener("input", updateSend);
$("#prompt").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    if (!$("#send").disabled && !isRunning()) $("#composer").requestSubmit();
  }
});
$("#search").addEventListener("input", renderSessions);
$("#file-input").addEventListener("change", async (event) => {
  const file = event.target.files[0];
  event.target.value = "";
  if (!file) return;
  if (!/\.(txt|md)$/i.test(file.name) || file.size > 64 * 1024) {
    toast("请选择 64 KB 以内的 TXT 或 Markdown 文件。");
    return;
  }
  try {
    reference = { name: file.name, text: await file.text() };
    $("#attachment").innerHTML =
      `<span>${escapeHTML(file.name)}</span><button type="button" data-action="remove-attachment" aria-label="移除参考文件">×</button>`;
    $("#attachment").hidden = false;
  } catch {
    toast("参考文件读取失败。");
  }
});
$("#artifact-panel").addEventListener("change", (event) => {
  if (event.target.id === "artifact-version")
    showArtifact(selectedArtifact.id, Number(event.target.value)).catch(
      (error) => toast(error.message),
    );
  if (event.target.id === "assets-scope") {
    assetsScopeOnly = event.target.checked;
    renderAssetPanel();
  }
});
$("#storyboard-panel").addEventListener("change", (event) => {
  const shotId = event.target.dataset?.selectShot;
  if (!shotId) return;
  if (event.target.checked) shotSelection.add(shotId);
  else shotSelection.delete(shotId);
  renderStoryboardPanel();
});
$("#season-panel").addEventListener("change", (event) => {
  if (event.target.id === "season-picker") {
    const seasonId = event.target.value;
    if (!seasonId) startNewSeason();
    else loadSeason(seasonId);
    return;
  }
  const episodeId = event.target.dataset?.seasonEpisode;
  if (episodeId) toggleSeasonEpisode(episodeId);
});
$("#season-panel").addEventListener("input", (event) => {
  if (event.target.id === "season-title") seasonDraftTitle = event.target.value;
});
$("#image-input").addEventListener("change", async (event) => {
  const file = event.target.files[0];
  event.target.value = "";
  if (!file) return;
  if (!IMAGE_TYPES.includes(file.type)) {
    toast("请选择 PNG、JPEG 或 WebP 图片。");
    return;
  }
  if (file.size > MAX_IMAGE_BYTES) {
    toast("参考图不能超过 20 MB。");
    return;
  }
  if (!activeId) {
    toast("请先选择或创建一个创作会话。");
    return;
  }
  uploadingImage = true;
  renderAssetPanel();
  try {
    const form = new FormData();
    form.append("projectId", activeId);
    form.append("file", file, file.name);
    const created = await upload("/api/assets", form);
    toast(
      created.repaired
        ? "素材文件已按原记录恢复。"
        : created.deduplicated
          ? "这张图片已在素材库中，已复用原素材。"
          : "素材已导入。",
    );
    await loadAssets();
    renderAssetPanel();
  } catch (error) {
    toast(error.message);
  } finally {
    uploadingImage = false;
    renderAssetPanel();
  }
});
$("#dialog").addEventListener("close", () => {
  $("#dialog").querySelectorAll('input[type="password"]').forEach((key) => { key.value = ""; });
});
document.addEventListener("click", async (event) => {
  try {
    const template = event.target.closest("[data-template]");
    if (template) {
      $("#prompt").value = templates[template.dataset.template];
      updateSend();
      $("#prompt").focus();
      return;
    }
    const session = event.target.closest("[data-session]");
    if (session && submitting) return;
    if (session) {
      $("#dialog").close();
      await selectSession(session.dataset.session);
      return;
    }
    const artifact = event.target.closest("[data-artifact]");
    if (artifact) {
      $("#dialog").close();
      await showArtifact(artifact.dataset.artifact, Number(artifact.dataset.version) || undefined);
      return;
    }
    const retry = event.target.closest("[data-retry-job], [data-retry-download]");
    if (retry) {
      const download = Boolean(retry.dataset.retryDownload);
      const id = retry.dataset.retryDownload || retry.dataset.retryJob, generation = viewGeneration;
      if (retryingJobs.has(id)) return;
      retryingJobs.add(id);
      renderJobs();
      try {
        const current = snapshot?.jobs?.find((job) => job.jobId === id);
        const request = download ? { clientRequestId: crypto.randomUUID(), expectedRevision: current.revision } : {};
        const job = await post(`/api/jobs/${encodeURIComponent(id)}/${download ? "retry-download" : "retry-query"}`, request);
        if (generation === viewGeneration && snapshot) {
          snapshot.jobs = upsertJob(snapshot.jobs || [], job, activeId);
          toast(download ? "已登记原媒体的下载恢复。" : "已恢复原任务的状态查询。");
        }
      } catch (error) {
        if (error.code === "JOB_REVISION_CONFLICT" && generation === viewGeneration && snapshot) {
          const current = await api(`/api/jobs/${encodeURIComponent(id)}`);
          snapshot.jobs = upsertJob(snapshot.jobs || [], current, activeId);
        }
        throw error;
      } finally {
        retryingJobs.delete(id);
        if (generation === viewGeneration) renderJobs();
      }
      return;
    }
    const action = event.target.closest("[data-action]")?.dataset.action;
    if (action === "home" || action === "new") newChat();
    if (["projects", "search", "project-picker"].includes(action)) {
      $("#sidebar").hidden = false;
      await refreshSessions();
      if (action === "search") $("#search").focus();
    }
    if (action === "close-sidebar") $("#sidebar").hidden = true;
    if (action === "close-dialog") $("#dialog").close();
    if (action === "close-artifact") {
      panelMode = null;
      $("#artifact-panel").hidden = true;
    }
    if (action === "close-storyboard") {
      panelMode = null;
      storyboardOverride = null;
      $("#storyboard-panel").hidden = true;
    }
    if (action === "close-episode") {
      panelMode = null;
      $("#episode-panel").hidden = true;
    }
    if (action === "close-season") {
      panelMode = null;
      $("#season-panel").hidden = true;
    }
    if (action === "season-new") startNewSeason();
    if (action === "save-season") await saveSeason();
    if (action === "refresh-season" && seasonEditing?.seasonId)
      await loadSeason(seasonEditing.seasonId);
    else if (action === "refresh-season") {
      seasonView = snapshot?.season || null;
      seasonList = seasonView?.seasons || [];
      resetSeasonEdit(seasonView);
      renderSeasonPanel();
    }
    if (action === "close-observe") $("#observe-panel").hidden = true;
    if (action === "observe") {
      $("#artifact-panel").hidden = true;
      $("#observe-panel").hidden = false;
      panelMode = "observe";
      renderObserve();
    }
    if (action === "storyboard") openStoryboardPanel();
    if (action === "episode") openEpisodePanel();
    if (action === "season") openSeasonPanel();
    if (action === "open-scene-storyboard")
      await openSceneStoryboard(
        event.target.closest("[data-storyboard-id]")?.dataset.storyboardId,
      );
    if (action === "assets") await showAssets();
    if (action === "usage") await showUsage();
    if (action === "refresh-storyboard")
      await refreshSession(activeId).catch((error) => toast(error.message));
    if (action === "toggle-all-shots") toggleAllShots();
    if (action === "assemble-film") await assembleFilm();
    if (action === "render-film") await renderFilm();
    if (action === "save-subtitle") await saveSubtitle();
    if (action === "export-subtitle")
      await exportSubtitle(event.target.closest("[data-mode]")?.dataset.mode || "soft");
    if (action === "refresh-subtitle") {
      subtitleLoadedKey = null;
      subtitleState = null;
      subtitleError = null;
      if (panelMode === "storyboard") renderStoryboardPanel();
    }
    if (action === "refresh-film") {
      filmLoadedKey = null;
      filmState = null;
      filmError = null;
      renderState = null;
      renderError = null;
      if (panelMode === "storyboard") renderStoryboardPanel();
    }
    if (action === "toggle-episode-subtitles") {
      episodeSubtitleOn = !episodeSubtitleOn;
      applyEpisodeSubtitles($("#episode-film-video"));
    }
    if (action === "batch-selected") await confirmBatch();
    if (action === "confirm-batch") await submitBatch();
    if (action === "redo-shot") await redoShot(shotIdOf(event));
    if (action === "play-shot")
      startSequence(shotIdOf(event), event.target.closest("[data-media-url]")?.dataset.mediaUrl);
    if (action === "play-sequence") startSequence();
    if (action === "stop-sequence") stopSequence();
    if (action === "sequence-next") advanceSequence();
    if (action === "upload-image") $("#image-input").click();
    if (action === "attach") $("#file-input").click();
    if (action === "remove-attachment") {
      reference = null;
      $("#attachment").hidden = true;
    }
    if (action === "resume" && currentRun()) {
      const result = await post(`/api/runs/${currentRun().id}/resume`);
      await selectSession(result.sessionId);
    }
    if (action === "help")
      openDialog(
        "和 Agent 一起完成创作",
        `<p>描述需求后，Agent 会按需读取 Skills、管理项目记忆、调用工具并保存方案。侧栏「编排观测」显示每次执行的上下文、工具结果和模型用量。</p><p>会话与产物保存在本机。可以停止执行并从检查点继续；旧版产物可在预览中切换。只读分析可读取和等待已有 Job，不能创建任务或修改产物。</p><p>${health?.videoMode === "live" ? "已启用真实视频生成，可能产生费用。云端生成成功后下载并校验本地 MP4，可播放、拖动和下载；下载失败时可重试原任务。停止 Agent 不会取消云端生成。" : health?.videoMode === "mock" ? "已启用模拟视频任务，只提供模拟结果描述，没有真实媒体。任务卡片会持续更新；停止 Agent 不会取消 Job，关闭页面不影响服务中的任务。查询暂停时可点击恢复查询。" : "当前视频模式为 off，不提供新的视频任务。已有 Job 仍可查看，服务会继续跟踪。"}</p><div class="actions"><button class="primary" data-action="close-dialog">开始创作</button></div>`,
      );
    if (action === "settings") await showSettings();
    if (action === "library") {
      const all = (await api("/api/artifacts")).artifacts;
      openDialog(
        "创作产物",
        all.length
          ? all.map(artifactCard).join("")
          : "<p>尚无产物。让 Agent 保存方案后，产物会显示在这里。</p>",
      );
      icons($("#dialog"));
    }
    if (action === "export" && selectedArtifact) {
      const selected = selectedArtifact.versions.find(
        (v) => v.version === Number($("#artifact-version").value),
      );
      const url = URL.createObjectURL(
        new Blob([selected.content], { type: "text/markdown;charset=utf-8" }),
      );
      const link = document.createElement("a");
      link.href = url;
      link.download = `vagent-${selectedArtifact.id}-v${selected.version}.md`;
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      toast("已发起 Markdown 下载");
    }
    if (action === "revise" && selectedArtifact) {
      const id = selectedArtifact.id,
        projectId = selectedArtifact.projectId;
      await selectSession(projectId);
      $("#prompt").value = `请先读取产物 ${id} 的最新版本，再修改：`;
      updateSend();
      $("#prompt").focus();
    }
  } catch (error) {
    toast(error.message);
  }
});
async function initialize() {
  icons();
  try {
    [health, { token: csrf }] = await Promise.all([
      api("/api/health"),
      api("/api/session-token"),
    ]);
    renderConnection();
    $("#skills-label").textContent =
      `${health.skills.length} Skills · ${health.mcp.length} MCP`;
    renderVideoMode();
    $("#execution-label").textContent =
      health.modelKind === "deepseek"
        ? "真实 Agent · 按实际模型用量计费"
        : "测试环境 · 注入模型";
    await refreshSessions();
    let saved;
    try {
      saved = sessionStorage.getItem("vagent.active-session");
    } catch {}
    if (saved && sessions.some((s) => s.id === saved))
      await selectSession(saved);
    else render();
    if (!health.apiKeyConfigured && health.modelKind === "deepseek" && !snapshot?.jobs?.length) await showSettings();
  } catch (error) {
    health = null;
    $("#connection-label").textContent = "Agent 服务未连接";
    $("#execution-label").textContent = "请使用 vagent web 启动本地服务";
    toast(error.message);
    updateSend();
  }
}
initialize();
