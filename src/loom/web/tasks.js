"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const ACTIVE = new Set(["queued", "running", "waiting_approval", "awaiting_confirmation", "needs_attention"]);
  const PAUSED = new Set(["blocked", "paused"]);
  const ENDED = new Set(["completed", "cancelled", "delivered", "verified", "terminated"]);
  const LABEL = {
    queued: "排队中", running: "进行中", waiting_approval: "等你批准", blocked: "已暂停",
    awaiting_confirmation: "待你确认", completed: "已完成", cancelled: "已取消",
    needs_attention: "需要处理", paused: "已暂停", delivered: "已交付", verified: "已复核", terminated: "已结束"
  };
  const KIND = {research: "调研", coding: "编程", session: "编程会话"};
  let rows = [];
  let filter = "active";
  let selected = null;
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
  function bucket(status) {
    if (PAUSED.has(status)) return "paused";
    if (ENDED.has(status)) return "ended";
    if (ACTIVE.has(status)) return "active";
    return "ended";
  }
  function matches(row) {
    const needle = ($("task-search").value || "").trim().toLowerCase();
    const group = bucket(row.status);
    if (filter !== "all" && group !== filter) return false;
    if (!needle) return true;
    return (row.title || "").toLowerCase().includes(needle) || (row.id || "").toLowerCase().includes(needle) || (row.prompt || "").toLowerCase().includes(needle);
  }
  function emptyText() {
    const needle = ($("task-search").value || "").trim();
    if (needle) return "没有匹配的任务。";
    if (filter === "paused") return "没有已暂停的任务。";
    if (filter === "ended") return "没有已结束的任务。";
    if (filter === "active") return "没有进行中的任务。";
    return "还没有任务。";
  }
  function paintList() {
    const host = $("task-list");
    const visible = rows.filter(matches);
    host.replaceChildren();
    if (!visible.length) { host.append(node("p", emptyText(), "task-empty")); return; }
    if (!visible.some(row => row.key === selected)) selected = visible[0].key;
    for (const row of visible) {
      const button = node("button", "", "task-row");
      button.type = "button";
      button.setAttribute("aria-current", String(row.key === selected));
      button.append(node("strong", row.title || "任务"), node("small", (KIND[row.kind] || row.kind) + " · " + (LABEL[row.status] || row.status)));
      button.addEventListener("click", () => { selected = row.key; paintList(); paintDetail(); });
      host.append(button);
    }
  }
  function paintDetail() {
    const host = $("task-detail");
    const row = rows.find(item => item.key === selected && matches(item));
    host.replaceChildren();
    if (!row) { host.append(node("p", emptyText(), "task-empty")); return; }
    const card = node("article", "", "task-card");
    card.append(node("h2", row.title || "任务"));
    const meta = node("p");
    meta.append(node("span", LABEL[row.status] || row.status, "app-status"));
    meta.append(document.createTextNode("  " + (KIND[row.kind] || "")));
    card.append(meta);
    if (row.prompt) card.append(node("p", row.prompt));
    const actions = node("div", "", "row");
    if (row.sessionId) {
      const open = node("a", "打开编程会话", "app-btn");
      open.href = "/console/sessions#s=" + encodeURIComponent(row.sessionId);
      actions.append(open);
    }
    if (row.kind === "research" && row.status === "blocked") {
      const retry = node("button", "重试", "app-btn");
      retry.type = "button";
      retry.addEventListener("click", async () => {
        retry.disabled = true;
        try { await request("POST", "/v1/assistant/tasks/" + encodeURIComponent(row.id) + "/retry", {}); await load(); }
        catch (error) { $("notice").hidden = false; $("notice").textContent = error.message; }
        finally { retry.disabled = false; }
      });
      actions.append(retry);
    }
    if (actions.childElementCount) card.append(actions);
    host.append(card);
  }
  async function load() {
    const [tasks, sessions] = await Promise.all([
      request("GET", "/v1/assistant/tasks").catch(() => ({items: []})),
      request("GET", "/v1/sessions").catch(() => ({items: []}))
    ]);
    const seen = new Set();
    rows = [];
    for (const task of tasks.items || []) {
      const sessionId = task.state && task.state.session_id;
      if (sessionId) seen.add(sessionId);
      rows.push({key: "task:" + task.id, id: task.id, title: task.title, prompt: task.brief || "", status: task.status, kind: task.kind, sessionId});
    }
    for (const session of sessions.items || []) {
      if (seen.has(session.session_id)) continue;
      rows.push({key: "session:" + session.session_id, id: session.session_id, title: session.title, prompt: "", status: session.state, kind: "session", sessionId: session.session_id});
    }
    paintList();
    paintDetail();
  }
  for (const button of document.querySelectorAll("[data-filter]")) button.addEventListener("click", () => {
    filter = button.dataset.filter;
    for (const item of document.querySelectorAll("[data-filter]")) item.setAttribute("aria-pressed", String(item === button));
    paintList();
    paintDetail();
  });
  $("task-search").addEventListener("input", () => { paintList(); paintDetail(); });
  load().catch(error => { $("notice").hidden = false; $("notice").textContent = error.message; });
})();
