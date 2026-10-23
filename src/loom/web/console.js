"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const state = {
    token: "", principal: null, goals: [], interventions: [], runId: null,
    goal: null, snapshot: null, plan: null, review: null, tab: "tasks",
    busy: false, session: 0, selection: 0, controllers: new Set(), keys: new Map(),
  };
  const statusNames = {
    draft: "草案", open: "运行开放", paused: "已暂停", awaiting_human: "待人工处理",
    pending: "等待执行", running: "执行中", submitted: "待独立复核", done: "已验证",
    cancelled: "已终止", succeeded: "已完成", failed: "失败", stale: "已失效",
    indeterminate: "结果未知", intent: "已登记发起", partial: "部分完成", approved: "已批准",
    settled: "已结算", reserved: "已预留", overrun: "超出预留", not_sent: "确认未发送",
  };
  const roleNames = {operator: "操作员", worker: "执行器", viewer: "只读", developer: "旧开发身份"};
  const unknownStates = new Set(["intent", "indeterminate", "partial"]);
  const terminalStates = new Set(["cancelled", "succeeded", "failed"]);
  const has = (capability) => Boolean(state.principal?.capabilities?.includes(capability));
  const str = (value, fallback = "—") => value === null || value === undefined ? fallback : String(value);
  const integer = (value) => Number.isSafeInteger(value) && value >= 0;
  const amount = (value) => integer(value) ? `${value.toLocaleString("zh-CN")} µUSD` : "—";
  const count = (value) => integer(value) ? value.toLocaleString("zh-CN") : "—";
  const pathForRun = () => `/v1/runs/${encodeURIComponent(state.runId)}`;
  const pathForGoal = () => `/v1/goals/${encodeURIComponent(state.runId)}`;

  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = str(text, "");
    return element;
  }

  function badge(value) {
    const kind = ["running", "open", "done", "approved"].includes(value) ? " active-state"
      : ["awaiting_human", "indeterminate", "partial", "submitted", "paused"].includes(value) ? " attention-state"
      : terminalStates.has(value) ? " end-state" : "";
    return node("span", `badge${kind}`, statusNames[value] || str(value));
  }

  function notify(message, error = false) {
    $("notice").textContent = message;
    $("notice").className = error ? "notice error" : "notice";
    $("notice").hidden = !message;
    $("notice").setAttribute("role", error ? "alert" : "status");
  }

  function publicError(error) {
    if (error.status === 401) return "访问凭据无效或已撤销，请重新登录。";
    if (error.status === 403) return "当前身份没有执行此操作的权限。";
    if (error.status === 404) return "目标已不存在，或不属于当前身份可访问的范围。";
    if (error.status === 409) return "操作被拒绝：运行状态、计划版本或核对条件已改变。可点击工作台“刷新”核对；同页面重提相同内容沿用原幂等键。退出或重载页面后须先核对列表，再决定是否重发。";
    if (error.status === 422) return "提交内容未满足接口约束，请检查必填项、整数上限和计划结构。";
    if (error.code === "RESPONSE_TOO_LARGE") return "响应超过页面读取上限，请使用本地接口按需查看记录。";
    if (error.code === "BAD_JSON") return "服务返回了无法解析的内容，请确认本地服务版本。";
    return "连接中断或请求超时，结果尚未确认。同页面内重提相同内容会沿用原幂等键；退出或重载页面会丢失该键，须先核对目标与事件列表，再决定是否重发。请勿盲目重试。";
  }

  async function request(method, path, body, key) {
    const controller = new AbortController();
    const session = state.session;
    state.controllers.add(controller);
    const timer = setTimeout(() => controller.abort(), 15000);
    try {
      const headers = {Accept: "application/json", Authorization: `Bearer ${state.token}`};
      if (body !== undefined) headers["Content-Type"] = "application/json";
      if (key) headers["Idempotency-Key"] = key;
      const response = await fetch(path, {
        method, headers, body: body === undefined ? undefined : JSON.stringify(body),
        cache: "no-store", credentials: "omit", redirect: "error", signal: controller.signal,
      });
      if (session !== state.session) throw Object.assign(new Error(), {code: "SESSION_CHANGED"});
      if (!response.ok) throw Object.assign(new Error(), {status: response.status});
      const declared = Number(response.headers.get("content-length"));
      if (Number.isFinite(declared) && declared > 2097152) throw Object.assign(new Error(), {code: "RESPONSE_TOO_LARGE"});
      if (!response.body) throw Object.assign(new Error(), {code: "BAD_JSON"});
      const reader = response.body.getReader(), chunks = [];
      let bytes = 0;
      while (true) {
        const {done, value} = await reader.read();
        if (done) break;
        bytes += value.byteLength;
        if (bytes > 2097152) {
          await reader.cancel();
          throw Object.assign(new Error(), {code: "RESPONSE_TOO_LARGE"});
        }
        chunks.push(value);
      }
      const raw = new Uint8Array(bytes);
      let offset = 0;
      for (const chunk of chunks) { raw.set(chunk, offset); offset += chunk.byteLength; }
      try { return JSON.parse(new TextDecoder("utf-8", {fatal: true}).decode(raw)); }
      catch { throw Object.assign(new Error(), {code: "BAD_JSON"}); }
    } finally {
      clearTimeout(timer);
      state.controllers.delete(controller);
    }
  }

  function refreshControls() {
    for (const element of document.querySelectorAll("[data-cap]")) {
      element.disabled = state.busy || !has(element.dataset.cap);
    }
    const status = state.snapshot?.status;
    const selected = Boolean(state.runId && state.snapshot);
    const approved = state.plan?.status === "approved";
    $("control-fields").hidden = !selected || !has("control");
    $("action-state").textContent = !selected ? "选择目标后可查看可用操作。"
      : !has("control") ? "此身份可查看运行，人工控制需要具名操作员权限。"
      : terminalStates.has(status) ? "运行已经终止或完成，不能再恢复执行。"
      : status === "draft" ? "请先保存并批准草案计划。"
      : status === "submitted" ? "候选等待独立复核，人工控制不会替代验收。"
      : "所有控制操作都会写入台账，并保留操作理由。";
    $("pause-button").disabled = state.busy || !has("control") || status !== "open";
    $("resume-button").disabled = state.busy || !has("control") || !["paused", "awaiting_human"].includes(status);
    $("terminate-button").disabled = state.busy || !has("control") || !selected || terminalStates.has(status);
    $("save-plan-button").disabled = state.busy || !has("goal.write") || !state.plan || approved || status !== "draft";
    $("plan-file").disabled = $("save-plan-button").disabled;
    $("plan-definition").readOnly = $("save-plan-button").disabled;
    $("approve-plan-button").disabled = state.busy || !has("plan.approve") || !state.plan?.digest || approved || status !== "draft";
    $("plan-reviewed").disabled = $("approve-plan-button").disabled;
    const effects = (state.review?.effects || []).filter((effect) => unknownStates.has(effect.state) && effect.model_generation === true);
    $("reconcile-section").hidden = !selected || !has("control") || effects.length === 0;
    $("reconcile-button").disabled = state.busy || !has("control") || effects.length === 0;
    $("refresh-button").disabled = state.busy;
    document.querySelectorAll(".goal-button, .intervention-item button").forEach((button) => { button.disabled = state.busy; });
    return effects;
  }

  function logout(showNotice = true) {
    state.session += 1;
    state.selection += 1;
    state.controllers.forEach((controller) => controller.abort());
    state.controllers.clear();
    state.token = "";
    state.principal = null;
    state.goals = [];
    state.interventions = [];
    state.runId = null;
    state.goal = state.snapshot = state.plan = state.review = null;
    state.keys.clear();
    state.busy = false;
    $("access-token").value = "";
    $("login-panel").hidden = false;
    $("workspace").hidden = true;
    $("logout-button").hidden = $("refresh-button").hidden = true;
    $("identity-label").textContent = "";
    $("goal-list").replaceChildren();
    $("task-list").replaceChildren();
    $("event-list").replaceChildren();
    $("intervention-list").replaceChildren();
    $("review-summary").replaceChildren();
    for (const id of ["run-title", "run-id", "run-description", "plan-digest", "plan-revision", "cost-spent", "cost-reserved", "tokens-spent", "task-count"]) $(id).textContent = "";
    $("plan-definition").value = "";
    $("control-reason").value = "";
    $("effect-evidence").value = "";
    $("effect-reference").value = "";
    $("goal-form").reset();
    $("goal-dialog").close();
    $("login-button").disabled = false;
    if (showNotice) notify("已退出，页面内存中的访问凭据已清除。");
    $("access-token").focus();
  }

  function renderGoals() {
    const query = $("goal-search").value.trim().toLocaleLowerCase();
    const goals = state.goals.filter((goal) => `${goal.title || ""} ${goal.run_id}`.toLocaleLowerCase().includes(query));
    $("goals-state").textContent = goals.length ? `${goals.length} 个目标` : query ? "没有符合筛选条件的目标。" : "还没有目标。可创建一个草案。";
    $("goal-list").replaceChildren(...goals.map((goal) => {
      const item = node("li");
      const button = node("button", `goal-button${goal.run_id === state.runId ? " active" : ""}`);
      button.type = "button";
      button.disabled = state.busy;
      if (goal.run_id === state.runId) button.setAttribute("aria-current", "true");
      button.append(node("strong", "", goal.title || "未命名目标"));
      const meta = node("span", "goal-meta");
      meta.append(badge(goal.status), node("span", "mono", str(goal.run_id).slice(0, 8)));
      button.append(meta);
      button.addEventListener("click", () => selectRun(goal.run_id));
      item.append(button);
      return item;
    }));
  }

  function renderTasks() {
    const tasks = state.review?.tasks || state.snapshot?.tasks || [];
    $("tasks-hint").textContent = `${tasks.length} 个任务`;
    $("task-count").textContent = `${tasks.filter((task) => task.status === "done").length} / ${tasks.length}`;
    if (!tasks.length) {
      $("task-list").replaceChildren(node("p", "inline-empty", state.snapshot?.status === "draft" ? "草案尚未创建执行任务。保存并批准计划后，任务才会进入调度。" : "此运行暂无任务。"));
      return;
    }
    $("task-list").replaceChildren(...tasks.map((task, index) => {
      const row = node("article", "task-row");
      const content = node("div");
      content.append(node("span", "task-title", task.title || task.id));
      const detail = [task.role, task.model_alias, `epoch ${str(task.epoch, "0")}`, task.channel].filter(Boolean).join(" · ");
      content.append(node("div", "task-detail", detail));
      const artifact = task.artifact;
      if (artifact) {
        const cases = Array.isArray(artifact.cases) ? artifact.cases : [];
        const summary = [artifact.kind, cases.length ? `冻结用例 ${cases.filter((item) => item.passed === true).length}/${cases.length}` : "", artifact.verification === "awaiting_independent_models" ? "等待独立模型复核" : artifact.verification].filter(Boolean).join(" · ");
        content.append(node("div", "task-detail", summary));
        if (artifact.content_hash) content.append(node("div", "task-detail mono", `产物 ${artifact.content_hash}`));
      }
      row.append(node("span", "task-number", String(index + 1).padStart(2, "0")), content, badge(task.status));
      return row;
    }));
  }

  function renderPlan() {
    const plan = state.plan;
    $("plan-status").replaceWith(Object.assign(badge(plan?.status), {id: "plan-status"}));
    $("plan-revision").textContent = str(plan?.revision, "未保存");
    $("plan-digest").textContent = str(plan?.digest, "保存计划后生成摘要");
    $("plan-definition").value = plan?.definition ? JSON.stringify(plan.definition, null, 2) : "";
    $("plan-reviewed").checked = false;
    $("plan-file").value = "";
  }

  function renderInterventions() {
    $("intervention-count").textContent = count(state.interventions.length);
    if (!state.interventions.length) $("intervention-list").replaceChildren(node("p", "inline-empty", "当前没有人工待办。双签未配置时，提交的候选仍不会自动验收。"));
    else $("intervention-list").replaceChildren(...state.interventions.map((item) => {
      const article = node("article", "intervention-item");
      article.append(node("strong", "", item.title || item.run_id), badge(item.status));
      const unknown = Array.isArray(item.unknown_effects) ? item.unknown_effects.length : (integer(item.unknown_effects) ? item.unknown_effects : 0);
      article.append(node("p", "muted", `未知调用 ${unknown} · 待复核候选 ${str(item.awaiting_independent_verification, "0")}`));
      const counts = item.task_counts && typeof item.task_counts === "object" ? Object.entries(item.task_counts) : [];
      const taskSummary = counts.filter(([, total]) => integer(total) && total > 0).map(([status, total]) => `${statusNames[status] || status} ${count(total)}`).join(" · ");
      if (taskSummary) article.append(node("p", "muted", taskSummary));
      if (item.run_id !== state.runId) {
        const button = node("button", "", "查看此运行");
        button.disabled = state.busy;
        button.addEventListener("click", () => selectRun(item.run_id, "interventions"));
        article.append(button);
      }
      return article;
    }));
    const review = state.review;
    const summary = $("review-summary");
    summary.replaceChildren();
    if (!review) return;
    summary.append(node("h3", "", "当前运行的调用与预留"));
    const effects = Array.isArray(review.effects) ? review.effects : [];
    if (!effects.length) summary.append(node("p", "muted", "没有模型外部调用记录。"));
    for (const effect of effects) {
      const article = node("article", "intervention-item");
      article.append(node("strong", "mono", effect.call_id), badge(effect.state), node("p", "muted", `任务 ${str(effect.task_id)} · epoch ${str(effect.epoch)}`));
      summary.append(article);
    }
    for (const reservation of Array.isArray(review.reservations) ? review.reservations : []) {
      const article = node("article", "intervention-item");
      article.append(node("strong", "mono", reservation.reservation_id), badge(reservation.state), node("p", "muted", `预留 ${amount(reservation.cost)} / ${count(reservation.tokens)} tokens；已核实 ${amount(reservation.actual_cost)} / ${count(reservation.actual_tokens)} tokens`));
      summary.append(article);
    }
    const prior = $("effect-select").value;
    const unknown = effects.filter((effect) => unknownStates.has(effect.state) && effect.model_generation === true);
    $("effect-select").replaceChildren(...unknown.map((effect) => {
      const option = node("option", "", `${effect.call_id} · ${statusNames[effect.state] || effect.state}`);
      option.value = effect.call_id;
      return option;
    }));
    if (unknown.some((effect) => effect.call_id === prior)) $("effect-select").value = prior;
  }

  function renderRun() {
    $("empty-selection").hidden = Boolean(state.snapshot);
    $("run-content").hidden = !state.snapshot;
    if (!state.snapshot) { refreshControls(); return; }
    $("run-title").textContent = state.goal?.title || "独立运行";
    $("run-id").textContent = state.runId;
    $("run-description").textContent = state.goal?.description || "此运行没有目标描述。";
    $("run-status").replaceWith(Object.assign(badge(state.snapshot.status), {id: "run-status"}));
    $("cost-spent").textContent = amount(state.snapshot.spent);
    $("cost-reserved").textContent = amount(state.snapshot.reserved);
    $("tokens-spent").textContent = count(state.snapshot.tokens_spent);
    renderTasks(); renderPlan(); renderInterventions(); refreshControls();
  }

  async function loadEvents() {
    if (!state.runId) return;
    const generation = state.selection;
    $("events-state").textContent = "正在读取事件…";
    $("event-list").replaceChildren();
    try {
      const [result, integrity] = await Promise.all([request("GET", `${pathForRun()}/events`), request("GET", `${pathForRun()}/integrity`)]);
      if (generation !== state.selection) return;
      const events = Array.isArray(result.events) ? result.events.slice(-200).reverse() : [];
      $("integrity-label").textContent = integrity.valid === true ? "事件链核验通过 · 外部检查点未配置" : "事件链核验未通过";
      $("events-state").textContent = events.length ? `最近 ${events.length} 条，最新在前` : "此运行暂无事件。";
      $("event-list").replaceChildren(...events.map((event) => {
        const item = node("li", "event-row");
        const head = node("div", "event-head");
        head.append(node("span", "mono", `#${str(event.seq)}`), node("strong", "", event.type));
        item.append(head, node("p", "event-meta", [event.timestamp || event.occurred_at, event.actor || event.actor_id].filter(Boolean).join(" · ")));
        const details = node("details");
        details.append(node("summary", "", "查看事件数据"), node("pre", "", JSON.stringify(event.payload || {}, null, 2)));
        item.append(details);
        return item;
      }));
    } catch (error) {
      if (generation === state.selection && error.code !== "SESSION_CHANGED") $("events-state").textContent = publicError(error);
    }
  }

  function switchTab(name) {
    state.tab = name;
    for (const button of document.querySelectorAll("[data-tab]")) {
      const selected = button.dataset.tab === name;
      button.classList.toggle("active", selected);
      if (selected) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    }
    for (const panel of document.querySelectorAll(".tab-panel")) panel.hidden = panel.id !== `tab-${name}`;
    if (name === "events") loadEvents();
  }

  async function selectRun(runId, tab = state.tab) {
    const generation = ++state.selection;
    state.runId = runId;
    $("run-content").classList.add("loading");
    $("control-reason").value = "";
    $("terminate-confirm").value = "";
    renderGoals();
    const runPath = `/v1/runs/${encodeURIComponent(runId)}`;
    const goalPath = `/v1/goals/${encodeURIComponent(runId)}`;
    try {
      const [goal, plan, snapshot, review] = await Promise.allSettled([
        request("GET", goalPath), request("GET", `${goalPath}/plan`), request("GET", runPath), request("GET", `${runPath}/review`),
      ]);
      if (generation !== state.selection) return;
      if (snapshot.status !== "fulfilled") throw snapshot.reason;
      state.goal = goal.status === "fulfilled" ? goal.value : null;
      state.plan = plan.status === "fulfilled" ? plan.value : null;
      state.snapshot = snapshot.value;
      state.review = review.status === "fulfilled" ? review.value : null;
      renderRun();
      switchTab(tab);
    } catch (error) {
      if (generation !== state.selection || error.code === "SESSION_CHANGED") return;
      state.goal = state.snapshot = state.plan = state.review = null;
      renderRun();
      notify(publicError(error), true);
    } finally {
      if (generation === state.selection) $("run-content").classList.remove("loading");
    }
  }

  async function refreshAll(selectId = state.runId) {
    const session = state.session;
    $("goals-state").textContent = "正在读取目标…";
    try {
      const [goals, interventions] = await Promise.allSettled([request("GET", "/v1/goals"), request("GET", "/v1/interventions")]);
      if (session !== state.session) return;
      if (goals.status !== "fulfilled") throw goals.reason;
      state.goals = Array.isArray(goals.value.goals) ? goals.value.goals : [];
      state.interventions = interventions.status === "fulfilled" && Array.isArray(interventions.value.items) ? interventions.value.items : [];
      renderGoals(); renderInterventions();
      if (selectId) await selectRun(selectId);
      else if (state.goals.length) await selectRun(state.goals[0].run_id);
      else { state.snapshot = state.goal = state.plan = state.review = null; renderRun(); }
    } catch (error) {
      if (session !== state.session || error.code === "SESSION_CHANGED") return;
      $("goals-state").textContent = "目标读取失败。";
      notify(publicError(error), true);
    }
  }

  async function mutate(method, path, body, successMessage, after) {
    if (state.busy) return;
    const session = state.session;
    const fingerprint = JSON.stringify([method, path, body]);
    if (!state.keys.has(fingerprint)) state.keys.set(fingerprint, `console:${crypto.randomUUID()}`);
    state.busy = true;
    refreshControls();
    notify("正在提交，等待台账回执…");
    try {
      const result = await request(method, path, body, state.keys.get(fingerprint));
      if (session !== state.session) return;
      state.keys.delete(fingerprint);
      notify(successMessage);
      if (after) await after(result);
      else await refreshAll();
    } catch (error) {
      if (session === state.session && error.code !== "SESSION_CHANGED") notify(publicError(error), true);
    } finally {
      if (session === state.session) { state.busy = false; refreshControls(); }
    }
  }

  $("login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const token = $("access-token").value.trim();
    $("access-token").value = "";
    if (!token) return;
    const session = ++state.session;
    state.token = token;
    $("login-button").disabled = true;
    notify("正在验证本地身份…");
    try {
      const principal = await request("GET", "/v1/me");
      if (session !== state.session) return;
      if (!principal || !Array.isArray(principal.roles) || !Array.isArray(principal.capabilities)) throw Object.assign(new Error(), {code: "BAD_JSON"});
      state.principal = principal;
      $("identity-label").textContent = `${str(principal.subject_id)} · ${principal.roles.map((role) => roleNames[role] || role).join(" / ")}`;
      $("login-panel").hidden = true;
      $("workspace").hidden = false;
      $("logout-button").hidden = $("refresh-button").hidden = false;
      refreshControls();
      notify(`已连接租户 ${str(principal.tenant_id)}。`);
      await refreshAll();
      $("workspace").focus();
    } catch (error) {
      if (session !== state.session) return;
      logout(false);
      notify(publicError(error), true);
    } finally { if (session === state.session) $("login-button").disabled = false; }
  });
  $("logout-button").addEventListener("click", () => logout());
  $("refresh-button").addEventListener("click", () => refreshAll());
  $("goal-search").addEventListener("input", renderGoals);
  document.querySelectorAll("[data-tab]").forEach((button) => button.addEventListener("click", () => switchTab(button.dataset.tab)));
  $("show-interventions").addEventListener("click", () => {
    if (!state.runId && state.interventions.length) selectRun(state.interventions[0].run_id, "interventions");
    else if (state.runId) switchTab("interventions");
    else notify("当前没有人工待办。");
  });
  $("new-goal-button").addEventListener("click", () => { if (has("goal.write")) $("goal-dialog").showModal(); });
  $("close-goal-dialog").addEventListener("click", () => $("goal-dialog").close());
  $("goal-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const title = $("goal-title").value.trim(), description = $("goal-description").value.trim();
    const acceptance = Array.from(document.querySelectorAll('input[name="criterion"]:checked')).map((input) => input.value);
    const cost = Number($("goal-cost-limit").value), tokens = Number($("goal-token-limit").value);
    if (!has("goal.write") || !title || !description || !acceptance.length || !integer(cost) || !integer(tokens)) { notify("请填写标题、需求和至少一项验收判据，并使用非负整数预算。", true); return; }
    mutate("POST", "/v1/goals", {title, description, acceptance, cost_limit_microusd: cost, token_limit: tokens}, "目标草案已创建，尚未发起模型或代码执行。", async (result) => {
      $("goal-dialog").close(); $("goal-form").reset(); await refreshAll(result.run_id);
    });
  });
  $("plan-file").addEventListener("change", async () => {
    const file = $("plan-file").files[0];
    if (!file) return;
    if (file.size > 262144) { notify("计划文件不能超过 256 KiB。", true); $("plan-file").value = ""; return; }
    const generation = state.selection;
    try {
      const text = await file.text();
      const definition = JSON.parse(text);
      if (!definition || Array.isArray(definition) || typeof definition !== "object") throw new Error();
      if (generation === state.selection) { $("plan-definition").value = JSON.stringify(definition, null, 2); $("plan-reviewed").checked = false; }
    } catch { notify("计划文件不是有效的 JSON 对象。", true); }
  });
  $("plan-definition").addEventListener("input", () => { $("plan-reviewed").checked = false; });
  $("plan-form").addEventListener("submit", (event) => {
    event.preventDefault();
    if (!has("goal.write") || !state.plan) return;
    let definition;
    try {
      definition = JSON.parse($("plan-definition").value);
      if (!definition || Array.isArray(definition) || typeof definition !== "object") throw new Error();
    } catch { notify("请提供有效的工作流 JSON 对象。", true); return; }
    mutate("PUT", `${pathForGoal()}/plan`, {definition, expected_revision: state.plan.revision}, "新修订已保存。请审阅服务端生成的摘要后批准。");
  });
  $("approve-plan-form").addEventListener("submit", (event) => {
    event.preventDefault();
    if (!has("plan.approve") || !state.plan?.digest || !$("plan-reviewed").checked) return;
    let edited;
    try { edited = JSON.parse($("plan-definition").value); } catch { edited = null; }
    if (JSON.stringify(edited) !== JSON.stringify(state.plan.definition)) { notify("计划文本有未保存的修改，请先保存并重新审阅摘要。", true); return; }
    mutate("POST", `${pathForGoal()}/plan/approve`, {expected_revision: state.plan.revision, expected_digest: state.plan.digest}, "当前计划已批准，冻结任务合同已创建。此操作不会执行模型或通过最终验收。");
  });

  function control(action) {
    if (!has("control") || !state.runId) return;
    const reason = $("control-reason").value.trim();
    if (!reason) { notify("请填写操作理由。", true); $("control-reason").focus(); return; }
    if (action === "terminate" && $("terminate-confirm").value !== "终止") { notify("请输入“终止”确认此操作。", true); $("terminate-confirm").focus(); return; }
    const messages = {pause: "运行已暂停，已发生或未知的调用继续保留。", resume: "运行已恢复。未取得独立验证的候选仍不会放行下游。", terminate: "运行已终止。未知调用与未核对预留仍需处理。"};
    mutate("POST", `${pathForRun()}/control/${action}`, {reason}, messages[action]);
  }
  $("pause-button").addEventListener("click", () => control("pause"));
  $("resume-button").addEventListener("click", () => control("resume"));
  $("terminate-button").addEventListener("click", () => control("terminate"));
  $("effect-decision").addEventListener("change", () => {
    if ($("effect-decision").value === "not_sent") { $("effect-cost").value = "0"; $("effect-tokens").value = "0"; }
  });
  $("reconcile-form").addEventListener("submit", (event) => {
    event.preventDefault();
    if (!has("control") || !state.runId) return;
    const call = $("effect-select").value;
    const decision = $("effect-decision").value;
    const reference = $("effect-reference").value.trim(), summary = $("effect-evidence").value.trim();
    const cost = Number($("effect-cost").value), tokens = Number($("effect-tokens").value);
    if (!call || !reference || !summary || !integer(cost) || !integer(tokens)) { notify("请填写调用、依据引用、核对说明及非负整数用量。", true); return; }
    if (decision === "not_sent" && (cost !== 0 || tokens !== 0)) { notify("确定未发送的调用不能同时登记非零使用量。", true); return; }
    mutate("POST", `${pathForRun()}/effects/${encodeURIComponent(call)}/reconcile`, {decision, evidence: {reference, summary}, actual_cost_microusd: cost, actual_tokens: tokens}, "核对结果已记录。运行未自动恢复，候选也未自动通过验收。", async () => {
      $("effect-reference").value = $("effect-evidence").value = "";
      await refreshAll();
    });
  });
  refreshControls();
})();
