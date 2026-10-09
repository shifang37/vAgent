const $ = (selector) => document.querySelector(selector);
const paths = {
  "plus-square":
    '<rect x="4" y="4" width="16" height="16" rx="5"/><path d="M12 8v8m-4-4h8"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
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
  completed: "已完成",
  cancelled: "已停止",
  failed: "执行失败",
  interrupted: "已中断",
};
const artifactNames = { all: "全部产物", brief: "方案", script: "脚本", storyboard: "分镜" };
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
const currentRun = () => snapshot?.run;
const isRunning = () => currentRun()?.status === "running";
function toast(message) {
  $("#toast").textContent = message;
  $("#toast").hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => ($("#toast").hidden = true), 4500);
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
const post = (path, body = {}) =>
  api(path, { method: "POST", body: JSON.stringify(body) });
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
      snapshot = next;
      renderConnection();
      render();
      if (runChanged) refreshSessions().catch((error) => toast(error.message));
    } catch {
      toast("执行状态读取失败，请刷新页面。");
    }
  });
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
async function selectSession(id) {
  const generation = ++viewGeneration;
  stream?.close();
  const next = await api(`/api/sessions/${encodeURIComponent(id)}`);
  if (generation !== viewGeneration) return;
  activeId = id;
  snapshot = next;
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
    const after = conversation.querySelector(".artifact-card, .run-details");
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
  const chatting = Boolean(snapshot?.messages.length || snapshot?.run);
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
  const detailsOpen = $("#conversation details")?.open;
  $("#conversation").innerHTML =
    (snapshot?.messages || [])
      .map(
        (m) =>
          `<div class="message ${m.role === "user" ? "user" : ""}">${m.role === "assistant" ? '<div class="agent-name"><img src="./mark.svg" alt="" />vagent <span class="demo-label">Agent 回复</span></div>' : ""}${escapeHTML(m.text)}</div>`,
      )
      .join("") + draftHTML() + (snapshot?.artifacts || []).map(artifactCard).join("");
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
      `<span class="run-state ${run.status}">${statusNames[run.status] || run.status}</span><span>模型 ${run.modelSteps} 步 · 工具 ${run.toolCalls} 次</span><button data-action="observe">查看编排</button>${run.resumable && run.status !== "running" ? '<button data-action="resume">从检查点继续</button>' : ""}${run.errorCode ? `<p>${escapeHTML(run.errorCode)} · ${escapeHTML(run.answer)}</p>` : ""}`;
  } else $("#run-status").hidden = true;
  icons($("#conversation"));
  if (followLatest) conversation.scrollTop = conversation.scrollHeight;
  renderSessions();
  updateSend();
  renderObserve();
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
    <h3>Skills · 按需读取</h3>${(health?.skills || []).map((s) => `<div class="capability"><strong>${escapeHTML(s.name)}</strong><small>${escapeHTML(s.description)}<br>版本 ${escapeHTML(s.version)}</small></div>`).join("")}
    <h3>MCP 服务</h3>${(health?.mcp || []).map((s) => `<div class="capability"><strong>${escapeHTML(s.name)} · 已连接</strong><small>${escapeHTML(s.transport)} / ${escapeHTML(s.protocolVersion)}<br>${s.tools.map(escapeHTML).join("<br>")}</small></div>`).join("") || '<p class="meta">未启用 MCP 服务</p>'}
    <h3>工具目录</h3>${(health?.tools || []).map((t) => `<div class="tool-line"><span>${escapeHTML(t.name)}</span><small>${t.effect === "read" ? "读取" : "写入"} · ${escapeHTML(t.source)}</small></div>`).join("")}
    <h3>模型用量</h3><div class="metrics">${metric("已知输入 Token", value(usage?.observedInputTokens))}${metric("已知输出 Token", value(usage?.observedOutputTokens))}${metric("缓存命中 Token", value(usage?.cacheHitTokens))}${metric("缓存命中率", usage?.cacheHitRate == null ? "未知" : `${(usage.cacheHitRate * 100).toFixed(1)}%`)}</div>
    <p class="meta">${usage?.tokenUsageComplete ? "本次调用 Token 用量完整" : "未知用量不计作零消耗"}。Redis ${health?.answerCacheEnabled ? "可用于只读任务" : "回答缓存未启用"}。</p>
    ${Array.isArray(run?.toolTrace) ? `<h3>工具输入与结果</h3>${run.toolTrace.map((t) => `<details class="trace-item"><summary>${escapeHTML(t.name)}</summary><small>输入</small><pre>${escapeHTML(JSON.stringify(t.arguments, null, 2))}</pre><small>结果</small><pre>${escapeHTML(t.result || "等待工具完成")}</pre></details>`).join("") || '<p class="meta">暂无工具调用</p>'}` : ""}
    <h3>执行事件</h3><div class="event-list">${(run?.events || []).map((e) => `<div><small>#${e.sequence}</small> ${escapeHTML(e.type)} ${escapeHTML(e.name || "")}${e.ok === false ? " ×" : ""}</div>`).join("") || "暂无事件"}</div>`;
  icons($("#observe-panel"));
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
}
async function showSettings() {
  if (configSubmitting) return toast("配置请求正在进行，请稍候。");
  const generation = openDialog("Agent 连接与配置", "<p>正在读取配置…</p>");
  const config = await api("/api/config");
  applyConfiguration(config);
  if ($("#dialog").open && generation === dialogGeneration) renderSettings(config);
}
function renderSettings(config, notice = "") {
  const sources = { environment: "环境变量 / .env", provided: "启动配置", local: "本地配置", default: "默认值", unset: "未配置" };
  const validation = config.validation;
  const status = { unverified: "尚未验证", validating: "正在验证", verified: "连接已验证", failed: "验证失败" }[validation.status];
  const generation = openDialog("Agent 连接与配置", `
    <p>配置保存在本机，下次执行即可生效。环境变量和 .env 优先；由启动配置提供的字段需在原处修改。</p>
    <p>视频模式：${config.videoMode === "mock" ? "mock（模拟，无真实媒体）" : "off（未启用）"} · ${sources[config.sources.videoMode] || "启动配置"}。视频模式在启动时生效，修改需重启。</p>
    <form id="settings-form">
      <label for="config-key">DeepSeek API Key · ${sources[config.sources.apiKey] || "启动配置"}
        <input id="config-key" type="password" autocomplete="new-password" maxlength="512" spellcheck="false" autocapitalize="off" placeholder="${config.apiKeyConfigured ? "已配置，留空保持" : "输入 API Key"}" ${!config.editable.apiKey || config.busy ? "disabled" : ""}>
      </label>
      <label for="config-model">模型 · ${sources[config.sources.model] || "启动配置"}
        <input id="config-model" value="${escapeHTML(config.model)}" maxlength="100" required spellcheck="false" ${!config.editable.model || config.busy ? "disabled" : ""}>
      </label>
      <p id="config-status" role="status">${escapeHTML(notice || (config.busy ? "请等待当前执行或验证结束。" : `${config.apiKeyConfigured ? "密钥已配置" : "密钥未配置"} · ${status}`))}${validation.checkedAt ? `<br>上次验证：${escapeHTML(new Date(validation.checkedAt).toLocaleString())}` : ""}</p>
      <p>保存不调用模型。「保存并验证」会发起一次简短模型请求，可能产生少量费用。验证状态在服务重启后重置。</p>
      <div class="actions config-actions">
        ${config.editable.apiKey && config.apiKeyConfigured ? `<button type="button" class="secondary" data-config="clear" ${config.busy ? "disabled" : ""}>移除密钥</button>` : ""}
        <button type="submit" class="secondary" ${config.busy ? "disabled" : ""}>保存</button>
        <button type="button" class="primary" data-config="validate" ${config.busy ? "disabled" : ""}>保存并验证</button>
      </div>
    </form>`);
  const form = $("#settings-form");
  async function submitConfiguration(action) {
    if (configSubmitting || config.busy) return;
    if (action !== "clear" && !form.reportValidity()) return;
    const update = {};
    const input = $("#config-key");
    const key = input.value.trim();
    input.value = "";
    if (action === "clear") update.clearApiKey = true;
    else {
      if (config.editable.apiKey && key) update.apiKey = key;
      const model = $("#config-model").value.trim();
      if (config.editable.model && model !== config.model) update.model = model;
    }
    configSubmitting = true;
    form.querySelectorAll("input, button").forEach((el) => { el.disabled = true; });
    $("#config-status").textContent = action === "validate" ? "正在保存并验证连接…" : "正在保存…";
    let result = config;
    let message;
    try {
      if (Object.keys(update).length)
        result = await api("/api/config", { method: "PATCH", body: JSON.stringify(update) });
      applyConfiguration(result);
      if (action === "validate") result = await post("/api/config/validate");
      message = action === "validate" ? "连接已验证，可以开始创作。" : action === "clear" ? "本地密钥已移除。" : "配置已保存，下次执行生效。";
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
      toast("已请求停止，等待当前操作结束。");
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
});
$("#dialog").addEventListener("close", () => {
  const key = $("#config-key");
  if (key) key.value = "";
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
      await showArtifact(artifact.dataset.artifact);
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
    if (action === "close-artifact") $("#artifact-panel").hidden = true;
    if (action === "close-observe") $("#observe-panel").hidden = true;
    if (action === "observe") {
      $("#artifact-panel").hidden = true;
      $("#observe-panel").hidden = false;
      renderObserve();
    }
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
        '<p>描述需求后，Agent 会按需读取 Skills、管理项目记忆、调用工具并保存方案。侧栏「编排观测」显示每次执行的上下文、工具结果和模型用量。</p><p>会话与产物保存在本机。可以停止执行并从检查点继续；旧版产物可在预览中切换。只读分析模式禁止修改项目或保存产物。当前不提供视频生成。</p><div class="actions"><button class="primary" data-action="close-dialog">开始创作</button></div>',
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
    if (!health.apiKeyConfigured && health.modelKind === "deepseek") await showSettings();
  } catch (error) {
    health = null;
    $("#connection-label").textContent = "Agent 服务未连接";
    $("#execution-label").textContent = "请使用 vagent web 启动本地服务";
    toast(error.message);
    updateSend();
  }
}
initialize();
