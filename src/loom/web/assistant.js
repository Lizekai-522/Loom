"use strict";
// Resident assistant page. Credentials never enter the page: the loopback console
// marker or the public-preview HttpOnly cookie authenticates every request.
// Live updates arrive over a same-origin server-sent-event stream read with fetch.
(() => {
  const $ = (id) => document.getElementById(id);
  const state = {me: null, thread: null, cursor: 0, messages: new Map(), order: [], tasks: new Map(), todos: new Map(),
                 approvals: new Map(), mandates: [], settings: null, tab: "active", streamAbort: null, retry: 1000,
                 canWrite: true, canApprove: false, attachments: [], pendingMessage: null,
                 mail: {status: {configured: false}, items: [], open: null, detail: null}};
  const MAIL_STATUS = {new: "待处理", history: "历史邮件", skipped: "自动邮件", summarized: "已摘要", filtered: "已拦截", failed: "处理失败"};
  const OUTBOX = {queued: "排队发送", sending: "发送中", accepted: "服务器已接受", partial: "部分收件人被拒", rejected: "服务器拒收",
                  unknown: "发送结果待核对", failed: "发送失败"};
  const STATUS = {queued: "排队中", running: "进行中", waiting_approval: "等你批准", blocked: "已暂停",
                  awaiting_confirmation: "待你确认", completed: "已完成", cancelled: "已取消"};
  const KIND = {research: "调研", coding: "编程"};

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = String(text);
    return e;
  }
  const key = () => (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2));
  const usd = (micro) => "$" + (Number(micro || 0) / 1e6).toFixed(micro < 10000 ? 4 : 2);
  function notice(message, error = false) {
    $("notice").textContent = message || "";
    $("notice").className = error ? "notice error" : "notice";
    $("notice").hidden = !message;
  }
  async function request(method, path, body, idempotency) {
    const headers = {Accept: "application/json", "X-Loom-Console": "local"};
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (idempotency) headers["Idempotency-Key"] = idempotency;
    const response = await fetch(path, {method, headers, body: body === undefined ? undefined : JSON.stringify(body),
                                        credentials: "same-origin", cache: "no-store"});
    const text = await response.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch { data = null; }
    if (!response.ok) {
      const error = new Error(String(data?.code || response.status));
      error.status = response.status; error.data = data;
      throw error;
    }
    return data;
  }
  function explain(error) {
    if (error.status === 401) {
      if (location.protocol === "https:") { location.assign("/preview/login"); return "登录已过期。"; }
      return "本机入口未启用，请用项目启动器启动服务。";
    }
    if (error.status === 403) return "这个操作需要你本人（具名用户身份）来做。";
    if (error.status === 409) return "状态已经变化，已刷新，请核对后再试。";
    if (error.status === 422) return "输入不符合要求。";
    if (error.status === 429) return "请求太频繁，请稍后再试。";
    return "暂时连不上 Loom 服务，稍后会自动重连。";
  }
  function canNotify() { return "Notification" in window && Notification.permission === "granted"; }
  function ping(title, body) {
    // Local browser notification only (no push service, nothing leaves the page).
    if (document.hidden && canNotify()) { try { new Notification(title, {body}); } catch { /* ignore */ } }
  }

  // ---------- conversation ----------
  function messageNode(m) {
    if (m.kind === "report") {
      const card = el("div", "as-card report");
      card.append(el("h3", null, "调研报告：" + (m.meta.title || "")), el("div", "body", m.text));
      const list = el("ol", "as-sources");
      for (const s of m.meta.sources || []) {
        const li = el("li");
        const a = el("a", null, s.title || s.url); a.href = s.url; a.target = "_blank"; a.rel = "noopener noreferrer nofollow";
        li.append(a);
        if (s.snippet_only) li.append(el("span", "snip", "（只读到摘要）"));
        list.append(li);
      }
      if (list.children.length) card.append(el("p", "as-section-title", "来源（网页内容未经核实）"), list);
      const task = state.tasks.get(m.meta.task_id);
      if (task && task.status === "awaiting_confirmation" && state.canApprove) {
        const row = el("div", "row");
        row.append(button("确认完成", "primary", () => act("POST", `/v1/assistant/tasks/${task.id}/confirm`)),
                   button("继续追问", "ghost", () => { $("prompt").value = "关于《" + task.title + "》，"; $("prompt").focus(); }));
        card.append(row);
      }
      return card;
    }
    if (m.kind === "mail") {
      const c = el("div", "as-card mail");
      c.append(el("h3", null, m.meta.draft_id ? "回复草稿" : "邮件"), el("div", "body", m.text));
      const row = el("div", "row");
      row.append(button(m.meta.draft_id ? "查看并修改草稿" : "打开邮件", "ghost", () => openMail(m.meta.mail_id)));
      if (!m.meta.draft_id && m.meta.needs_reply && state.canWrite)
        row.append(button("生成回复", "ghost", () => requestDraft(m.meta.mail_id)));
      c.append(row);
      return c;
    }
    if (m.kind === "brief") { const c = el("div", "as-card brief"); c.append(el("h3", null, "每日简报"), el("div", "body", m.text)); return c; }
    if (m.kind === "approval") {
      const approval = state.approvals.get(m.meta.approval_id);
      const c = el("div", "as-card approval");
      c.append(el("h3", null, approval ? approval.title : m.text));
      if (approval) {
        c.append(el("p", null, approval.detail));
        if (approval.status === "pending") {
          const row = el("div", "row");
          if (state.canApprove) row.append(button("批准", "primary", () => decide(approval, "approved")),
                                           button("拒绝", "ghost", () => decide(approval, "rejected")));
          else row.append(el("span", "muted", "需要具名用户批准"));
          c.append(row);
        } else c.append(el("p", "muted", {approved: "已批准", rejected: "已拒绝", superseded: "草稿已修改，这次批准已作废"}[approval.status] || approval.status));
        if (approval.action === "send_mail") c.append(button("查看收件人与正文", "ghost", () => openMail(approval.payload.mail_id)));
      }
      return c;
    }
    if (m.kind === "task") {
      const task = state.tasks.get(m.meta.task_id);
      const c = el("div", "as-card task");
      const title = el("h3", null, m.text);
      if (task) title.append(el("span", "as-pill " + task.status, STATUS[task.status] || task.status));
      c.append(title);
      if (task) c.append(el("p", "muted", progressText(task)));
      return c;
    }
    const node = el("div", "as-msg " + (m.role === "notice" ? "notice" : m.role) + (m.status === "streaming" ? " streaming" : "")
                    + (m.status === "failed" ? " failed" : ""), m.text);
    if (m.role === "assistant" && m.status === "done" && m.meta && m.meta.cost_microusd !== undefined)
      node.append(el("span", "meta", "本条约 " + usd(m.meta.cost_microusd)));
    if (m.status === "failed" && m.meta?.message && m.text !== m.meta.message) node.append(el("span", "meta", m.meta.message));
    if (m.role === "user" && m.status === "queued") node.append(el("span", "meta", "等待回复…"));
    const hidden = Object.values(m.meta?.redacted || {}).reduce((a, b) => a + b, 0);
    if (hidden) node.append(el("span", "meta", `已隐藏 ${hidden} 处个人信息或密钥，不会发给模型`));
    if (m.role === "assistant" && m.meta?.safety && m.meta.safety.category !== "self_harm")
      node.append(el("span", "meta", m.meta.safety.action === "neutral" ? "涉政话题：只陈述公开事实" : "已按内容规则处理"));
    return node;
  }
  function progressText(t) {
    const s = t.state || {};
    if (t.status === "blocked") return s.blocked_message || s.attention || "已暂停";
    if (t.kind === "coding") return s.session_state ? "编程会话：" + s.session_state : "编程会话已创建";
    return {plan: "规划搜索", search: "正在搜索", read: "正在阅读网页", write: "正在整理报告", done: "已完成"}[s.step] || "";
  }
  function button(label, cls, onClick) {
    const b = el("button", cls, label); b.type = "button";
    b.addEventListener("click", async () => { b.disabled = true; try { await onClick(); } finally { b.disabled = false; } });
    return b;
  }
  function upsertMessage(m) {
    const fresh = !state.messages.has(m.id);
    state.messages.set(m.id, {...(state.messages.get(m.id) || {}), ...m});
    if (fresh) state.order.push(m.id);
    renderMessage(m.id);
  }
  function renderMessage(id) {
    const m = state.messages.get(id);
    let holder = document.querySelector(`[data-mid="${CSS.escape(id)}"]`);
    const stream = $("stream");
    const atBottom = stream.scrollHeight - stream.scrollTop - stream.clientHeight < 80;
    const node = messageNode(m); node.dataset.mid = id;
    if (holder) holder.replaceWith(node); else $("messages").append(node);
    $("welcome").hidden = state.order.length > 0;
    if (atBottom || m.role === "user") stream.scrollTop = stream.scrollHeight;
    refreshInterrupt();
    const badge = $("route-badge");
    if (badge && m.meta && m.meta.route) {
      const route = m.meta.route;
      badge.hidden = false;
      badge.textContent = (route.recommendation || "") + (route.reason ? " · " + route.reason : "");
      badge.title = route.enforcement ? "已强制执行" : "影子评估，不代替批准";
    }
  }
  function streamingNow() {
    return [...state.messages.values()].some(item => item.role === "assistant" && item.status === "streaming");
  }
  function refreshInterrupt() {
    const live = streamingNow();
    const button = $("interrupt");
    if (button) button.hidden = !live;
    const send = $("send");
    if (send && state.canWrite) send.textContent = live ? "发送并打断" : "发送";
  }
  function rerenderLinked(field, id) {
    for (const m of state.messages.values()) if (m.meta && m.meta[field] === id) renderMessage(m.id);
  }

  function showPanel(open) {
    $("panel").hidden = !open;
    $("panel").classList.toggle("open", open);
    $("shell").classList.toggle("panel-open", open);
    $("toggle-panel").setAttribute("aria-expanded", String(open));
  }
  function noticeRunning(task, previous) {
    if (task.status === "running" && previous?.status !== "running") showPanel(true);
  }

  // ---------- live stream ----------
  function apply(type, data) {
    window.dispatchEvent(new CustomEvent("verilo:event", {detail: {type, data}}));
    if (type === "message" || type === "message.updated") {
      upsertMessage(data);
      if (type === "message" && data.role === "assistant" && data.kind !== "text") ping("Verilo", data.text.slice(0, 80));
      if (type === "message.updated" && data.role === "assistant" && data.status === "done") ping("Verilo 回复了", data.text.slice(0, 80));
    } else if (type === "message.delta") {
      const m = state.messages.get(data.id);
      if (m) { m.text = data.text; renderMessage(data.id); }
    } else if (type === "task") {
      const before = state.tasks.get(data.id);
      state.tasks.set(data.id, data); noticeRunning(data, before); rerenderLinked("task_id", data.id); renderPanel();
      if (before && before.status !== data.status && ["awaiting_confirmation", "blocked", "waiting_approval"].includes(data.status))
        ping("任务《" + data.title + "》", STATUS[data.status]);
    } else if (type === "todo") { state.todos.set(data.id, data); renderPanel(); }
    else if (type === "hook") ping(data.title || "Verilo", data.event === "task.completed" ? "任务已完成" : "回复已完成");
    else if (type.startsWith("mail")) { refreshMailSoon(); }
    else if (type === "approval") {
      state.approvals.set(data.id, data); rerenderLinked("approval_id", data.id); renderPanel();
      if (data.status === "pending") ping("需要你批准", data.title);
    }
  }
  async function listen() {
    if (!state.thread) return;
    const controller = new AbortController(); state.streamAbort = controller;
    try {
      const response = await fetch(`/v1/assistant/threads/${encodeURIComponent(state.thread.id)}/stream?after_seq=${state.cursor}`,
        {headers: {Accept: "text/event-stream", "X-Loom-Console": "local"}, credentials: "same-origin", cache: "no-store",
         signal: controller.signal});
      if (!response.ok || !response.body) throw Object.assign(new Error("stream"), {status: response.status});
      state.retry = 1000; setStatusLink(true);
      const reader = response.body.getReader(); const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const {value, done} = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, {stream: true});
        let cut;
        while ((cut = buffer.indexOf("\n\n")) >= 0) {
          const frame = buffer.slice(0, cut); buffer = buffer.slice(cut + 2);
          let id = null, type = "message", data = "";
          for (const line of frame.split("\n")) {
            if (line.startsWith("id:")) id = Number(line.slice(3).trim());
            else if (line.startsWith("event:")) type = line.slice(6).trim();
            else if (line.startsWith("data:")) data += line.slice(5).trim();
          }
          if (id && data) { state.cursor = Math.max(state.cursor, id); try { apply(type, JSON.parse(data)); } catch { /* skip */ } }
        }
      }
    } catch (error) {
      if (controller.signal.aborted) return;
      if (error.status === 401) { notice(explain(error), true); return; }
      setStatusLink(false);
      state.retry = Math.min(state.retry * 2, 15000);
    }
    setTimeout(listen, state.retry);
  }
  function setStatusLink(connected) {
    if (!connected) { $("status").className = "as-status offline"; $("status-text").textContent = "连接中断，正在重连…"; }
  }

  // ---------- panel ----------
  function renderPanel() {
    const pending = [...state.approvals.values()].filter(a => a.status === "pending");
    $("count-approvals").textContent = pending.length ? String(pending.length) : "";
    $("badge").hidden = pending.length === 0;
    const body = $("tab-body"); body.replaceChildren();
    const tasks = [...state.tasks.values()].sort((a, b) => b.updated_at - a.updated_at);
    const empty = (text) => body.append(el("div", "as-empty", text));
    if (state.tab === "active") {
      const items = tasks.filter(t => ["queued", "running", "blocked", "waiting_approval"].includes(t.status));
      if (!items.length) empty("后台没有进行中的工作");
      for (const t of items) {
        const card = taskItem(t); const row = el("div", "row");
        if (t.status === "blocked" && t.kind === "research") row.append(button("重试", "ghost", () => act("POST", `/v1/assistant/tasks/${t.id}/retry`)));
        if (t.kind === "coding") { const a = el("a", "ghost", "打开编程会话"); a.href = "/console/sessions#s=" + encodeURIComponent(t.state.session_id || ""); row.append(a); }
        if (["queued", "blocked", "waiting_approval"].includes(t.status)) row.append(button("取消", "ghost", () => act("POST", `/v1/assistant/tasks/${t.id}/cancel`)));
        if (row.children.length) card.append(row);
        body.append(card);
      }
    } else if (state.tab === "approvals") {
      if (!pending.length) empty("没有等你批准的事项");
      for (const a of pending) {
        const card = el("div", "as-item"); card.append(el("h4", null, a.title), el("p", null, a.detail));
        const row = el("div", "row");
        if (state.canApprove) row.append(button("批准", "primary", () => decide(a, "approved")), button("拒绝", "ghost", () => decide(a, "rejected")));
        card.append(row); body.append(card);
      }
      const waitingCode = tasks.filter(t => t.kind === "coding" && t.status === "waiting_approval");
      for (const t of waitingCode) {
        const card = taskItem(t); const a = el("a", "primary", "去批准实现计划");
        a.href = "/console/sessions#s=" + encodeURIComponent(t.state.session_id || ""); card.append(a); body.append(card);
      }
    } else if (state.tab === "results") {
      const items = tasks.filter(t => ["awaiting_confirmation", "completed"].includes(t.status));
      if (!items.length) empty("还没有成果");
      for (const t of items) {
        const card = taskItem(t);
        if (t.state.report) card.append(el("p", null, t.state.report.summary));
        if (t.status === "awaiting_confirmation" && state.canApprove)
          card.append(button("确认完成", "primary", () => act("POST", `/v1/assistant/tasks/${t.id}/confirm`)));
        body.append(card);
      }
    } else if (state.tab === "todos") {
      const form = el("form", "as-form");
      const input = el("input"); input.type = "text"; input.maxLength = 1000; input.placeholder = "新增待办…";
      const research = el("input"); research.type = "checkbox";
      const label = el("label"); label.append(research, el("span", null, "需要查资料（有授权时巡检会自动开始）"));
      const add = el("button", "primary", "添加"); add.type = "submit";
      form.append(input, label, add);
      form.addEventListener("submit", async (event) => {
        event.preventDefault(); if (!input.value.trim()) return;
        await act("POST", "/v1/assistant/todos", {text: input.value.trim(), research: research.checked}); input.value = "";
      });
      body.append(form);
      const items = [...state.todos.values()].filter(t => ["open", "in_progress"].includes(t.status));
      if (!items.length) empty("没有待办");
      for (const t of items) {
        const card = el("div", "as-item"); card.append(el("h4", null, t.text));
        card.append(el("p", null, (t.status === "in_progress" ? "处理中 · " : "") + (t.due ? "截止 " + t.due + " · " : "") +
                                   ({user: "你记的", assistant: "助手记的", session: "来自编程会话", task: "来自任务"}[t.source] || "")));
        const row = el("div", "row");
        row.append(button("完成", "ghost", () => act("POST", `/v1/assistant/todos/${t.id}`, {status: "done"})),
                   button("忽略", "ghost", () => act("POST", `/v1/assistant/todos/${t.id}`, {status: "dismissed"})));
        card.append(row); body.append(card);
      }
    } else if (state.tab === "mail") renderMail(body);
    else renderSettings(body);
  }

  // ---------- mail ----------
  let mailTimer = null;
  function refreshMailSoon() { clearTimeout(mailTimer); mailTimer = setTimeout(() => refreshMail().catch(() => {}), 300); }
  async function refreshMail() {
    let status;
    // The invited preview never exposes mail; skip the call instead of logging a refused request.
    if (state.me?.authentication === "public-session") status = {configured: false};
    else try { status = await request("GET", "/v1/assistant/mail/status"); } catch { status = {configured: false}; }
    state.mail.status = status;
    $("tab-mail").hidden = !status.configured;
    $("nav-mail").hidden = !status.configured;
    if (!status.configured) return;
    state.mail.items = (await request("GET", "/v1/assistant/mail/messages?limit=100")).items;
    if (state.mail.open) state.mail.detail = await request("GET", `/v1/assistant/mail/messages/${encodeURIComponent(state.mail.open)}`);
    if (state.mail.detail?.draft && $("notice").textContent === "正在起草回复…") notice("");
    if (state.tab === "mail") renderPanel();
  }
  async function openMail(id) {
    state.mail.open = id; state.tab = "mail";
    for (const t of document.querySelectorAll("#panel .as-tabs button")) t.setAttribute("aria-selected", String(t.dataset.tab === "mail"));
    $("panel").classList.add("open");
    await refreshMail();
    renderPanel();
  }
  async function requestDraft(id) {
    try { await request("POST", `/v1/assistant/mail/messages/${encodeURIComponent(id)}/draft`, undefined, key()); notice("正在起草回复…"); }
    catch (error) { notice(explain(error), true); }
    await openMail(id);
  }
  const when = (ts) => ts ? new Date(ts * 1000).toLocaleString("zh-CN", {hour12: false}) : "";
  function renderMail(body) {
    const st = state.mail.status;
    const head = el("div", "as-item");
    head.append(el("h4", null, st.account || "邮箱"),
                el("p", null, (st.status === "ok" ? "已连接" : st.status === "error" ? "同步暂停" : "等待首次同步") +
                              (st.last_sync_at ? " · 最近检查 " + when(st.last_sync_at) : "") + (st.send_enabled ? " · 可发信" : " · 只收不发")));
    if (st.message) head.append(el("p", "warn", st.message));
    const r = el("div", "row"); r.append(button("立即检查新邮件", "ghost", () => act("POST", "/v1/assistant/mail/sync"))); head.append(r);
    body.append(head);
    const d = state.mail.detail;
    if (state.mail.open && d) return renderMailDetail(body, d);
    if (!state.mail.items.length) return body.append(el("div", "as-empty", "还没有邮件"));
    for (const m of state.mail.items) {
      const card = el("div", "as-item clickable");
      const h = el("h4", null, m.header.subject || "（无主题）");
      h.append(el("span", "as-pill " + m.status, MAIL_STATUS[m.status] || m.status)); card.append(h);
      card.append(el("p", null, (m.header.from_name ? m.header.from_name + " " : "") + "<" + m.header.from_address + "> · " + when(m.received_at)));
      if (m.summary) card.append(el("p", null, m.summary.summary));
      card.addEventListener("click", () => openMail(m.id));
      body.append(card);
    }
  }
  function renderMailDetail(body, d) {
    const m = d.mail, h = m.header;
    body.append(button("← 返回邮件列表", "ghost", () => { state.mail.open = null; state.mail.detail = null; renderPanel(); }));
    const card = el("div", "as-item mail-detail");
    card.append(el("h4", null, h.subject || "（无主题）"));
    card.append(el("p", null, "发件人：" + (h.from_name ? h.from_name + " " : "") + "<" + h.from_address + ">" +
                              (h.auth && h.auth.aligned ? " · 发件认证通过" : " · 发件认证未通过或缺失")));
    card.append(el("p", null, "收件人：" + h.to.map(a => a.address).join(", ") + (h.cc.length ? "；抄送：" + h.cc.map(a => a.address).join(", ") : "")));
    card.append(el("p", null, when(m.received_at) + (h.attachments.length ? " · 附件 " + h.attachments.map(a => a.name || a.type).join("、") + "（未打开）" : "")));
    if (m.summary) {
      const s = el("div", "mail-summary"); s.append(el("p", null, "AI 摘要（生成内容，请对照原文）：" + m.summary.summary));
      for (const [label, list] of [["对方诉求", m.summary.asks], ["建议待办", m.summary.todos]])
        if (list && list.length) s.append(el("p", null, label + "：" + list.join("；")));
      card.append(s);
    }
    card.append(el("pre", "mail-text", m.safe_text || "（无正文）"));
    if (h.truncated || h.text_truncated) card.append(el("p", "warn", "邮件过长，只显示了前面部分。"));
    body.append(card);
    const draft = d.draft;
    if (!draft) {
      if (m.draft_state === "requested") body.append(el("div", "as-empty", "正在起草回复…"));
      else if (state.canWrite && m.status !== "filtered") body.append(button(m.draft_state === "failed" ? "重新生成回复" : "生成回复", "primary", () => requestDraft(m.id)));
      return;
    }
    const form = el("form", "as-form draft");
    const field = (label, value, tag = "input") => { const l = el("label"); const i = el(tag); i.value = value; l.append(el("span", null, label), i); form.append(l); return i; };
    const editable = draft.status === "draft";
    form.append(el("p", "as-section-title", "回复草稿 · 第 " + draft.version + " 版 · " +
      ({draft: "可编辑", awaiting_approval: "等你批准发送", queued: "排队发送", sent: "已发送", unknown: "发送结果待核对"}[draft.status] || draft.status)));
    const to = field("收件人（逗号分隔）", draft.to.join(", ")), cc = field("抄送", draft.cc.join(", ")), subject = field("主题", draft.subject);
    const text = field("正文", draft.body, "textarea"); text.rows = 10;
    for (const i of [to, cc, subject, text]) i.disabled = !editable;
    if (draft.placeholders.length) form.append(el("p", "warn", "草稿里有 " + draft.placeholders.join("、") + "，这是脱敏占位符，发送前请改成真实内容。"));
    const split = (v) => v.split(/[,，;；\s]+/).map(x => x.trim()).filter(Boolean);
    const save = async () => request("PUT", `/v1/assistant/mail/drafts/${draft.id}`,
      {to: split(to.value), cc: split(cc.value), subject: subject.value, body: text.value, expected_version: draft.version}, key());
    const row = el("div", "row");
    if (editable && state.canWrite) {
      row.append(button("保存修改", "ghost", async () => { try { await save(); notice("已保存。"); } catch (e) { notice(explain(e), true); } await refreshMail(); }));
      if (state.mail.status.send_enabled) row.append(button("请求发送", "primary", async () => {
        try {
          const changed = text.value !== draft.body || subject.value !== draft.subject || to.value !== draft.to.join(", ") || cc.value !== draft.cc.join(", ");
          const current = changed ? await save() : draft;
          await request("POST", `/v1/assistant/mail/drafts/${current.id}/request-send`, {expected_version: current.version}, key());
          notice("已生成发送审批卡，批准后才会发出。");
        } catch (e) { notice(explain(e), true); }
        await refreshMail(); await refreshSide();
      }));
      else row.append(el("span", "muted", "执行器未开启发信：可复制正文到网页版邮箱发送。"));
      row.append(button("复制正文", "ghost", async () => { try { await navigator.clipboard.writeText(text.value); notice("已复制。"); } catch { notice("复制失败，请手动选择。", true); } }));
    }
    form.append(row);
    for (const o of d.outbox) form.append(el("p", "muted", "发送记录：" + (OUTBOX[o.status] || o.status) + " · " + when(o.updated_at)));
    form.addEventListener("submit", (e) => e.preventDefault());
    body.append(form);
  }
  function taskItem(t) {
    const card = el("div", "as-item");
    const h = el("h4", null, (KIND[t.kind] || t.kind) + "：" + t.title);
    h.append(el("span", "as-pill " + t.status, STATUS[t.status] || t.status));
    if (t.card && t.card.worker_state) h.append(el("span", "as-pill", t.card.worker_state));
    card.append(h, el("p", null, progressText(t) + (t.origin === "proactive" ? " · 授权内自动开始" : "")));
    if (t.card && t.card.human_action) card.append(el("p", null, "需要：" + t.card.human_action));
    return card;
  }
  function renderSettings(body) {
    body.append(el("p", "as-section-title", "常设授权（只能由你本人签发）"));
    const active = state.mandates.filter(m => m.active);
    if (!active.length) body.append(el("div", "as-empty", "未签发授权：主动工作会先问你"));
    for (const m of active) {
      const card = el("div", "as-item");
      card.append(el("h4", null, m.scopes.map(s => ({research: "自动调研", resume: "自动重试"}[s] || s)).join(" + ")),
                  el("p", null, "每日上限 " + usd(m.daily_budget_microusd) + " · 到期 " + new Date(m.expires_at * 1000).toLocaleDateString("zh-CN")));
      card.append(button("撤销", "ghost", () => act("POST", `/v1/assistant/mandates/${m.id}/revoke`)));
      body.append(card);
    }
    if (state.canApprove) {
      const form = el("form", "as-form");
      const scope = (name, text, checked) => { const box = el("input"); box.type = "checkbox"; box.value = name; box.checked = checked;
        const l = el("label"); l.append(box, el("span", null, text)); return [l, box]; };
      const [l1, b1] = scope("research", "待办里需要查资料的，自动开始只读调研", true);
      const [l2, b2] = scope("resume", "因网络/预算暂停的调研，自动重试一次", true);
      const budget = el("input"); budget.type = "number"; budget.min = "0.01"; budget.step = "0.01"; budget.value = "0.50";
      const days = el("input"); days.type = "number"; days.min = "1"; days.max = "90"; days.value = "30";
      const grid = el("div", "inline");
      const lb = el("label"); lb.append(el("span", null, "每日上限 $"), budget);
      const ld = el("label"); ld.append(el("span", null, "天数"), days);
      grid.append(lb, ld);
      const submit = el("button", "primary", "签发授权"); submit.type = "submit";
      form.append(l1, l2, grid, el("p", "muted", "授权外的事、发消息/付款/删除等对外操作不在授权内；编程计划仍需你逐次批准。"), submit);
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        const scopes = [b1, b2].filter(b => b.checked).map(b => b.value);
        if (!scopes.length) return notice("至少选择一项授权范围。", true);
        await act("POST", "/v1/assistant/mandates", {scopes, daily_budget_microusd: Math.round(Number(budget.value) * 1e6), days: Number(days.value)});
      });
      body.append(form);
    }
    const s = state.settings;
    if (s) {
      body.append(el("p", "as-section-title", "巡检与简报"));
      const form = el("form", "as-form");
      const num = (value, min, max) => { const i = el("input"); i.type = "number"; i.min = String(min); i.max = String(max); i.value = String(value); return i; };
      const patrol = num(s.patrol_minutes, 15, 1440), hour = num(s.brief_hour, -1, 23), daily = num((s.daily_budget_microusd / 1e6).toFixed(2), 0, 20);
      daily.step = "0.01";
      for (const [text, input] of [["巡检间隔（分钟）", patrol], ["简报时间（点，-1 关闭）", hour], ["对话与调研每日上限 $", daily]]) {
        const l = el("label"); l.append(el("span", null, text), input); form.append(l);
      }
      const save = el("button", "ghost", "保存设置"); save.type = "submit"; form.append(save);
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        await act("PUT", "/v1/assistant/settings", {patrol_minutes: Number(patrol.value), brief_hour: Number(hour.value),
                                                   daily_budget_microusd: Math.round(Number(daily.value) * 1e6)});
      });
      body.append(form);
    }
    if ("Notification" in window && Notification.permission === "default")
      body.append(button("开启本机提醒（页面在后台时）", "ghost", async () => { await Notification.requestPermission(); renderPanel(); }));
  }
  async function act(method, path, body) {
    try {
      await request(method, path, body, key()); notice("");
      await refreshSide();
    } catch (error) { notice(explain(error), true); }
  }
  async function decide(approval, decision) {
    try {
      const result = await request("POST", `/v1/assistant/approvals/${approval.id}`, {decision}, key());
      notice(decision === "approved" ? (result.session_id ? "已批准，编程会话已创建。" : "已批准，开始处理。") : "已拒绝。");
      await refreshSide();
    } catch (error) { notice(explain(error), true); }
  }
  async function refreshSide() {
    const [tasks, todos, approvals, mandates, settings] = await Promise.all([
      request("GET", "/v1/assistant/tasks"), request("GET", "/v1/assistant/todos"), request("GET", "/v1/assistant/approvals?status=all"),
      request("GET", "/v1/assistant/mandates"), request("GET", "/v1/assistant/settings")]);
    const previousTasks = state.tasks;
    state.tasks = new Map(tasks.items.map(t => [t.id, t]));
    for (const task of tasks.items) noticeRunning(task, previousTasks.get(task.id));
    state.todos = new Map(todos.items.map(t => [t.id, t]));
    state.approvals = new Map(approvals.items.map(a => [a.id, a]));
    state.mandates = mandates.items; state.settings = settings;
    const rt = settings.runtime || {};
    $("status").className = "as-status " + (rt.online ? "online" : "offline");
    // Settings alignment: surface model readiness from the settings hub so a
    // missing API key is visible in the header instead of only after a silent turn.
    let modelNote = "";
    try {
      const o = await request("GET", "/v1/settings/overview");
      if (!o.model || !o.model.configured) modelNote = " · 模型未配置（设置页配置后即可对话）";
      else if (o.model.probe && o.model.probe.state === "error") modelNote = " · 模型探测异常（" + (o.model.probe.code || "未知") + "）";
    } catch { /* settings hub only exists in the local console */ }
    const statusDetail = rt.online ? "执行器在线" + modelNote + (rt.search_provider && rt.search_provider !== "none" ? " · 可联网搜索" : " · 未配置搜索")
                                   : "执行器离线：消息会排队，执行器上线后处理";
    $("status").title = statusDetail;
    $("status").setAttribute("aria-label", statusDetail);
    $("status-text").textContent = rt.online ? (modelNote ? "模型异常" : "在线") : "离线";
    for (const id of state.order) renderMessage(id);
    renderPanel();
  }

  // Text is read locally and travels in the same authenticated message as the
  // question. Binary documents are rejected rather than pretending to parse them.
  const TEXT_FILE = /\.(txt|md|csv|json|jsonl|py|js|ts|tsx|jsx|html|css|yaml|yml|xml|log|sh|ps1|toml|ini|c|h|cpp|hpp|java|go|rs|sql|rb|php|swift|kt|vue|svelte|scala|cs|bat)$/i;
  function renderAttachments() {
    const list = $("attachments"); list.replaceChildren();
    for (const file of state.attachments) {
      const chip = el("div", "as-attachment");
      const name = el("span", null, file.name); name.title = file.name;
      const remove = el("button", null, "×"); remove.type = "button";
      remove.setAttribute("aria-label", "移除 " + file.name);
      remove.addEventListener("click", () => {
        state.attachments = state.attachments.filter(item => item !== file);
        renderAttachments();
      });
      chip.append(name, remove); list.append(chip);
    }
    list.hidden = state.attachments.length === 0;
  }
  async function addFiles(files) {
    if (!state.canWrite) return notice("当前身份只读。", true);
    const incoming = Array.from(files);
    if (incoming.length + state.attachments.length > 3) return notice("每次最多添加 3 个文件。", true);
    const accepted = [];
    try {
      for (const file of incoming) {
        if (!TEXT_FILE.test(file.name)) throw new Error("目前支持文本和代码文件；PDF、Word 与图片暂未接入。");
        if (file.size > 16000) throw new Error("单个文本文件不能超过 16 KB。");
        const text = new TextDecoder("utf-8", {fatal: true}).decode(await file.arrayBuffer());
        if (text.includes("\u0000")) throw new Error("文件不是可读取的 UTF-8 文本。");
        accepted.push({name: file.name.replace(/[\x00-\x1f\x7f]/g, "_").slice(0, 120), text});
      }
      const all = [...state.attachments, ...accepted];
      if (all.reduce((sum, item) => sum + item.text.length, 0) > 3000)
        throw new Error("文件内容合计不能超过 3000 个字符。");
      state.attachments = all; renderAttachments(); notice("");
    } catch (error) { notice(error instanceof TypeError ? "请使用 UTF-8 编码的文本文件。" : error.message, true); }
  }

  // ---------- startup ----------
  async function send(event) {
    event.preventDefault();
    const prompt = $("prompt").value.trim();
    let text = prompt;
    if (state.attachments.length) {
      const contents = state.attachments.map(file => `文件：${file.name}\n${file.text}`).join("\n\n");
      text = (prompt || "请阅读这些文件。") + "\n\n" + contents;
    }
    if (!text || !state.thread) return;
    if (text.length > 4000) return notice("消息与文件内容合计不能超过 4000 个字符。", true);
    const payload = {text, ...(window.VeriloWorkspace?.selections() || {})};
    if (streamingNow()) payload.barge_in = true;
    const fingerprint = JSON.stringify(payload);
    if (!state.pendingMessage || state.pendingMessage.fingerprint !== fingerprint)
      state.pendingMessage = {fingerprint, key: key()};
    $("send").disabled = true;
    try {
      const m = await request("POST", `/v1/assistant/threads/${encodeURIComponent(state.thread.id)}/messages`, payload, state.pendingMessage.key);
      upsertMessage(m); $("prompt").value = ""; state.attachments = []; state.pendingMessage = null;
      renderAttachments(); autoGrow(); notice("");
    } catch (error) { notice(explain(error), true); }
    finally { $("send").disabled = !state.canWrite; }
  }
  function autoGrow() { const t = $("prompt"); t.style.height = "auto"; t.style.height = Math.min(t.scrollHeight, 200) + "px"; }
  async function loadBento() {
    const host = $("bento");
    if (!host) return;
    host.replaceChildren();
    const card = (title, body, href) => {
      const item = el("article");
      item.append(el("strong", null, title), el("p", null, body));
      if (href) { const link = el("a", null, "打开"); link.href = href; item.append(link); }
      return item;
    };
    try {
      const [projects, approvals, automations] = await Promise.all([
        request("GET", "/v1/workspace/projects").catch(() => ({items: []})),
        request("GET", "/v1/assistant/approvals").catch(() => ({items: []})),
        request("GET", "/v1/workspace/automations").catch(() => ({items: []})),
      ]);
      const pending = (approvals.items || []).filter(item => item.status === "pending");
      if (pending.length) host.append(card("待批准", pending.length + " 项", null));
      for (const project of (projects.items || []).slice(0, 3)) host.append(card(project.name, "项目", "/console/projects"));
      for (const automation of (automations.items || []).slice(0, 3)) {
        host.append(card(automation.name, (automation.mode || "observe") + (automation.enabled ? " · 已启用" : " · 未启用"), "/console/automations"));
      }
      if (!host.childElementCount) host.append(card("项目", "还没有项目", "/console/projects"));
    } catch (error) { host.append(card("工作区", error.message || "暂时无法读取", "/console/projects")); }
  }
  async function start() {
    $("composer").addEventListener("submit", send);
    $("interrupt")?.addEventListener("click", async () => {
      if (!state.thread) return;
      const text = $("prompt").value.trim();
      const body = {reason: "user_interrupt", ...(window.VeriloWorkspace?.selections() || {})};
      if (text) body.followup_text = text;
      try {
        await request("POST", `/v1/assistant/threads/${encodeURIComponent(state.thread.id)}/interrupt`, body);
        if (text) { $("prompt").value = ""; autoGrow(); }
        notice("");
      } catch (error) { notice(explain(error), true); }
    });
    $("attach").addEventListener("click", () => $("file-input").click());
    $("file-input").addEventListener("change", async (event) => {
      await addFiles(event.target.files); event.target.value = "";
    });
    for (const type of ["dragover", "drop"]) window.addEventListener(type, event => {
      if (!Array.from(event.dataTransfer?.types || []).includes("Files")) return;
      event.preventDefault();
      document.body.classList.toggle("file-drag", type === "dragover");
      if (type === "drop") addFiles(event.dataTransfer.files);
    });
    window.addEventListener("dragleave", event => { if (!event.relatedTarget) document.body.classList.remove("file-drag"); });
    $("prompt").addEventListener("input", autoGrow);
    $("prompt").addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); $("composer").requestSubmit(); }
    });
    for (const tab of document.querySelectorAll("#panel .as-tabs button")) tab.addEventListener("click", () => {
      state.tab = tab.dataset.tab;
      for (const t of document.querySelectorAll("#panel .as-tabs button")) t.setAttribute("aria-selected", String(t === tab));
      renderPanel();
    });
    for (const link of document.querySelectorAll("[data-open-tab]")) link.addEventListener("click", () => {
      const tab = document.querySelector(`.as-tabs button[data-tab="${link.dataset.openTab}"]`);
      if (tab && !tab.hidden) tab.click();
      showPanel(true);
    });
    $("toggle-panel").addEventListener("click", () => {
      showPanel($("panel").hidden);
    });
    $("close-panel").addEventListener("click", () => {
      showPanel(false);
      $("toggle-panel").focus();
    });
    $("panel-narrow").addEventListener("click", () => {
      const size = Number($("shell").dataset.panelSize || 1);
      $("shell").dataset.panelSize = String(Math.max(0, size - 1));
    });
    $("panel-widen").addEventListener("click", () => {
      const size = Number($("shell").dataset.panelSize || 1);
      $("shell").dataset.panelSize = String(Math.min(2, size + 1));
    });
    $("logout").addEventListener("click", async () => {
      try { await fetch("/preview/logout", {method: "POST", credentials: "same-origin"}); } finally { location.assign("/preview/login"); }
    });
    try {
      state.me = await request("GET", "/v1/me");
      $("logout").hidden = state.me.authentication !== "public-session";
      $("who").textContent = (state.me.authentication === "public-session" ? "体验成员 · " : "本机 · ") + state.me.subject_id; $("who").hidden = false;
      state.canWrite = state.me.capabilities.includes("goal.write");
      state.canApprove = state.me.capabilities.includes("plan.approve") && state.me.roles.includes("user");
      if (!state.canWrite) { $("prompt").disabled = true; $("send").disabled = true; $("attach").disabled = true; $("prompt").placeholder = "当前身份只读"; }
      const data = await request("GET", "/v1/assistant/thread");
      state.thread = data.thread;
      await refreshSide();
      await refreshMail();
      for (const m of data.messages) upsertMessage(m);
      loadBento();
      state.cursor = data.head_seq;
      $("stream").scrollTop = $("stream").scrollHeight;
      listen();
      // The executor writes its heartbeat once it sees this thread; re-check shortly after first load.
      setTimeout(() => refreshSide().catch(() => {}), 2500);
      setInterval(() => refreshSide().catch(() => {}), 30000);
    } catch (error) { notice(explain(error), true); }
  }
  start();
})();
