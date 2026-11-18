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
    const coding = (tasks.items || []).filter(task => task.kind === "coding" && task.state && task.state.session_id);
    if (!coding.length) { host.append(node("p", "还没有编程会话。")); return; }
    for (const task of coding) {
      const card = node("article");
      const summary = task.card ? task.card.worker_state + " · " + task.card.card_summary : task.status;
      card.append(node("strong", task.title), node("p", summary));
      try {
        const view = await request("GET", "/v1/sessions/" + encodeURIComponent(task.state.session_id));
        const list = node("ol");
        for (const step of view.steps || []) list.append(node("li", (step.label || step.role || step.key) + " · " + (step.status || "")));
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
  function bind() {
    $("open-inspector")?.addEventListener("click", () => { const open = $("inspector").hidden; show("inspector", open); if (open) renderInspector(); });
    $("open-settings")?.addEventListener("click", () => { const open = $("settings-drawer").hidden; show("settings-drawer", open); if (open) renderSettings(); });
    for (const button of document.querySelectorAll("#inspector [data-inspector]")) button.addEventListener("click", () => {
      state.inspector = button.dataset.inspector;
      for (const item of document.querySelectorAll("#inspector [data-inspector]")) item.setAttribute("aria-selected", String(item === button));
      renderInspector();
    });
    for (const button of document.querySelectorAll("#settings-drawer [data-settings]")) button.addEventListener("click", () => {
      state.settings = button.dataset.settings;
      for (const item of document.querySelectorAll("#settings-drawer [data-settings]")) item.setAttribute("aria-selected", String(item === button));
      renderSettings();
    });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", bind);
  else bind();
})();
