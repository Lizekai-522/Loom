"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const state = window.VeriloWorkbench && typeof window.VeriloWorkbench === "object"
    ? window.VeriloWorkbench
    : {inspector: "diff", settings: "agent", worktreeId: null, computerSession: null, frameUrl: null, browserHistory: []};
  state.inspector ||= "diff";
  state.settings ||= "agent";
  state.browserHistory ||= [];
  window.VeriloWorkbench = state;
  const node = (tag, text, cls) => { const e = document.createElement(tag); if (text !== undefined) e.textContent = String(text); if (cls) e.className = cls; return e; };
  async function request(method, path, body) {
    const headers = {Accept: "application/json", "X-Loom-Console": "local"};
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (method !== "GET") headers["Idempotency-Key"] = crypto.randomUUID();
    const response = await fetch(path, {method, headers, credentials: "same-origin", cache: "no-store", body: body === undefined ? undefined : JSON.stringify(body)});
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || data.message || data.code || ("请求失败 " + response.status));
    return data;
  }
  function show(id, open) {
    const panel = $(id);
    if (!panel) return;
    panel.hidden = !open;
    const button = id === "inspector" ? $("open-inspector") : $("open-settings");
    button?.setAttribute("aria-expanded", String(open));
    if (id === "inspector") {
      const bar = $("rightbar");
      const any = open || ($("panel") && !$("panel").hidden);
      if (bar) bar.hidden = !any;
      $("shell")?.classList.toggle("panel-open", Boolean(any));
    }
  }
  function guard(host, action) {
    return async (...args) => {
      try { await action(...args); }
      catch (error) {
        let banner = host.querySelector("p.wm-error");
        if (!banner) { banner = node("p", "", "wm-error"); host.append(banner); }
        banner.textContent = error.message || String(error);
      }
    };
  }
  function field(label, input) { const wrap = node("label", label); input.setAttribute("aria-label", label); wrap.append(input); return wrap; }
  async function renderDiff(host) {
    host.replaceChildren();
    const projects = await request("GET", "/v1/workspace/projects");
    const items = projects.items || [];
    if (!items.length) { host.append(node("p", "还没有项目。")); return; }
    host.append(node("p", "选择项目后在这里核对差异。"));
    const row = node("div", undefined, "row");
    const select = node("select"); select.setAttribute("aria-label", "项目");
    for (const project of items) { const option = node("option", project.name); option.value = project.id; select.append(option); }
    if (state.projectId && [...select.options].some(option => option.value === state.projectId)) select.value = state.projectId;
    row.append(select); host.append(row);
    const list = node("div"); host.append(list);
    async function reload() {
      list.replaceChildren();
      if (!select.value) { list.append(node("p", "还没有项目。")); return; }
      const trees = await request("GET", "/v1/workspace/worktrees?project_id=" + encodeURIComponent(select.value));
      for (const item of trees.items || []) {
        const card = node("article");
        card.append(node("strong", item.branch), node("p", item.path));
        const actions = node("div", undefined, "row");
        const use = node("button", "终端使用此目录"); use.type = "button";
        use.addEventListener("click", () => {
          state.worktreeId = item.id; state.projectId = item.project_id;
          window.dispatchEvent(new CustomEvent("verilo:worktree", {detail: {id: item.id}}));
        });
        const diff = node("button", "查看差异"); diff.type = "button";
        diff.addEventListener("click", guard(card, async () => {
          const result = await request("GET", "/v1/workspace/worktrees/" + encodeURIComponent(item.id) + "/diff");
          let pre = card.querySelector("pre.wm-diff");
          if (!pre) { pre = node("pre", "", "wm-diff"); card.append(pre); }
          pre.textContent = (result.stat || "") + "\n" + (result.diff || "");
          const banner = card.querySelector("p.wm-error");
          if (banner) banner.remove();
        }));
        const apply = node("button", "应用到项目目录"); apply.type = "button";
        apply.addEventListener("click", guard(card, async () => {
          const result = await request("POST", "/v1/workspace/worktrees/" + encodeURIComponent(item.id) + "/apply", {});
          card.append(node("p", result.applied ? "已应用到工作区，尚未提交。" : "没有差异。"));
        }));
        const remove = node("button", "移除工作树"); remove.type = "button";
        remove.addEventListener("click", guard(card, async () => {
          await request("DELETE", "/v1/workspace/worktrees/" + encodeURIComponent(item.id));
          if (state.worktreeId === item.id) state.worktreeId = null;
          await reload();
        }));
        actions.append(use, diff, apply, remove); card.append(actions); list.append(card);
      }
      if (!(trees.items || []).length) list.append(node("p", "这个项目还没有工作树。"));
    }
    select.addEventListener("change", () => reload().catch(error => list.replaceChildren(node("p", error.message))));
    await reload();
  }
  async function renderBrowser(host) {
    host.replaceChildren();
    const sources = state.browserSources || [];
    if (sources.length) {
      host.append(node("p", "这个任务正在使用的页面。"));
      const list = node("ul");
      for (const item of sources.slice(0, 8)) {
        const li = node("li");
        const link = node("a", item.title || item.url);
        link.href = item.url; link.target = "_blank"; link.rel = "noreferrer noopener";
        li.append(link); list.append(li);
      }
      host.append(list);
    }
    const url = node("input"); url.type = "url"; url.placeholder = "https://"; url.setAttribute("aria-label", "页面地址");
    const go = node("button", "预览"); go.type = "button";
    const row = node("div", undefined, "row"); row.append(url, go); host.append(row);
    const out = node("div"); host.append(out);
    const nav = node("div", undefined, "row");
    const back = node("button", "后退"); back.type = "button";
    const reload = node("button", "重新加载"); reload.type = "button";
    nav.append(back, reload); host.append(nav);
    async function showPreview(target) {
      out.replaceChildren();
      const preview = await request("POST", "/v1/assistant/browser/preview", {url: target});
      state.browserHistory.push(target);
      out.append(node("p", (preview.title || preview.url) + " · " + (preview.renderer || "")));
      for (const element of preview.elements || []) {
        const pick = node("button", element.tag + " " + (element.text || element.selector)); pick.type = "button";
        pick.addEventListener("click", guard(out, async () => {
          const chosen = await request("POST", "/v1/assistant/browser/pick", {preview_id: preview.id, selector: element.selector});
          const prompt = $("prompt");
          if (prompt) { prompt.value = (prompt.value ? prompt.value + "\n" : "") + chosen.attachment; prompt.dispatchEvent(new Event("input", {bubbles: true})); prompt.focus(); }
        }));
        out.append(pick);
      }
      if (!(preview.elements || []).length) out.append(node("p", preview.text || "页面没有可点选的元素。"));
    }
    go.addEventListener("click", guard(out, async () => { if (url.value) await showPreview(url.value); }));
    reload.addEventListener("click", guard(out, async () => { if (url.value) await showPreview(url.value); }));
    back.addEventListener("click", guard(out, async () => {
      state.browserHistory.pop();
      const previous = state.browserHistory.pop();
      if (!previous) { out.replaceChildren(node("p", "没有更早的页面。")); return; }
      url.value = previous;
      await showPreview(previous);
    }));
  }
  async function renderComputer(host) {
    host.replaceChildren();
    let status;
    try { status = await request("GET", "/v1/computer/status"); }
    catch (error) { host.append(node("p", error.message)); return; }
    host.append(node("p", status.reason || (status.available ? "可以采集本机画面。" : "当前系统不能采集画面。")));
    const row = node("div", undefined, "row");
    const enable = node("button", "启用预览"); enable.type = "button";
    const input = node("button", "启用键鼠"); input.type = "button";
    const frame = node("button", "刷新画面"); frame.type = "button";
    const stop = node("button", "停止"); stop.type = "button";
    row.append(enable, input, frame, stop); host.append(row);
    const view = node("div"); host.append(view);
    async function grant(allow) {
      const result = await request("POST", "/v1/computer/enable", {allow_input: allow});
      state.computerSession = result.session_id;
      view.replaceChildren(node("p", "会话 " + (result.session_id || "") + " · " + (result.expires_in || 0) + " 秒"));
    }
    enable.addEventListener("click", () => grant(false).catch(error => view.replaceChildren(node("p", error.message))));
    input.addEventListener("click", () => grant(true).catch(error => view.replaceChildren(node("p", error.message))));
    stop.addEventListener("click", () => request("POST", "/v1/computer/disable", {}).then(result => { state.computerSession = null; view.replaceChildren(node("p", result.enabled ? "仍在启用" : "已停止")); }).catch(error => view.replaceChildren(node("p", error.message))));
    frame.addEventListener("click", guard(view, async () => {
      if (!state.computerSession) { view.replaceChildren(node("p", "先启用预览。")); return; }
      const response = await fetch("/v1/computer/frame", {headers: {"X-Loom-Console": "local", "X-Verilo-Computer-Session": state.computerSession, Accept: "image/png"}, credentials: "same-origin", cache: "no-store"});
      if (!response.ok) { view.replaceChildren(node("p", "画面 " + response.status)); return; }
      const blob = await response.blob();
      if (state.frameUrl) URL.revokeObjectURL(state.frameUrl);
      state.frameUrl = URL.createObjectURL(blob);
      const image = node("img"); image.alt = "当前画面"; image.src = state.frameUrl;
      image.addEventListener("click", guard(view, async event => {
        const rect = image.getBoundingClientRect();
        const left = Number(response.headers.get("X-Desktop-Left") || 0);
        const top = Number(response.headers.get("Y-Desktop-Top") || response.headers.get("X-Desktop-Top") || 0);
        const width = Number(response.headers.get("X-Desktop-Width") || rect.width);
        const height = Number(response.headers.get("X-Desktop-Height") || rect.height);
        const x = Math.round(left + (event.offsetX / rect.width) * width);
        const y = Math.round(top + (event.offsetY / rect.height) * height);
        await request("POST", "/v1/computer/click", {session_id: state.computerSession, frame_id: response.headers.get("X-Frame-Id"), x, y, button: "left"});
      }));
      view.replaceChildren(image);
    }));
  }
  async function renderFiles(host) {
    host.replaceChildren();
    const projects = await request("GET", "/v1/workspace/projects");
    const select = node("select"); select.setAttribute("aria-label", "项目文件");
    for (const project of projects.items || []) { const option = node("option", project.name); option.value = project.id; select.append(option); }
    if (state.projectId && [...select.options].some(option => option.value === state.projectId)) select.value = state.projectId;
    const query = node("input"); query.type = "search"; query.setAttribute("aria-label", "过滤文件");
    const row = node("div", undefined, "row"); row.append(select, query); host.append(row);
    const list = node("div"); host.append(list);
    async function reloadFiles() {
      list.replaceChildren();
      if (!select.value) { list.append(node("p", "还没有项目。")); return; }
      const files = await request("GET", "/v1/workspace/projects/" + encodeURIComponent(select.value) + "/files?q=" + encodeURIComponent(query.value || ""));
      for (const file of files.items || []) {
        const button = node("button", file.path); button.type = "button";
        button.addEventListener("click", guard(list, async () => {
          const body = await request("GET", "/v1/workspace/projects/" + encodeURIComponent(select.value) + "/file?rel=" + encodeURIComponent(file.path));
          const prompt = $("prompt");
          if (prompt) {
            const mention = "@" + body.path + " ";
            if (!prompt.value.includes(mention)) prompt.value = (prompt.value ? prompt.value + " " : "") + mention;
            prompt.dispatchEvent(new Event("input", {bubbles: true}));
            prompt.focus();
          }
          window.VeriloHarness?.addMention?.(body.path);
        }));
        list.append(button);
      }
      if (!(files.items || []).length) list.append(node("p", "没有匹配的文件。"));
    }
    select.addEventListener("change", () => reloadFiles().catch(error => list.replaceChildren(node("p", error.message))));
    query.addEventListener("input", () => reloadFiles().catch(error => list.replaceChildren(node("p", error.message))));
    await reloadFiles();
  }
  async function renderDag(host) {
    host.replaceChildren(node("p", "编程会话的角色进度。批准仍由你本人做出。"));
    const tasks = await request("GET", "/v1/assistant/tasks");
    const sessions = await request("GET", "/v1/sessions").catch(() => ({items: []}));
    const ids = [];
    const titles = new Map();
    for (const task of tasks.items || []) {
      const sessionId = task.kind === "coding" && task.state && task.state.session_id;
      if (sessionId && !ids.includes(sessionId)) { ids.push(sessionId); titles.set(sessionId, task.title); }
    }
    for (const session of sessions.items || []) {
      if (["running", "awaiting_approval", "paused", "needs_attention"].includes(session.state) && !ids.includes(session.session_id)) {
        ids.push(session.session_id); titles.set(session.session_id, session.title);
      }
    }
    if (!ids.length) { host.append(node("p", "还没有编程会话。")); return; }
    for (const sessionId of ids) {
      const card = node("article");
      card.append(node("strong", titles.get(sessionId) || "编程会话"));
      try {
        const view = await request("GET", "/v1/sessions/" + encodeURIComponent(sessionId));
        const labels = {running: "进行中", awaiting_approval: "等你批准", paused: "已暂停", needs_attention: "需要处理", delivered: "已交付", verified: "已复核", terminated: "已结束"};
        card.append(node("p", labels[view.state] || view.state || ""));
        const list = node("ol");
        const stepLabel = {queued: "排队", waiting: "等待", running: "进行中", done: "完成", succeeded: "完成", skipped: "跳过", failed: "失败", blocked: "已暂停", pending: "未开始"};
        for (const step of view.steps || []) list.append(node("li", (step.label || step.role || step.key) + " · " + (stepLabel[step.status] || step.status || "")));
        card.append(list);
        if (view.state === "awaiting_approval" && view.approval && view.approval.digest) {
          card.append(node("p", view.approval.message || ""));
          const approve = node("button", "批准实现计划"); approve.type = "button";
          approve.addEventListener("click", guard(card, async () => {
            await request("POST", "/v1/sessions/" + encodeURIComponent(view.session_id) + "/approve", {expected_digest: view.approval.digest});
            card.append(node("p", "已批准，开始编写。"));
          }));
          card.append(approve);
        }
      } catch (error) { card.append(node("p", error.message)); }
      host.append(card);
    }
  }
  async function renderInspector() {
    const host = $("inspector-body");
    if (!host) return;
    try {
      if (state.inspector === "browser") await renderBrowser(host);
      else if (state.inspector === "computer") await renderComputer(host);
      else if (state.inspector === "files") await renderFiles(host);
      else if (state.inspector === "dag") await renderDag(host);
      else await renderDiff(host);
    } catch (error) { host.replaceChildren(node("p", error.message)); }
  }
  async function renderAgent(host) {
    host.replaceChildren();
    for (const kind of ["memory", "skills", "commands", "hooks"]) {
      const section = node("section"); section.append(node("h3", kind));
      const data = await request("GET", "/v1/product/entries/" + kind);
      for (const item of data.items || []) {
        const row = node("div", undefined, "row");
        row.append(node("span", item.title + (item.enabled ? "" : " · 停用")));
        if (kind === "commands" || kind === "skills") {
          const use = node("button", "使用"); use.type = "button";
          use.addEventListener("click", guard(row, async () => {
            await request("POST", `/v1/product/entries/${encodeURIComponent(kind)}/${encodeURIComponent(item.id)}/run`, {input: ""});
            location.assign("/console");
          }));
          row.append(use);
        }
        section.append(row);
      }
      const title = node("input"); title.maxLength = 120; title.placeholder = "名称"; title.setAttribute("aria-label", kind + " 名称");
      const content = node("input"); content.maxLength = 4000; content.placeholder = kind === "hooks" ? "" : "内容"; content.hidden = kind === "hooks";
      const add = node("button", "添加"); add.type = "button";
      add.addEventListener("click", guard(section, async () => {
        const body = {title: title.value.trim(), content: kind === "hooks" ? "" : content.value, enabled: true, config: kind === "hooks" ? {event: "reply.completed", action: "browser.notification"} : {}};
        await request("POST", "/v1/product/entries/" + encodeURIComponent(kind), body);
        await renderAgent(host);
      }));
      const form = node("div", undefined, "row"); form.append(title, content, add); section.append(form);
      host.append(section);
    }
  }
  async function renderRuntime(host) {
    host.replaceChildren();
    let overview;
    try { overview = await request("GET", "/v1/settings/overview"); }
    catch (error) { host.append(node("p", error.message)); host.append(link("打开运行时设置", "/console/settings")); return; }
    const rows = [
      ["模型", overview.model?.configured ? "READY" : "UNSET"],
      ["搜索", overview.search?.configured ? "READY" : "UNSET"],
      ["邮箱", overview.mail?.configured ? "READY" : "UNSET"],
      ["沙箱", overview.sandbox?.image_set ? "READY" : "UNSET"],
      ["复核", overview.verification?.configured ? "READY" : "UNSET"],
    ];
    for (const [label, badge] of rows) {
      const row = node("div", undefined, "row"); row.append(node("span", label), node("strong", badge)); host.append(row);
    }
    host.append(link("模型与密钥", "/console/admin#section-model"));
    host.append(link("记忆与命令", "/console/preferences#memory"));
  }
  function link(text, href) { const a = node("a", text); a.href = href; return a; }
  async function renderSettings() {
    const host = $("settings-drawer-body");
    if (!host) return;
    try {
      if (state.settings === "runtime") await renderRuntime(host);
      else await renderAgent(host);
    } catch (error) { host.replaceChildren(node("p", error.message)); }
  }
  const TABS = ["files", "diff", "browser", "computer", "dag"];
  const ACTIVE = new Set(["queued", "running", "blocked", "waiting_approval", "awaiting_confirmation"]);
  const SESSION_ACTIVE = new Set(["running", "awaiting_approval", "paused", "needs_attention"]);
  const WEB_STEPS = new Set(["search", "read", "write", "done"]);
  const MEMORY_KEY = "verilo.inspector.memory";
  const WIDTH_KEY = "verilo.inspector.width";
  let memory = {dismissed: [], reasons: {}};
  try { memory = {...memory, ...JSON.parse(sessionStorage.getItem(MEMORY_KEY) || "{}")}; } catch { /* keep defaults */ }
  if (!Array.isArray(memory.dismissed)) memory.dismissed = [];
  if (!memory.reasons || typeof memory.reasons !== "object") memory.reasons = {};
  const dismissed = new Set(memory.dismissed);
  let currentReasons = {};
  let rendered = "";
  const storedWidth = Number(localStorage.getItem(WIDTH_KEY));
  let width = Number.isFinite(storedWidth) && storedWidth >= 280 ? storedWidth : 420;
  let computerOn = false;
  let computerChecked = 0;
  let last = {tasks: [], sessions: [], messages: []};
  function persistMemory() {
    memory.dismissed = [...dismissed];
    try { sessionStorage.setItem(MEMORY_KEY, JSON.stringify(memory)); } catch { /* private mode */ }
  }
  function clampWidth(px) {
    const max = Math.max(280, Math.min(840, Math.round(window.innerWidth * 0.68)));
    return Math.max(280, Math.min(max, Math.round(px)));
  }
  function applyWidth() {
    const px = clampWidth(width);
    $("shell")?.style.setProperty("--inspector-width", px + "px");
    const handle = $("inspector-resize");
    if (handle) {
      handle.setAttribute("aria-valuenow", String(px));
      handle.setAttribute("aria-valuemax", String(Math.max(280, Math.min(840, Math.round(window.innerWidth * 0.68)))));
    }
  }
  function setWidth(px, persist) {
    width = Math.max(280, Math.min(840, Math.round(px)));
    if (persist) { try { localStorage.setItem(WIDTH_KEY, String(width)); } catch { /* private mode */ } }
    applyWidth();
  }
  function resizeBy(delta) { setWidth(width + delta, true); }
  function layout() {
    const inspectorOpen = $("inspector") && !$("inspector").hidden;
    const panelOpen = $("panel") && !$("panel").hidden;
    const open = Boolean(inspectorOpen || panelOpen);
    const bar = $("rightbar");
    if (bar) bar.hidden = !open;
    $("shell")?.classList.toggle("panel-open", open);
    $("open-inspector")?.setAttribute("aria-expanded", String(Boolean(inspectorOpen)));
    if (open) applyWidth();
  }
  function visibleIds() { return TABS.filter(id => currentReasons[id] && !dismissed.has(id)); }
  function paint() {
    const shown = visibleIds();
    for (const id of TABS) {
      const tab = document.querySelector(`[data-inspector-tab="${id}"]`);
      if (tab) tab.hidden = !shown.includes(id);
    }
    const inspector = $("inspector");
    if (inspector) inspector.hidden = shown.length === 0;
    if (shown.length && !shown.includes(state.inspector)) state.inspector = shown[0];
    for (const button of document.querySelectorAll("#inspector [data-inspector]")) {
      button.setAttribute("aria-selected", String(shown.includes(button.dataset.inspector) && button.dataset.inspector === state.inspector));
    }
    const signature = shown.includes(state.inspector)
      ? state.inspector + "|" + (state.inspector === "browser" ? (state.browserSources || []).map(item => item.url).join(",") : "") + "|" + (state.inspector === "dag" ? currentReasons.dag || "" : "")
      : "";
    if (signature && rendered !== signature) { rendered = signature; renderInspector(); }
    if (!signature) rendered = "";
    layout();
  }
  function computeReasons() {
    const reasons = {};
    const projects = new Set();
    const trees = new Set();
    const sessionIds = [];
    const sources = [];
    const browserIds = [];
    for (const task of last.tasks) {
      if (!task || !ACTIVE.has(task.status)) continue;
      const detail = task.state || {};
      if (task.kind === "coding" && detail.session_id && !sessionIds.includes(detail.session_id)) sessionIds.push(detail.session_id);
      if (task.kind === "coding" && detail.project_id) projects.add(detail.project_id);
      if (task.kind === "coding" && detail.worktree_id) trees.add(detail.worktree_id);
      if (task.kind === "research" && WEB_STEPS.has(detail.step || "")) browserIds.push(task.id);
      if (task.kind === "research") {
        for (const item of [...(detail.pages || []), ...(detail.results || []), ...(detail.sources || [])]) {
          if (item && item.url && !sources.some(source => source.url === item.url)) sources.push({url: item.url, title: item.title || item.url});
        }
      }
    }
    for (const session of last.sessions) {
      if (session && SESSION_ACTIVE.has(session.state) && !sessionIds.includes(session.session_id)) sessionIds.push(session.session_id);
    }
    const selected = $("workspace-project")?.value || "";
    if (selected && sessionIds.length) projects.add(selected);
    if (state.worktreeId) {
      trees.add(state.worktreeId);
      if (selected) projects.add(selected);
    }
    const mentionKey = last.messages.filter(message => Array.isArray(message?.meta?.mentions) && message.meta.mentions.length).map(message => message.id).sort().join(",");
    if (mentionKey) projects.add("mentions");
    state.browserSources = sources;
    if (sessionIds.length) reasons.dag = sessionIds.slice().sort().join(",");
    if (projects.size || mentionKey) reasons.files = [...projects].sort().join(",") + "|" + mentionKey;
    if (trees.size) reasons.diff = [...trees].sort().join(",");
    if (browserIds.length) reasons.browser = browserIds.slice().sort().join(",");
    if (state.computerSession || computerOn) reasons.computer = state.computerSession || "enabled";
    return reasons;
  }
  function consider() {
    const reasons = computeReasons();
    for (const id of TABS) {
      if (!reasons[id]) continue;
      if (memory.reasons[id] !== reasons[id]) dismissed.delete(id);
      memory.reasons[id] = reasons[id];
    }
    currentReasons = reasons;
    persistMemory();
    paint();
  }
  function closeTab(id) {
    dismissed.add(id);
    persistMemory();
    paint();
  }
  function toggle() {
    const shown = visibleIds();
    if (shown.length) {
      for (const id of shown) dismissed.add(id);
      persistMemory();
      paint();
      return;
    }
    if (!Object.keys(currentReasons).length) {
      const banner = $("notice");
      if (banner) { banner.hidden = false; banner.className = "notice"; banner.textContent = "当前没有任务或会话在使用检查器。"; }
      return;
    }
    for (const id of Object.keys(currentReasons)) dismissed.delete(id);
    persistMemory();
    paint();
  }
  async function refreshComputer() {
    if (Date.now() - computerChecked < 15000) return;
    computerChecked = Date.now();
    let on = false;
    try {
      const status = await request("GET", "/v1/computer/status");
      on = Boolean(status.enabled || status.session_id);
      if (status.session_id) state.computerSession = state.computerSession || status.session_id;
    } catch { on = Boolean(state.computerSession); }
    if (on !== computerOn) { computerOn = on; consider(); }
  }
  function sync(partial = {}) {
    if (partial.tasks) last.tasks = partial.tasks;
    if (partial.sessions) last.sessions = partial.sessions;
    else if (window.VeriloHarness?.sessions) last.sessions = window.VeriloHarness.sessions();
    if (partial.messages) last.messages = partial.messages;
    consider();
    refreshComputer();
  }
  function bindResize() {
    const handle = $("inspector-resize");
    if (!handle) return;
    handle.addEventListener("pointerdown", event => {
      if (event.button !== 0) return;
      event.preventDefault();
      const startX = event.clientX;
      const startW = clampWidth(width);
      handle.setPointerCapture(event.pointerId);
      document.body.classList.add("inspector-resizing");
      const move = e => setWidth(startW + (startX - e.clientX), false);
      const end = () => {
        handle.removeEventListener("pointermove", move);
        document.body.classList.remove("inspector-resizing");
        setWidth(clampWidth(width), true);
      };
      handle.addEventListener("pointermove", move);
      handle.addEventListener("pointerup", end, {once: true});
      handle.addEventListener("pointercancel", end, {once: true});
    });
    handle.addEventListener("keydown", event => {
      if (event.key === "ArrowLeft") { event.preventDefault(); resizeBy(24); }
      else if (event.key === "ArrowRight") { event.preventDefault(); resizeBy(-24); }
      else if (event.key === "Home") { event.preventDefault(); setWidth(420, true); }
    });
    handle.addEventListener("dblclick", () => setWidth(420, true));
    window.addEventListener("resize", applyWidth);
  }
  window.VeriloInspector = {sync, layout, resizeBy, toggle, closeTab};
  function bind() {
    bindResize();
    applyWidth();
    layout();
    $("open-inspector")?.addEventListener("click", () => toggle());
    $("open-settings")?.addEventListener("click", () => { const open = $("settings-drawer").hidden; show("settings-drawer", open); if (open) renderSettings(); });
    for (const button of document.querySelectorAll("#inspector [data-inspector]")) button.addEventListener("click", () => {
      state.inspector = button.dataset.inspector;
      rendered = "";
      paint();
    });
    for (const button of document.querySelectorAll("[data-close-inspector]")) button.addEventListener("click", event => {
      event.preventDefault();
      event.stopPropagation();
      closeTab(button.dataset.closeInspector);
    });
    document.addEventListener("change", event => {
      if (event.target && (event.target.id === "workspace-project" || event.target.id === "workspace-branch")) sync();
    });
    window.addEventListener("verilo:worktree", () => sync());
    window.addEventListener("verilo:preferences", () => sync());
    for (const button of document.querySelectorAll("#settings-drawer [data-settings]")) button.addEventListener("click", () => {
      state.settings = button.dataset.settings;
      for (const item of document.querySelectorAll("#settings-drawer [data-settings]")) item.setAttribute("aria-selected", String(item === button));
      renderSettings();
    });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", bind);
  else bind();
})();
