"use strict";
// Conversation-first workspace. Bearer credentials never enter the page: local
// mode uses the loopback console marker, the public preview an HttpOnly cookie.
(() => {
  const $ = (id) => document.getElementById(id);
  const state = {me: null, sessions: [], current: null, view: null, timer: null, listTimer: null,
                 pendingKeys: new Map(), detailsOpen: false, events: null, busy: false,
                 views: new Map(), open: new Set(), closed: new Set(), snapshot: null, executorWarning: null, canWrite: true};
  const EXAMPLES = [
    "统计英文文本词频：输入 {\"text\":字符串,\"limit\":整数}，输出出现最多的单词及次数",
    "校验中国大陆手机号：输入 {\"phone\":字符串}，输出 {\"valid\":布尔,\"carrier\":运营商或null}",
    "计算购物车总价：输入商品单价、数量和满减规则，输出应付金额",
  ];
  const RESTART_CODES = new Set(["ACCEPTANCE_FAILED", "REPAIR_ATTEMPTS_EXHAUSTED", "REPEATED_SOURCE", "TOO_FEW_CASES",
    "PUBLIC_CHECKS_INVALID", "MODEL_SOURCE_INVALID", "MODEL_OUTPUT_TRUNCATED", "BUDGET_EXHAUSTED", "budget_insufficient"]);
  const STATE_NAMES = {running: "自动推进中", needs_attention: "需要你处理", paused: "已暂停", awaiting_approval: "等你批准",
                       delivered: "已交付", verified: "已独立复核", terminated: "已终止"};
  const STEP_NAMES = {submitted: "已完成", verified: "已复核", running: "进行中", queued: "排队中",
                      waiting: "等待中", attention: "需处理", pending: "待复核", done: "已复核", not_configured: "未配置",
                      rejected: "未通过", disputed: "结论不一致", blocked: "已拦截", unavailable: "不可用",
                      awaiting_approval: "等你批准"};

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = String(text);
    return e;
  }
  function key() {
    return (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2));
  }
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
    if (error.status === 401) return state.me?.authentication === "public-session" || location.protocol === "https:"
      ? "登录已过期，请重新打开体验链接。" : "本机工作台入口未启用，请用项目启动器启动服务。";
    if (error.status === 403) return "当前身份没有这个操作的权限。";
    if (error.status === 409) return "操作被拒绝：状态已变化或额度已满。已刷新，请核对后再试。";
    if (error.status === 422) return "输入不符合要求（需求最多 4000 字）。";
    if (error.status === 429) return "请求太频繁，请稍后再试。";
    return "暂时无法连接 Loom 服务，请稍后重试。";
  }
  function relTime(seconds) {
    if (!seconds) return "";
    const diff = Date.now() / 1000 - seconds;
    if (diff < 60) return "刚刚";
    if (diff < 3600) return Math.floor(diff / 60) + " 分钟前";
    if (diff < 86400) return Math.floor(diff / 3600) + " 小时前";
    return new Date(seconds * 1000).toLocaleDateString("zh-CN");
  }
  const usd = (micro) => Number.isFinite(micro) ? "$" + (micro / 1e6).toFixed(micro < 10000 ? 4 : 2) : "—";
  const json = (value) => JSON.stringify(value);

  // ---------- sidebar ----------
  function renderList() {
    const list = $("session-list");
    list.replaceChildren();
    if (!state.sessions.length) { list.append(el("div", "empty-list", "还没有会话")); return; }
    for (const item of state.sessions) {
      const button = el("button", "session-item");
      button.type = "button";
      button.setAttribute("aria-current", String(item.thread_id === state.view?.thread_id || item.session_id === state.current));
      button.append(el("span", "dot " + item.state), el("span", "t", item.title));
      if (item.turns > 1) button.append(el("span", "turns", item.turns + " 轮"));
      button.title = (STATE_NAMES[item.state] || item.state) + " · " + relTime(item.updated_at);
      button.addEventListener("click", () => { select(item.session_id); closeSidebar(); });
      list.append(button);
    }
  }
  async function loadList() {
    try {
      state.sessions = (await request("GET", "/v1/sessions")).items;
      renderList();
    } catch (error) { if (error.status === 401) handleUnauthorized(); }
  }

  // ---------- conversation ----------
  function stepIcon(status) {
    if (status === "submitted" || status === "verified" || status === "done") return el("span", "icon done", "✓");
    if (status === "running") return el("span", "icon running spin");
    if (["attention", "rejected", "disputed", "blocked", "unavailable"].includes(status)) return el("span", "icon attention", "!");
    if (status === "not_configured") return el("span", "icon off", "–");
    return el("span", "icon waiting");
  }
  function stepCard(step, summary, body, open = false) {
    const card = el("details", "step");
    if (open) card.open = true;
    const head = el("summary");
    head.append(stepIcon(step.status), el("span", "label", step.label), el("span", "sub", summary || STEP_NAMES[step.status] || ""),
                el("span", "chev", "›"));
    card.append(head);
    if (body) { const b = el("div", "step-body"); b.append(body); card.append(b); }
    return card;
  }
  function requirementsBody(content) {
    if (!content) return null;
    const box = el("div");
    if (content.summary) box.append(el("p", null, content.summary));
    if (Array.isArray(content.items) && content.items.length) {
      const ul = el("ul", "plain");
      for (const item of content.items) ul.append(el("li", null, item.text || json(item)));
      box.append(ul);
    }
    if (Array.isArray(content.uncertainties) && content.uncertainties.length) {
      box.append(el("p", "note", "未决事项："));
      const ul = el("ul", "plain");
      for (const item of content.uncertainties) ul.append(el("li", null, typeof item === "string" ? item : (item.text || json(item))));
      box.append(ul);
    }
    return box;
  }
  function checksBody(step) {
    const cases = step.content?.cases;
    if (!Array.isArray(cases)) return null;
    const held = new Set(step.split?.heldout_names || []);
    const box = el("div");
    const table = el("table", "case-table");
    const head = el("tr");
    for (const name of ["用例", "输入", "期望输出", ""]) head.append(el("th", null, name));
    table.append(head);
    for (const item of cases) {
      const row = el("tr");
      // Held-out inputs/answers stay private to the executor; the server sends only the name.
      const input = el("td"); input.append(item.hidden ? el("span", "muted", "不公开") : el("code", null, json(item.input)));
      const expected = el("td"); expected.append(item.hidden ? el("span", "muted", "不公开") : el("code", null, json(item.expected)));
      const tagCell = el("td");
      tagCell.append(el("span", held.has(item.name) ? "tag held" : "tag", held.has(item.name) ? "留出验收" : "开发检查"));
      row.append(el("td", null, item.name), input, expected, tagCell);
      table.append(row);
    }
    box.append(table);
    if (step.split) box.append(el("p", "note", `留出验收用例由测试角色模型生成，实现者看不到；通过它不等于独立双模型复核。`));
    const mine = state.renderingView?.acceptance_examples || [];
    if (mine.length) {
      box.append(el("p", "note", `另有你提供的 ${mine.length} 个验收样例，同样不提供给任何模型：`));
      const list = el("ul", "user-examples");
      for (const item of mine) { const li = el("li"); li.append(el("code", null, `${json(item.input)} → ${json(item.expected)}`)); list.append(li); }
      box.append(list);
    }
    return box;
  }
  function implementBody(step, view) {
    const box = el("div");
    if (step.attempts?.length) {
      const list = el("div", "attempts");
      for (const a of step.attempts) {
        const row = el("div", "attempt");
        if (a.phase === "stopped") {
          row.append(el("span", "tag fail", "停止"), el("span", null, a.message || a.code));
        } else {
          const pass = a.passed === true;
          row.append(el("span", pass ? "tag pass" : "tag fail", pass ? "通过" : "未通过"),
                     el("span", null, `${a.phase === "development" ? "公开检查" : "留出验收"} ${a.passed_cases ?? "?"}/${a.total ?? "?"}` +
                                       (a.epoch > 1 ? ` · 第 ${a.epoch} 版` : "")));
        }
        list.append(row);
      }
      box.append(list);
    }
    if (step.source) {
      const bar = el("div", "code-bar");
      bar.append(el("span", null, "main.py"));
      const actions = el("div", "actions");
      const copy = el("button", "ghost", "复制"); copy.type = "button";
      copy.addEventListener("click", async () => {
        try { await navigator.clipboard.writeText(step.source); copy.textContent = "已复制"; setTimeout(() => copy.textContent = "复制", 1500); }
        catch { notice("浏览器不允许复制，请手动选择代码。", true); }
      });
      const download = el("button", "ghost", "下载"); download.type = "button";
      download.addEventListener("click", () => {
        const url = URL.createObjectURL(new Blob([step.source], {type: "text/x-python"}));
        const link = document.createElement("a"); link.href = url; link.download = "main.py"; link.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      });
      actions.append(copy, download); bar.append(actions);
      box.append(bar, el("pre", "code", step.source));
    }
    if (!step.attempts?.length && !step.source) return null;
    return box;
  }
  function summaryFor(step) {
    if (step.key === "requirements" && step.content?.summary) return step.content.summary;
    if (step.key === "checks" && step.content?.cases) {
      const split = step.split ? ` · ${step.split.development} 个开发检查 / ${step.split.heldout} 个留出验收` +
        (step.split.user_examples ? ` + 你的 ${step.split.user_examples} 个样例` : "") : "";
      return `${step.content.cases.length} 个用例${split}`;
    }
    if (step.key === "implement") {
      const last = [...(step.attempts || [])].reverse().find(a => a.phase === "acceptance");
      if (last) return last.passed ? `留出验收 ${last.passed_cases}/${last.total} 通过` : `留出验收 ${last.passed_cases}/${last.total}`;
      if (step.status === "running") return "正在生成代码并在隔离环境运行公开检查";
    }
    if (step.key === "verify") {
      if (step.status === "not_configured") return "本实例未配置独立复核；结果标注为未独立复核";
      if (step.status === "pending") return "等待申请独立复核";
      if (step.status === "queued" || step.status === "running") return "Flash 与 Pro 正在分别复核";
      if (step.status === "rejected") return "复核未通过，程序不会记为完成";
      if (step.status === "disputed") return "两个复核模型结论不一致，需要人工裁决";
      if (step.status === "blocked") return "复核被拦截，不会自动重试";
      if (step.status === "unavailable") return "执行器无法加载复核配置";
    }
    return null;
  }
  function elapsed(seconds) { return seconds < 90 ? seconds + " 秒" : Math.round(seconds / 60) + " 分钟"; }
  function tickActivity() {
    for (const node of document.querySelectorAll(".activity-text")) {
      const seconds = Math.max(0, Math.round(Date.now() / 1000 - Number(node.dataset.since)));
      node.textContent = `${node.dataset.label} · ${elapsed(seconds)}`;
    }
  }
  function turn(view, last) {
    const frag = document.createDocumentFragment();
    frag.append(el("div", "msg-user", view.request || view.prompt));
    const agent = el("div", "msg-agent");
    const body = el("div", "agent-body");
    if (last) {
      body.append(el("p", "lead", view.topology === "direct" ? "你给的样例已经说清了行为，我会直接实现，并用你的全部样例验收（其中一部分不给实现者看）。"
        : view.parent_session_id ? "收到，我会参考上一版程序重新分析、测试并实现。" : "我会依次分析需求、设计测试、编写程序并在隔离环境验收。"));
      if (view.state === "running" && view.activity) {
        const line = el("p", "activity");
        const seconds = Math.max(0, Math.round(Date.now() / 1000 - view.activity.since));
        const text = el("span", "activity-text", `${view.activity.text} · ${elapsed(seconds)}`);
        text.dataset.since = String(view.activity.since); text.dataset.label = view.activity.text;
        line.append(el("span", "dot running"), text);
        body.append(line);
        if (state.executorWarning) body.append(el("p", "activity warn-text", state.executorWarning));
      }
    }
    state.renderingView = view;
    for (const step of view.steps) {
      let content = null;
      if (step.key === "requirements") content = requirementsBody(step.content);
      if (step.key === "checks") content = checksBody(step);
      if (step.key === "implement") content = implementBody(step, view);
      const openKey = view.session_id + ":" + step.key;
      const card = stepCard(step, summaryFor(step), content, state.open.has(openKey) || (last && step.key === "implement" && !!step.source && !state.closed.has(openKey)));
      card.dataset.key = openKey;
      // Remember only what the user opened or closed, not the default expansion.
      card.querySelector("summary").addEventListener("click", () => {
        if (card.open) { state.open.delete(openKey); state.closed.add(openKey); } else { state.open.add(openKey); state.closed.delete(openKey); }
      });
      body.append(card);
    }
    if (!last) {
      const done = view.state === "delivered" || view.state === "verified";
      body.append(el("p", "note", done ? "这一轮已交付。" : view.attention ? "这一轮已停止：" + view.attention.message : "这一轮：" + (STATE_NAMES[view.state] || view.state)));
    } else if (view.state === "needs_attention" && view.attention) {
      const call = el("div", "callout warn");
      call.append(el("h3", null, "需要你处理"), el("p", null, view.attention.message));
      const row = el("div", "row");
      const expert = el("a", "ghost", "在项目空间查看证据");
      expert.href = "/console/expert";
      if (RESTART_CODES.has(view.attention.code)) {
        // Resuming cannot change a failed attempt; a follow-up turn can.
        const again = el("button", "primary", "补充说明并重试"); again.type = "button";
        again.addEventListener("click", () => { $("prompt").focus(); notice("在下方写下补充说明（例如边界情况或输出格式），发送后会参考上一版重新实现。"); });
        row.append(again, expert);
      } else {
        const resume = el("button", "primary", "已处理，继续自动推进"); resume.type = "button";
        resume.addEventListener("click", () => resumeSession(view));
        row.append(resume, expert);
      }
      call.append(row);
      body.append(call);
    } else if (view.state === "awaiting_approval" && view.approval) {
      // Approval is the person's own decision; the system never approves on their behalf.
      const a = view.approval;
      const call = el("div", "callout approve");
      const facts = [];
      if (a.development_cases !== null) facts.push(`${a.development_cases} 个开发检查给实现者`);
      if (a.heldout_cases !== null) facts.push(`${a.heldout_cases} 个留出验收不给实现者看`);
      if (a.user_examples) facts.push(`另含你的 ${a.user_examples} 个样例`);
      facts.push(`预算上限 ${usd(a.cost_limit_microusd)}，最多尝试 ${a.max_attempts} 次`);
      call.append(el("h3", null, "请批准实现计划"), el("p", null, a.message), el("p", "approval-facts", facts.join(" · ")));
      const row = el("div", "row");
      const approve = el("button", "primary", "批准并开始编写"); approve.type = "button";
      approve.addEventListener("click", () => approveSession(view, approve));
      const expert = el("a", "ghost", "在项目空间查看完整计划"); expert.href = "/console/expert";
      row.append(approve, expert); call.append(row); body.append(call);
    } else if (view.state === "delivered" || view.state === "verified") {
      const impl = view.steps.find(s => s.key === "implement");
      const lastRun = [...(impl?.attempts || [])].reverse().find(a => a.phase === "acceptance");
      const call = el("div", "callout ok");
      call.append(el("h3", null, view.state === "verified" ? "已完成并通过独立复核" : "程序已交付"),
                 el("p", null, (lastRun ? `通过留出验收 ${lastRun.passed_cases}/${lastRun.total}。` : "") +
                     (view.state === "verified" ? "" : "尚未经过独立双模型复核，请按需自行检查后再使用。") +
                     ` 本轮费用 ${usd(view.spend.cost_microusd)}，${(view.spend.tokens || 0).toLocaleString("zh-CN")} 词元。需要修改可以直接在下方继续说。`));
      body.append(call);
    } else if (view.state === "paused") {
      body.append(el("p", "lead", "已暂停。点击右上角“继续”恢复。"));
    } else if (view.state === "terminated") {
      body.append(el("p", "lead", "会话已终止。"));
    } else if (!view.executor_configured) {
      body.append(el("p", "lead", "本机执行器未配置，任务会停在排队状态。"));
    }
    agent.append(el("div", "avatar", "L"), body);
    frag.append(agent);
    return frag;
  }
  function render(views) {
    const view = views[views.length - 1];
    state.view = view;
    $("welcome").hidden = true;
    const box = $("conversation"), stream = $("stream"), top = stream.scrollTop;
    box.hidden = false;
    box.replaceChildren();
    views.forEach((v, i) => box.append(turn(v, i === views.length - 1)));
    stream.scrollTop = top;
    $("session-title").textContent = view.title;
    const badge = $("session-state");
    badge.hidden = false; badge.className = "state " + view.state; badge.textContent = STATE_NAMES[view.state] || view.state;
    $("pause").hidden = view.state !== "running";
    $("continue").hidden = view.state !== "paused";
    $("toggle-details").hidden = false;
    updateComposer();
    renderDetails();
  }
  function updateComposer() {
    const view = state.view, prompt = $("prompt");
    if (!state.canWrite) return;
    const busy = view && (view.state === "running" || view.state === "paused" || view.state === "awaiting_approval");
    prompt.disabled = !!busy; $("send").disabled = !!busy; $("toggle-examples").disabled = !!busy;
    prompt.placeholder = !view ? "描述你想要的程序…" : busy ? (view.state === "awaiting_approval" ? "请先批准上方的实现计划" : "本轮完成后可继续补充") : "继续补充或修改要求…（会参考上一版程序）";
  }
  function scrollToEnd() { const s = $("stream"); s.scrollTop = s.scrollHeight; }

  async function checkExecutor(view) {
    state.executorWarning = null;
    if (view.state !== "running" || !view.activity || !view.activity.text.startsWith("已排队")) return;
    if (Date.now() / 1000 - view.activity.since < 15) return;
    try {
      const status = await request("GET", "/v1/executor-status");
      if (status.state !== "recent") state.executorWarning = "本机执行器似乎没有在运行（未收到近期心跳）。启动执行器后会自动继续，不需要重新提交。";
    } catch { /* status is advisory only */ }
  }
  async function refresh(scroll = false) {
    if (!state.current) return;
    const id = state.current;
    try {
      const view = await request("GET", "/v1/sessions/" + encodeURIComponent(id));
      if (state.current !== id) return;
      state.views.set(id, view);
      for (const other of view.thread) {
        const cached = state.views.get(other);
        if (other !== id && (!cached || !["delivered", "verified", "terminated"].includes(cached.state))) {
          state.views.set(other, await request("GET", "/v1/sessions/" + encodeURIComponent(other)));
        }
      }
      await checkExecutor(view);
      if (state.current !== id) return;
      const views = view.thread.map(t => state.views.get(t)).filter(Boolean);
      const snapshot = json(views) + (state.executorWarning || "");
      // Re-render only on real changes so selections and copy buttons survive polling.
      const changed = snapshot !== state.snapshot;
      state.snapshot = snapshot;
      if (changed) { render(views); if (scroll) scrollToEnd(); if (state.detailsOpen) loadEvents(); }
      schedule(view.state === "running" ? 2000 : 8000);
      const item = state.sessions.find(s => s.thread_id === view.thread_id);
      if (!item || item.state !== view.state || item.session_id !== view.session_id) loadList();
    } catch (error) {
      if (error.status === 401) return handleUnauthorized();
      if (error.status === 404) { notice("会话不存在或不属于当前身份。", true); return newSession(); }
      notice(explain(error), true);
      schedule(5000);
    }
  }
  function schedule(ms) { clearTimeout(state.timer); state.timer = setTimeout(() => refresh(false), ms); }

  function select(id) {
    if (state.current === id && state.view) return;
    state.current = id; state.view = null; state.events = null; state.snapshot = null; state.executorWarning = null;
    history.replaceState(null, "", "#s=" + encodeURIComponent(id));
    notice("");
    renderList();
    $("conversation").replaceChildren(); $("welcome").hidden = true; $("conversation").hidden = false;
    refresh(true);
  }
  function newSession() {
    clearTimeout(state.timer);
    state.current = null; state.view = null; state.events = null; state.snapshot = null;
    history.replaceState(null, "", "#");
    $("conversation").hidden = true; $("welcome").hidden = false;
    $("session-title").textContent = "新会话"; $("session-state").hidden = true;
    for (const id of ["pause", "continue", "toggle-details"]) $(id).hidden = true;
    setDetails(false);
    updateComposer();
    renderList();
    $("prompt").focus();
  }

  async function submit(event) {
    event.preventDefault();
    const prompt = $("prompt").value.trim();
    if (!prompt || state.busy) return;
    const parent = state.view && !["running", "paused"].includes(state.view.state) ? state.view.session_id : null;
    let examples;
    try { examples = parseExamples($("examples-input").value); }
    catch (error) { notice(error.message, true); $("examples-box").hidden = false; return; }
    const body = parent ? {prompt, parent_session_id: parent} : {prompt};
    if (examples.length) body.examples = examples;
    // Same text after a lost response reuses its key, so no duplicate session.
    const pendingKey = (parent || "") + "\n" + prompt + "\n" + json(examples);
    if (!state.pendingKeys.has(pendingKey)) state.pendingKeys.set(pendingKey, key());
    state.busy = true; $("send").disabled = true;
    try {
      const view = await request("POST", "/v1/sessions", body, state.pendingKeys.get(pendingKey));
      state.pendingKeys.delete(pendingKey);
      $("prompt").value = ""; autoGrow(); notice("");
      $("examples-input").value = ""; setExamplesOpen(false);
      state.views.set(view.session_id, view);
      await loadList();
      state.current = null;
      select(view.session_id);
    } catch (error) {
      if (error.status === 401) return handleUnauthorized();
      notice(error.status === 409 ? "暂时无法发送：上一轮还在进行，或体验空间的目标/预算额度已满。" : explain(error), true);
    } finally { state.busy = false; updateComposer(); if (!state.view) $("send").disabled = false; }
  }

  async function control(action) {
    const view = state.view; if (!view) return;
    try {
      await request("POST", `/v1/runs/${encodeURIComponent(view.current_run_id)}/control/${action}`,
                    {reason: action === "pause" ? "用户在会话中暂停" : "用户在会话中继续"}, key());
      notice("");
    } catch (error) { notice(explain(error), true); }
    state.view = null; refresh();
  }
  async function approveSession(view, button) {
    if (button) button.disabled = true;
    try {
      await request("POST", `/v1/sessions/${encodeURIComponent(view.session_id)}/approve`, {expected_digest: view.approval.digest}, key());
      notice("已批准，开始编写程序。");
    } catch (error) { notice(explain(error), true); if (button) button.disabled = false; }
    state.view = null; refresh();
  }
  async function resumeSession(view) {
    try {
      await request("POST", `/v1/sessions/${encodeURIComponent(view.session_id)}/resume`, {reason: "用户已查看并确认继续自动推进"}, key());
      notice("已继续。如果原因未解决，自动推进会再次停下。");
    } catch (error) { notice(explain(error), true); }
    state.view = null; refresh();
  }

  // ---------- details ----------
  function setDetails(open) {
    state.detailsOpen = open;
    $("details").hidden = !open;
    $("shell").classList.toggle("with-details", open);
    $("toggle-details").setAttribute("aria-expanded", String(open));
    if (open) { renderDetails(); loadEvents(); }
  }
  async function loadEvents() {
    const view = state.view; if (!view) return;
    try {
      const data = await request("GET", `/v1/runs/${encodeURIComponent(view.current_run_id)}/events`);
      state.events = data.events.slice(-40).reverse();
      renderDetails();
    } catch (error) { state.events = null; if (error.status === 401) handleUnauthorized(); }
  }
  function renderDetails() {
    if (!state.detailsOpen) return;
    const view = state.view; const body = $("details-body");
    body.replaceChildren();
    if (!view) return;
    const kv = el("dl", "kv");
    const add = (k, v) => { kv.append(el("dt", null, k), el("dd", null, v)); };
    add("状态", STATE_NAMES[view.state] || view.state);
    add("费用", `${usd(view.spend.cost_microusd)}（上限 ${usd(view.spend.cost_limit_microusd)}）`);
    add("预留中", usd(view.spend.reserved_microusd));
    add("词元", (view.spend.tokens || 0).toLocaleString("zh-CN"));
    add("需求运行", view.runs.spec);
    if (view.runs.build) add("实现运行", view.runs.build);
    add("验收来源", "测试角色生成的留出用例");
    add("独立复核", view.independently_verified ? "已通过" : "未完成");
    body.append(kv);
    body.append(el("h3", null, "最近事件（当前运行）"));
    if (!state.events) { body.append(el("p", "muted", "加载中…")); return; }
    const list = el("ol", "events");
    for (const e of state.events) {
      const li = el("li");
      li.append(el("span", "seq", "#" + e.seq), el("span", "ty", e.type + (e.actor ? " · " + e.actor : "")));
      list.append(li);
    }
    body.append(list);
    body.append(el("p", "note", "完整计划、证据、人工待办与审计链在“项目空间”。"));
  }

  // ---------- shell ----------
  function openSidebar() { $("shell").classList.add("sidebar-open"); $("scrim").hidden = false; }
  function closeSidebar() { $("shell").classList.remove("sidebar-open"); $("scrim").hidden = true; }
  // One example per line: `<input JSON> => <expected JSON>`; "=>" inside strings is tolerated.
  function parseExamples(text) {
    const lines = text.split("\n").map(l => l.trim()).filter(Boolean);
    if (lines.length > 10) throw new Error("验收样例最多 10 个。");
    return lines.map((line, index) => {
      let at = line.indexOf("=>");
      while (at !== -1) {
        try { return {input: JSON.parse(line.slice(0, at)), expected: JSON.parse(line.slice(at + 2))}; }
        catch { at = line.indexOf("=>", at + 2); }
      }
      throw new Error(`第 ${index + 1} 个验收样例格式不对，应为：输入 JSON => 期望输出 JSON`);
    });
  }
  function setExamplesOpen(open) {
    $("examples-box").hidden = !open;
    $("toggle-examples").setAttribute("aria-expanded", String(open));
  }
  function autoGrow() { const t = $("prompt"); t.style.height = "auto"; t.style.height = Math.min(t.scrollHeight, 200) + "px"; }
  function handleUnauthorized() {
    if (location.protocol === "https:") { location.assign("/preview/login"); return; }
    notice(explain({status: 401}), true);
  }

  async function start() {
    for (const text of EXAMPLES) {
      const b = el("button", "example", text.length > 28 ? text.slice(0, 28) + "…" : text);
      b.type = "button"; b.title = text;
      b.addEventListener("click", () => { $("prompt").value = text; autoGrow(); $("prompt").focus(); });
      $("examples").append(b);
    }
    $("composer").addEventListener("submit", submit);
    $("toggle-examples").addEventListener("click", () => {
      const open = $("examples-box").hidden; setExamplesOpen(open); if (open) $("examples-input").focus();
    });
    $("prompt").addEventListener("input", autoGrow);
    $("prompt").addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("composer").requestSubmit(); }
    });
    $("new-session").addEventListener("click", () => { newSession(); closeSidebar(); });
    $("pause").addEventListener("click", () => control("pause"));
    $("continue").addEventListener("click", () => control("resume"));
    $("toggle-details").addEventListener("click", () => setDetails(!state.detailsOpen));
    $("close-details").addEventListener("click", () => setDetails(false));
    $("open-sidebar").addEventListener("click", openSidebar);
    $("close-sidebar").addEventListener("click", closeSidebar);
    $("scrim").addEventListener("click", closeSidebar);
    $("logout").addEventListener("click", async () => {
      try { await fetch("/preview/logout", {method: "POST", headers: {"Content-Type": "application/json"}, body: "{}"}); } catch {}
      location.assign("/preview/login");
    });
    try {
      state.me = await request("GET", "/v1/me");
    } catch (error) {
      if (error.status === 401) return handleUnauthorized();
      notice(explain(error), true); return;
    }
    const preview = state.me.authentication === "public-session";
    $("identity").textContent = (preview ? "公网体验 · " : "本机 · ") + state.me.subject_id;
    $("logout").hidden = !preview;
    if (!state.me.capabilities.includes("control")) {
      state.canWrite = false;
      $("prompt").disabled = true; $("send").disabled = true;
      $("prompt").placeholder = "当前身份只读，不能新建自动会话";
    }
    await loadList();
    const match = location.hash.match(/^#s=([A-Za-z0-9-]+)$/);
    if (match) select(decodeURIComponent(match[1])); else newSession();
    state.listTimer = setInterval(loadList, 15000);
    setInterval(tickActivity, 1000);
    window.addEventListener("hashchange", () => {
      const next = location.hash.match(/^#s=([A-Za-z0-9-]+)$/);
      if (next && decodeURIComponent(next[1]) !== state.current) select(decodeURIComponent(next[1]));
    });
  }
  document.addEventListener("DOMContentLoaded", start);
})();
