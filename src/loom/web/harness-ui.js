"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const state = {mentions: [], linked: null, tasks: [], sessions: []};
  const node = (tag, text, cls) => { const e = document.createElement(tag); if (text !== undefined) e.textContent = String(text); if (cls) e.className = cls; return e; };
  async function request(method, path, body) {
    if (window.VeriloWorkspace?.request) return window.VeriloWorkspace.request(method, path, body);
    const headers = {Accept: "application/json", "X-Loom-Console": "local"};
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (method !== "GET") headers["Idempotency-Key"] = crypto.randomUUID();
    const response = await fetch(path, {method, headers, credentials: "same-origin", cache: "no-store", body: body === undefined ? undefined : JSON.stringify(body)});
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || data.message || data.code || ("请求失败 " + response.status));
    return data;
  }
  function projectId() { return $("workspace-project")?.value || ""; }
  window.VeriloHarness = {
    extras() {
      const extra = {};
      if (state.mentions.length) extra.mentions = state.mentions.slice();
      if (state.linked) extra.linked_session_id = state.linked;
      return extra;
    },
    addMention(path) { if (path && !state.mentions.includes(path) && state.mentions.length < 8) { state.mentions.push(path); renderChips(); } },
    onSent() { state.mentions = []; state.linked = null; renderChips(); }
  };
  function renderChips() {
    const host = $("mention-chips");
    if (!host) return;
    host.replaceChildren();
    host.hidden = state.mentions.length === 0 && !state.linked;
    for (const path of state.mentions) {
      const chip = node("button", "@" + path, "mention-chip"); chip.type = "button";
      chip.addEventListener("click", () => { state.mentions = state.mentions.filter(item => item !== path); renderChips(); });
      host.append(chip);
    }
    if (state.linked) {
      const chip = node("button", "#" + state.linked.slice(0, 8), "mention-chip"); chip.type = "button";
      chip.addEventListener("click", () => { state.linked = null; renderChips(); });
      host.append(chip);
    }
  }
  function insertAtCursor(text) {
    const input = $("prompt");
    if (!input) return;
    const start = input.selectionStart ?? input.value.length;
    const end = input.selectionEnd ?? start;
    input.setRangeText(text, start, end, "end");
    input.dispatchEvent(new Event("input", {bubbles: true}));
    input.focus();
  }
  async function renderRail(filter = "") {
    const rail = $("session-rail");
    if (!rail) return;
    const needle = filter.trim().toLowerCase();
    rail.replaceChildren();
    const tasks = state.tasks.filter(task => !needle || (task.title || "").toLowerCase().includes(needle) || (task.card?.card_summary || "").toLowerCase().includes(needle));
    for (const task of tasks.slice(0, 30)) {
      const button = node("button", "", "session-row"); button.type = "button";
      const mark = task.card?.worker_state || task.status || "";
      button.append(node("strong", task.title || "任务"), node("small", mark));
      button.addEventListener("click", () => {
        if ($("panel")?.hidden) $("toggle-panel")?.click();
        document.querySelector('#panel .as-tabs button[data-tab="active"]')?.click();
      });
      rail.append(button);
    }
    for (const session of state.sessions.filter(item => !needle || (item.title || "").toLowerCase().includes(needle)).slice(0, 20)) {
      const button = node("button", "", "session-row"); button.type = "button";
      button.append(node("strong", session.title || "编程会话"), node("small", session.state || ""));
      button.addEventListener("click", () => { location.assign("/console/sessions#s=" + encodeURIComponent(session.session_id)); });
      rail.append(button);
    }
    if (!rail.children.length) rail.append(node("p", needle ? "没有匹配的任务。" : "还没有任务。", "session-empty"));
    if (needle && projectId()) {
      try {
        const files = await request("GET", "/v1/workspace/projects/" + encodeURIComponent(projectId()) + "/files?q=" + encodeURIComponent(needle));
        for (const file of (files.items || []).slice(0, 12)) {
          const button = node("button", file.path, "session-row"); button.type = "button";
          button.addEventListener("click", () => { insertAtCursor("@" + file.path + " "); window.VeriloHarness.addMention(file.path); });
          rail.append(button);
        }
      } catch (error) { rail.append(node("p", error.message, "session-empty")); }
    }
  }
  async function refreshRail() {
    try {
      const [tasks, sessions] = await Promise.all([
        request("GET", "/v1/assistant/tasks").catch(() => ({items: []})),
        request("GET", "/v1/sessions").catch(() => ({items: []}))
      ]);
      state.tasks = tasks.items || [];
      state.sessions = sessions.items || [];
    } catch { state.tasks = []; state.sessions = []; }
    await renderRail($("task-search")?.value || "");
  }
  function closePalette() { const box = $("command-palette"); if (box) box.hidden = true; }
  async function openPalette() {
    const box = $("command-palette");
    if (!box) return;
    box.hidden = false;
    const input = $("command-palette-input");
    input.value = "";
    input.focus();
    await paintPalette("");
  }
  async function paintPalette(query) {
    const list = $("command-palette-list");
    if (!list) return;
    const needle = query.trim().toLowerCase();
    const actions = [
      ["新建任务", () => location.assign("/console/sessions")],
      ["模型与密钥", () => location.assign("/console/admin#section-model")],
      ["打开检查器", () => $("open-inspector")?.click()],
      ["打开终端", () => $("toggle-terminal")?.click()],
      ["打开任务", () => $("toggle-panel")?.click()],
      ["技能", () => $("open-skills")?.click()],
      ["项目", () => location.assign("/console/projects")]
    ];
    list.replaceChildren();
    for (const [label, run] of actions) {
      if (needle && !label.toLowerCase().includes(needle)) continue;
      const button = node("button", label); button.type = "button";
      button.addEventListener("click", () => { closePalette(); run(); });
      list.append(button);
    }
    try {
      const commands = await request("GET", "/v1/product/entries/commands");
      for (const item of commands.items || []) {
        if (!item.enabled || (needle && !item.title.toLowerCase().includes(needle))) continue;
        const button = node("button", "/" + item.title); button.type = "button";
        button.addEventListener("click", () => { closePalette(); insertAtCursor("/" + item.title + " "); });
        list.append(button);
      }
      const skills = await request("GET", "/v1/product/entries/skills");
      for (const item of skills.items || []) {
        if (!item.enabled || (needle && !item.title.toLowerCase().includes(needle))) continue;
        const button = node("button", "$" + item.title); button.type = "button";
        button.addEventListener("click", () => { closePalette(); insertAtCursor("$" + item.title + " "); });
        list.append(button);
      }
    } catch (error) { list.append(node("p", error.message)); }
  }
  function cycleMode() {
    const select = $("workspace-confirm");
    if (!select || !select.options.length) return;
    select.selectedIndex = (select.selectedIndex + 1) % select.options.length;
    select.dispatchEvent(new Event("change", {bubbles: true}));
  }
  function mount() {
    const chips = node("div", undefined, "mention-chips"); chips.id = "mention-chips"; chips.hidden = true;
    $("attachments")?.before(chips);
    $("task-search")?.addEventListener("input", () => renderRail($("task-search").value));
    $("open-skills")?.addEventListener("click", () => {
      $("open-settings")?.click();
      document.querySelector('#settings-drawer [data-settings="agent"]')?.click();
    });
    $("collapse-sidebar")?.addEventListener("click", () => {
      document.querySelector(".verilo-sidebar")?.classList.toggle("collapsed");
      $("shell")?.classList.toggle("rail");
    });
    const accountTrigger = $("account-trigger");
    const accountMenu = $("account-menu");
    if (accountTrigger && accountMenu) {
      const setAccountOpen = open => {
        accountMenu.hidden = !open;
        accountTrigger.setAttribute("aria-expanded", String(open));
      };
      accountTrigger.addEventListener("click", event => {
        event.stopPropagation();
        setAccountOpen(accountMenu.hidden);
      });
      accountMenu.addEventListener("click", event => {
        if (event.target.closest("[role='menuitem']")) setAccountOpen(false);
      });
      document.addEventListener("click", event => {
        if (!accountMenu.hidden && !accountTrigger.contains(event.target) && !accountMenu.contains(event.target)) setAccountOpen(false);
      });
      document.addEventListener("keydown", event => {
        if (event.key === "Escape" && !accountMenu.hidden) { setAccountOpen(false); accountTrigger.focus(); }
      });
    }
    $("command-palette-input")?.addEventListener("input", () => paintPalette($("command-palette-input").value));
    $("command-palette")?.addEventListener("keydown", event => { if (event.key === "Escape") closePalette(); });
    document.addEventListener("keydown", event => {
      const key = event.key.toLowerCase();
      if ((event.metaKey || event.ctrlKey) && key === "k") { event.preventDefault(); $("command-palette")?.hidden ? openPalette() : closePalette(); }
      if ((event.metaKey || event.ctrlKey) && key === "b" && !event.altKey) { event.preventDefault(); $("collapse-sidebar")?.click(); }
      if (event.key === "Tab" && event.shiftKey && event.target?.id === "prompt") { event.preventDefault(); cycleMode(); }
    });
    const prompt = $("prompt");
    prompt?.addEventListener("input", async () => {
      const value = prompt.value.slice(0, prompt.selectionStart || 0);
      const token = value.match(/(^|\s)([@#$/])([^\s]*)$/);
      const pop = $("suggest-pop") || mountPop();
      if (!token || !projectId() && token[2] === "@") { pop.hidden = true; return; }
      const kind = token[2], typed = token[3];
      let items = [];
      try {
        if (kind === "@") {
          const files = await request("GET", "/v1/workspace/projects/" + encodeURIComponent(projectId()) + "/files?q=" + encodeURIComponent(typed));
          items = (files.items || []).slice(0, 8).map(file => ({label: "@" + file.path, apply: () => { replaceToken(token[0], "@" + file.path + " "); window.VeriloHarness.addMention(file.path); }}));
        } else if (kind === "/") {
          const commands = await request("GET", "/v1/product/entries/commands");
          items = (commands.items || []).filter(item => item.enabled && item.title.startsWith(typed)).slice(0, 8)
            .map(item => ({label: "/" + item.title, apply: () => replaceToken(token[0], "/" + item.title + " ")}));
        } else if (kind === "$") {
          const skills = await request("GET", "/v1/product/entries/skills");
          items = (skills.items || []).filter(item => item.enabled && item.title.startsWith(typed)).slice(0, 8)
            .map(item => ({label: "$" + item.title, apply: () => replaceToken(token[0], "$" + item.title + " ")}));
        } else if (kind === "#") {
          items = state.sessions.filter(item => (item.title || "").includes(typed)).slice(0, 8)
            .map(item => ({label: "# " + (item.title || item.session_id), apply: () => { replaceToken(token[0], ""); state.linked = item.session_id; renderChips(); }}));
        }
      } catch (error) { pop.replaceChildren(node("p", error.message)); pop.hidden = false; return; }
      pop.replaceChildren();
      for (const item of items) {
        const button = node("button", item.label); button.type = "button";
        button.addEventListener("click", () => { item.apply(); pop.hidden = true; });
        pop.append(button);
      }
      pop.hidden = items.length === 0;
    });
    window.addEventListener("verilo:event", () => refreshRail());
    refreshRail();
  }
  function mountPop() {
    const pop = node("div", undefined, "suggest-pop"); pop.id = "suggest-pop"; pop.hidden = true;
    ($("composer") || document.body).append(pop);
    return pop;
  }
  function replaceToken(token, text) {
    const input = $("prompt");
    const at = input.selectionStart || input.value.length;
    const start = at - token.trimStart().length;
    const prefix = token.startsWith(" ") || token.startsWith("\n") ? token[0] : "";
    input.setRangeText(prefix + text, Math.max(0, start), at, "end");
    input.dispatchEvent(new Event("input", {bubbles: true}));
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", mount);
  else mount();
})();
