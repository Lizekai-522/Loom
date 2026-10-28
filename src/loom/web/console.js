"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  // Keep JSON number lexemes for display without changing metadata types.
  // Weak keys let candidate data leave memory when a selection/session clears.
  const numberSources = new WeakMap();
  function parseResponseJSON(text) {
    return JSON.parse(text, function (key, value, context) {
      if (typeof value === "number" && typeof context?.source === "string") {
        if (!numberSources.has(this)) numberSources.set(this, new Map());
        numberSources.get(this).set(key, context.source);
      }
      return value;
    });
  }
  function exactBodyText(body) {
    return JSON.stringify(body, function (key, value) {
      if (typeof value !== "number") return value;
      const literal = numberSources.get(this)?.get(key);
      if (typeof JSON.rawJSON !== "function" || typeof literal !== "string") throw new Error("EXACT_JSON_UNAVAILABLE");
      return JSON.rawJSON(literal);
    }, 2);
  }
  const state = {
    principal: null, goals: [], interventions: [], runId: null,
    goal: null, snapshot: null, plan: null, review: null, tab: "tasks",
    busy: false, loading: false, pendingRunId: null, loadedSelection: 0,
    interventionsError: false, noticeKind: null, session: 0, selection: 0, controllers: new Set(), keys: new Map(),
    artifacts: [], artifactStatus: "idle", artifactError: "", artifactId: "", artifactDetail: null,
    detailStatus: "idle", detailError: "", targetId: "", context: null, contextStatus: "idle", contextError: "",
    artifactVersion: 0, detailVersion: 0, contextVersion: 0, artifactsSelection: 0,
    contextBindingChanged: false,
  };
  const label = window.LoomZH.label;
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

  function notify(message, error = false, kind = null) {
    state.noticeKind = kind;
    $("notice").textContent = message;
    $("notice").className = error ? "notice error" : "notice";
    $("notice").hidden = !message;
    $("notice").setAttribute("role", error ? "alert" : "status");
  }

  function publicError(error) {
    if (error.status === 401) return window.LoomPreview?.active ? "体验会话已过期，请重新打开体验链接并输入访问码。" : "未启用本机开发入口，请使用项目启动器启动服务，再点击“重新连接”。";
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
      const headers = {Accept: "application/json", "X-Loom-Console": "local"};
      if (body !== undefined) headers["Content-Type"] = "application/json";
      if (key) headers["Idempotency-Key"] = key;
      const response = await fetch(path, {
        method, headers, body: body === undefined ? undefined : JSON.stringify(body),
        cache: "no-store", credentials: "same-origin", redirect: "error", signal: controller.signal,
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
      try { return parseResponseJSON(new TextDecoder("utf-8", {fatal: true}).decode(raw)); }
      catch { throw Object.assign(new Error(), {code: "BAD_JSON"}); }
    } finally {
      clearTimeout(timer);
      state.controllers.delete(controller);
    }
  }

  function refreshControls() {
    for (const element of document.querySelectorAll("[data-cap]")) {
      element.disabled = state.busy || state.loading || !has(element.dataset.cap);
    }
    const status = state.snapshot?.status;
    const selected = Boolean(state.runId && state.snapshot);
    const unchecked = state.loading || (selected && state.loadedSelection !== state.selection);
    const approved = state.plan?.status === "approved";
    $("control-fields").hidden = !selected || !has("control");
    $("action-state").textContent = state.loading ? "正在核对所选运行，加载完成前暂停写入操作。"
      : unchecked ? "当前显示的数据尚未重新核对，请刷新或重选目标后再操作。"
      : !selected ? "选择目标后可查看可用操作。"
      : !has("control") ? "此身份可查看运行，人工控制需要具名操作员权限。"
      : terminalStates.has(status) ? "运行已经终止或完成，不能再恢复执行。"
      : status === "draft" ? "请先保存并批准草案计划。"
      : status === "submitted" ? "候选等待独立复核，人工控制不会替代验收。"
      : "所有控制操作都会写入台账，并保留操作理由。";
    $("pause-button").disabled = state.busy || unchecked || !has("control") || status !== "open";
    $("resume-button").disabled = state.busy || unchecked || !has("control") || !["paused", "awaiting_human"].includes(status);
    $("resume-preview-button").disabled = state.busy || unchecked || !has("control") || !selected;
    $("terminate-button").disabled = state.busy || unchecked || !has("control") || !selected || terminalStates.has(status);
    $("save-plan-button").disabled = state.busy || unchecked || !has("goal.write") || !state.plan || approved || status !== "draft";
    $("plan-file").disabled = $("save-plan-button").disabled;
    $("plan-definition").readOnly = $("save-plan-button").disabled;
    const suiteApprovalMissing = state.plan?.definition?.nodes?.some((task) => task.development_suite) && !has("suite.approve");
    $("approve-plan-button").disabled = state.busy || unchecked || !has("plan.approve") || suiteApprovalMissing || !state.plan?.digest || approved || status !== "draft";
    $("plan-reviewed").disabled = $("approve-plan-button").disabled;
    const effects = (state.review?.effects || []).filter((effect) => unknownStates.has(effect.state) && effect.model_generation === true);
    $("reconcile-section").hidden = !selected || !has("control") || effects.length === 0;
    $("reconcile-button").disabled = state.busy || unchecked || !has("control") || effects.length === 0;
    $("refresh-button").disabled = state.busy;
    document.querySelectorAll(".goal-button, .intervention-item button").forEach((button) => { button.disabled = state.busy; });
    document.querySelectorAll(".source-open").forEach((button) => { button.disabled = state.busy || !runChecked(); });
    refreshArtifactControls();
    suites.sync();
    reviewDecisions.sync();
    progression.sync();
    localExecution.sync();
    sourcePanel.sync();
    return effects;
  }

  function logout(showNotice = true) {
    state.session += 1;
    state.selection += 1;
    state.controllers.forEach((controller) => controller.abort());
    state.controllers.clear();
    state.principal = null;
    state.goals = [];
    state.interventions = [];
    state.runId = null;
    state.goal = state.snapshot = state.plan = state.review = null;
    state.keys.clear();
    state.busy = false;
    state.loading = false;
    state.pendingRunId = null;
    state.loadedSelection = 0;
    state.interventionsError = false;
    resetArtifacts();
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
    $("resume-preview-state").textContent = "检查不会恢复运行或发起模型调用。";
    $("effect-evidence").value = "";
    $("effect-reference").value = "";
    $("goal-form").reset();
    $("goal-dialog").close();
    $("login-button").disabled = false;
    if (showNotice) notify("已断开本机工作台。点击“重新连接”即可继续。");
    $("login-button").focus();
  }

  function renderGoals() {
    const query = $("goal-search").value.trim().toLocaleLowerCase();
    const goals = state.goals.filter((goal) => `${goal.title || ""} ${goal.run_id}`.toLocaleLowerCase().includes(query));
    $("goals-state").textContent = goals.length ? `${goals.length} 个目标` : query ? "没有符合筛选条件的目标。" : "还没有目标。可创建一个草案。";
    $("goal-list").replaceChildren(...goals.map((goal) => {
      const item = node("li");
      const selectedId = state.pendingRunId || state.runId;
      const button = node("button", `goal-button${goal.run_id === selectedId ? " active" : ""}`);
      button.type = "button";
      button.disabled = state.busy;
      if (goal.run_id === selectedId) button.setAttribute("aria-current", "true");
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
    $("tasks-hint").textContent = state.review ? `${tasks.length} 个任务` : "任务证据摘要读取失败，模型来源状态未知；请刷新重试。";
    $("task-count").textContent = `${tasks.filter((task) => task.status === "done").length} / ${tasks.length}`;
    if (!tasks.length) {
      $("task-list").replaceChildren(node("p", "inline-empty", state.snapshot?.status === "draft" ? "草案尚未创建执行任务。保存并批准计划后，任务才会进入调度。" : "此运行暂无任务。"));
      return;
    }
    $("task-list").replaceChildren(...tasks.map((task, index) => {
      const row = node("article", "task-row");
      const content = node("div");
      content.append(node("span", "task-title", task.title || task.id));
      const detail = [label(task.role), task.model_alias, `轮次 ${str(task.epoch, "0")}`, label(task.channel)].filter(Boolean).join(" · ");
      content.append(node("div", "task-detail", detail));
      const artifact = task.artifact;
      if (artifact) {
        const cases = Array.isArray(artifact.cases) ? artifact.cases : [];
        const summary = [label(artifact.kind), cases.length ? `冻结用例 ${cases.filter((item) => item.passed === true).length}/${cases.length}` : "", artifact.verification === "awaiting_independent_models" ? "等待独立模型复核" : label(artifact.verification)].filter(Boolean).join(" · ");
        content.append(node("div", "task-detail", summary));
        if (artifact.content_hash) content.append(node("div", "task-detail mono", `${artifact.kind === "role-artifact" ? "任务输出摘要" : "产物"} ${artifact.content_hash}`));
        if (artifact.kind === "role-artifact") {
          const source = artifact.model_provenance;
          const recorded = artifact.model_source_recorded === true && artifact.model_source_status === "recorded"
            && validRoleSource(source) && source.source_epoch === task.epoch && source.plan_digest === state.plan?.digest;
          content.append(roleSourceDisplay(recorded ? source : null, recorded ? "recorded" : artifact.model_source_status, true));
        }
        if (artifact.kind === "python-json-program" && artifact.trusted_evaluation === true && ["submitted", "done"].includes(task.status)) {
          const button = node("button", "source-open", "查看源码与导出"); button.type = "button"; button.dataset.sourceTask = task.id;
          button.disabled = state.busy || !runChecked(); button.addEventListener("click", () => { if (!button.disabled) sourcePanel.open(task); });
          content.append(button);
        }
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
    $("intervention-count").textContent = state.interventionsError ? "读取失败" : count(state.interventions.length);
    if (state.interventionsError) $("intervention-list").replaceChildren(node("p", "inline-empty", "人工待办读取失败，当前数量未知。请点击工作台“刷新”重试，不能据此判断没有待办。"));
    else if (!state.interventions.length) $("intervention-list").replaceChildren(node("p", "inline-empty", "当前没有人工待办。双签未配置时，提交的候选仍不会自动验收。"));
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
      article.append(node("strong", "mono", effect.call_id), badge(effect.state), node("p", "muted", `任务 ${str(effect.task_id)} · 轮次 ${str(effect.epoch)}`));
      summary.append(article);
    }
    for (const reservation of Array.isArray(review.reservations) ? review.reservations : []) {
      const article = node("article", "intervention-item");
      article.append(node("strong", "mono", reservation.reservation_id), badge(reservation.state), node("p", "muted", `预留 ${amount(reservation.cost)} / ${count(reservation.tokens)} 词元；已核实 ${amount(reservation.actual_cost)} / ${count(reservation.actual_tokens)} 词元`));
      summary.append(article);
    }
    const attempts = Array.isArray(review.attempts) ? review.attempts : [];
    if (attempts.length) summary.append(node("h3", "", "执行与修复记录"));
    for (const attempt of attempts.slice(-50).reverse()) {
      const article = node("article", "intervention-item");
      const contextEvent = attempt.event_type === "execution.context_bound";
      const roleEvent = attempt.event_type === "role.candidate_generated";
      const roleSource = roleEvent && validRoleSource(attempt) && attempt.source_epoch === attempt.epoch
        && attempt.source_event_hash === attempt.event_hash ? attempt : null;
      const contextValid = contextEvent && typeof attempt.context_digest === "string" && hashPattern.test(attempt.context_digest)
        && integer(attempt.context_bytes) && attempt.context_bytes <= 65536
        && integer(attempt.context_item_count) && attempt.context_item_count <= 8
        && attempt.untrusted_data === true && attempt.producer_verified === false && attempt.counts_as_success === false;
      article.append(node("strong", "", roleEvent ? roleSource ? "模型生成候选来源已绑定执行记录" : "模型来源记录待核对"
        : contextEvent ? contextValid ? "本次尝试已绑定辅助上下文" : "辅助上下文绑定记录待核对" : label(attempt.event_type)));
      const detail = [attempt.task_id, attempt.epoch === undefined ? "" : `轮次 ${attempt.epoch}`,
        ...(contextEvent || roleEvent ? [] : [...[attempt.primitive, attempt.family, attempt.code, attempt.reason, attempt.stage].map(label)])].filter(Boolean).join(" · ");
      article.append(node("p", "muted", detail));
      if (roleEvent) {
        article.append(roleSourceDisplay(roleSource, "unavailable"));
        const currentTask = (review.tasks || []).find((task) => task.id === attempt.task_id);
        if (currentTask && currentTask.epoch !== attempt.epoch) article.append(node("p", "muted", `历史模型来源记录 · 当前任务 轮次 ${str(currentTask.epoch)}`));
        article.append(node("p", "field-help", "来源记录仅关联本机网关的模型响应，不证明内容正确、供应商账单已核验或独立验收通过。"));
      } else if (contextEvent) {
        if (contextValid) {
          article.append(node("p", "mono break-word", `辅助上下文摘要 ${attempt.context_digest}`),
            node("p", "muted", `${count(attempt.context_bytes)} UTF-8 字节 · ${count(attempt.context_item_count)} 个辅助条目 · 不可信数据 · 未验证`));
        } else article.append(node("p", "inline-error", "绑定摘要字段不完整或不合法，未据此确认绑定。请刷新并核对执行证据。"));
        const currentTask = (review.tasks || []).find((task) => task.id === attempt.task_id);
        if (currentTask && currentTask.epoch !== attempt.epoch) article.append(node("p", "muted", `历史尝试记录 · 当前任务 轮次 ${str(currentTask.epoch)}`));
        article.append(node("p", "field-help", "此记录仅表示本次尝试获准使用该辅助上下文，不证明提供方已收到或执行完成；不验证内容、不放行依赖，也不代替独立验收。"));
      } else if (attempt.from_epoch !== undefined || attempt.to_epoch !== undefined) {
        article.append(node("p", "muted", `尝试 ${str(attempt.from_epoch)} → ${str(attempt.to_epoch)}`));
      }
      const hashes = contextEvent || roleEvent ? [["event_hash", "事件"], ["plan_digest", "计划摘要"]]
        : [["event_hash", "事件"], ["source_sha256", "源码摘要"], ["evaluation_digest", "测试摘要"]];
      for (const [key, label] of hashes) {
        if (typeof attempt[key] === "string") article.append(node("p", "mono break-word", `${label} ${attempt[key]}`));
      }
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
    syncEffectEvidence();
  }

  function syncEffectEvidence() {
    const effect = (state.review?.effects || []).find((item) => item.call_id === $("effect-select").value);
    const option = $("effect-decision").querySelector('option[value="not_sent"]');
    const allowed = effect?.not_sent_conflicts_with_evidence === false;
    option.disabled = !allowed;
    if (!allowed && $("effect-decision").value === "not_sent") $("effect-decision").value = "completed";
    $("effect-evidence-state").textContent = !effect ? "" : effect.not_sent_conflicts_with_evidence === true
      ? "已有部分调用证据，不能登记为确定未发送；请核对真实用量。"
      : allowed ? "仅在确有可靠依据时才能登记为确定未发送。" : "调用证据状态未能核对，请刷新后再处理。";
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

  const hashPattern = /^[0-9a-f]{64}$/;
  function validRoleSource(source) {
    if (!source || typeof source !== "object" || source.model_response_recorded !== true
        || source.producer_verified !== false || source.counts_as_success !== false
        || source.provider !== "deepseek" || source.model !== "deepseek-flash"
        || typeof source.route !== "string" || !/^[a-z][a-z0-9_-]{0,47}$/.test(source.route)
        || typeof source.artifact_id !== "string" || !/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(source.artifact_id)
        || !integer(source.source_epoch) || source.source_epoch < 1) return false;
    if (!["requirements", "design", "public-checks", "review-findings"].includes(source.kind)) return false;
    const expected = {requirements: ["analyst", "loom.requirements/v1"], design: ["architect", "loom.design/v1"], "public-checks": ["tester", "loom.public-checks/v1"], "review-findings": ["reviewer", "loom.review-findings/v1"]}[source.kind];
    if (!expected || source.role !== expected[0] || source.schema_version !== expected[1]) return false;
    if (source.kind === "public-checks" && source.public_checks_executed !== false) return false;
    if (["call_id", "artifact_content_hash", "plan_digest", "source_event_hash"].some((key) => typeof source[key] !== "string" || !hashPattern.test(source[key]))) return false;
    if (["prompt_tokens", "completion_tokens", "total_tokens"].some((key) => !integer(source[key]) || source[key] > 1048576)
        || source.prompt_tokens + source.completion_tokens !== source.total_tokens
        || !integer(source.usage_cost_upper_microusd) || source.usage_cost_upper_microusd > 1000000000000) return false;
    return true;
  }

  function candidateRoleSource(item) {
    const source = item?.model_provenance;
    if (item?.origin !== "model-gateway-response" || item.model_source_recorded !== true || !source
        || item.producer_verified !== false || item.counts_as_success !== false
        || source.task_id !== item.source_task_id || source.epoch !== item.source_epoch
        || source.content_hash !== item.content_hash || source.plan_digest !== item.plan_digest
        || source.role_output?.kind !== item.kind || source.role_output?.schema_version !== item.schema_version) return null;
    const summary = {artifact_id: item.artifact_id, call_id: source.call_id, artifact_content_hash: item.content_hash,
      plan_digest: item.plan_digest, source_event_hash: item.source_event_hash, role: source.role, kind: item.kind,
      schema_version: item.schema_version, source_epoch: item.source_epoch, provider: source.provider,
      model: source.model, route: source.route, prompt_tokens: source.usage?.prompt_tokens,
      completion_tokens: source.usage?.completion_tokens, total_tokens: source.usage?.total_tokens,
      usage_cost_upper_microusd: source.usage_cost_upper_microusd, model_response_recorded: true,
      producer_verified: false, counts_as_success: false};
    if (item.kind === "public-checks") summary.public_checks_executed = false;
    return validRoleSource(summary) ? summary : null;
  }

  function roleSourceDisplay(source, status, compact = false) {
    const block = node("div", "artifact-metadata");
    const labels = {version_mismatch: "版本不匹配，未确认当前来源", worker_submission: "未取得匹配执行记录（执行器 提交）", unavailable: "证据暂不可用"};
    block.append(node("p", "task-detail", `模型来源：${source ? "已绑定执行记录" : labels[status] || "证据摘要待核对"}`),
      node("p", "task-detail", "内容：未独立验证"));
    if (source) {
      block.append(node("p", "task-detail", `${label(source.role)} · ${source.provider} / ${source.model} · 路由 ${source.route}`));
      if (source.kind === "public-checks") block.append(node("p", "task-detail", "公开开发检查候选：用例未执行，未作为冻结验收。"));
      if (!compact) {
        for (const [key, label] of [["artifact_id", "候选"], ["artifact_content_hash", "候选正文摘要"], ["call_id", "模型调用"], ["source_event_hash", "来源事件"]]) block.append(node("p", "mono break-word", `${label} ${source[key]}`));
        block.append(node("p", "task-detail", `已知用量 ${count(source.prompt_tokens)} 输入 + ${count(source.completion_tokens)} 输出 = ${count(source.total_tokens)} 词元；费用上界 ${amount(source.usage_cost_upper_microusd)}，非供应商账单。`));
      }
    }
    return block;
  }
  const artifactFields = ["artifact_id", "revision", "kind", "schema_version", "source_task_id", "source_node", "source_epoch", "plan_digest", "content_hash", "source_event_hash"];
  const runChecked = () => Boolean(state.runId && state.snapshot && !state.loading
    && state.loadedSelection === state.selection && (state.snapshot.id || state.snapshot.run_id) === state.runId);
  const targetTask = () => state.review?.tasks?.find((task) => task.id === state.targetId);
  const sameArtifact = (left, right) => Boolean(left && right && artifactFields.every((key) => left[key] === right[key]));
  const suites = window.LoomSuitePanel({state, $, node, has, request, mutate, notify, publicError,
    exactBodyText, sameArtifact, runChecked, label});
  const reviewDecisions = window.LoomReviewDecisions({state, $, node, has, request, publicError, sameArtifact, runChecked});
  const localExecution = window.LoomExecutionPanel({state,node,has,request,publicError,runChecked,refresh:()=>refreshAll()});
  const sourcePanel = window.LoomSourcePanel({state,node,request,publicError,runChecked});
  const progression = window.LoomProgression({state, $, node, request, publicError, runChecked, label, navigate: navigateProgression, executionPanel:localExecution});

  async function navigateProgression(item) {
    if (!runChecked() || state.busy) return;
    if (["view_inputs", "review_decision", "view_candidate"].includes(item.action)) {
      const loading = loadArtifacts(item.artifact_id || item.suggested_inputs[0]?.artifact_id || "", item.task_id);
      switchTab("artifacts");
      await loading;
    } else switchTab(item.action === "view_plan" ? "plan" : item.action === "view_interventions" ? "interventions" : "tasks");
  }

  function resetArtifacts() {
    sourcePanel.reset();
    suites.reset();
    reviewDecisions.reset();
    state.artifactVersion += 1; state.detailVersion += 1; state.contextVersion += 1;
    state.artifacts = []; state.artifactStatus = "idle"; state.artifactError = "";
    state.artifactId = ""; state.artifactDetail = null; state.detailStatus = "idle"; state.detailError = "";
    state.targetId = ""; state.context = null; state.contextStatus = "idle"; state.contextError = "";
    state.contextBindingChanged = false;
    state.artifactsSelection = 0;
    $("auxiliary-reviewed").checked = false;
    renderArtifacts();
  }

  function approvalBody() {
    const item = state.artifactDetail, task = targetTask();
    const listed = state.artifacts.find((candidate) => candidate.artifact_id === state.artifactId);
    if (!runChecked() || state.busy || !has("control") || !state.principal.roles.includes("operator")
        || state.snapshot.status !== "open" || state.artifactStatus !== "ready" || state.detailStatus !== "ready"
        || state.contextStatus === "loading" || state.contextBindingChanged || state.artifactsSelection !== state.selection
        || !sameArtifact(item, listed) || item.stale !== false || listed.stale !== false
        || !(item.origin === "authenticated-worker-submission" || candidateRoleSource(item)) || item.producer_verified !== false || item.counts_as_success !== false
        || !task || !integer(task.epoch) || ["stale", "cancelled", "done", "indeterminate"].includes(task.status)
        || !Number.isSafeInteger(item.revision) || item.revision < 1 || !Number.isSafeInteger(item.source_epoch) || item.source_epoch < 1
        || !hashPattern.test(item.content_hash) || !hashPattern.test(item.plan_digest) || item.plan_digest !== state.plan?.digest
        || !(task.input_artifacts || []).some((selector) => selector.source_node === item.source_node
          && selector.kind === item.kind && selector.schema_version === item.schema_version)) return null;
    try { exactBodyText(item.body); } catch { return null; }
    if (item.kind === "review-findings" && item.body?.verdict !== "findings") return null;
    return {artifact_id: item.artifact_id, expected_plan_digest: item.plan_digest, expected_source_epoch: item.source_epoch,
      expected_target_epoch: task.epoch, expected_revision: item.revision, expected_content_hash: item.content_hash};
  }

  function refreshArtifactControls() {
    const locked = state.busy || !runChecked();
    $("artifacts-refresh").disabled = locked || state.artifactStatus === "loading";
    $("input-target").disabled = locked || state.artifactStatus !== "ready" || !state.review;
    $("context-refresh").disabled = locked || !targetTask() || state.contextStatus === "loading";
    document.querySelectorAll(".artifact-select").forEach((button) => { button.disabled = locked || state.artifactStatus !== "ready"; });
    const allowed = Boolean(approvalBody());
    $("auxiliary-form").hidden = state.artifactDetail?.kind === "review-findings" || !has("control") || !state.principal?.roles?.includes("operator");
    $("auxiliary-reviewed").disabled = !allowed;
    $("auxiliary-approve").disabled = !allowed || !$("auxiliary-reviewed").checked;
    $("auxiliary-help").textContent = state.artifactDetail?.kind === "review-findings" && state.artifactDetail.body?.verdict === "no_findings"
      ? "本次审查未发现问题，无需批准修复；候选仍未独立验证，不能据此完成任务。"
      : allowed ? "本次批准不会验证候选、恢复任务或修改依赖。服务端将再次核对绑定版本。"
      : "选择已读取的当前候选与允许该类型的目标，核对版本后方可批准。失效、读取中或状态变化时须重新读取。";
  }

  function boundedBody(body) {
    const details = node("details", "artifact-body");
    details.append(node("summary", "", "展开不可信 结构化 正文（纯文本）"));
    let text;
    try { text = exactBodyText(body) || ""; }
    catch {
      details.append(node("p", "inline-error", "当前浏览器无法原样显示 JSON 数值，不能据此批准候选。请更新浏览器后重试。"));
      return details;
    }
    details.append(node("pre", "", text.slice(0, 32768)));
    if (text.length > 32768) details.append(node("p", "field-help", "页面显示已截断至 32768 个字符，摘要仍对应服务端完整正文。"));
    return details;
  }

  function artifactMetadata(item) {
    const block = node("div", "artifact-metadata");
    block.append(node("p", "muted", `${label(item.kind)} · ${str(item.schema_version)} · 节点 ${str(item.source_node)} · 源 轮次 ${str(item.source_epoch)} · 修订 ${str(item.revision)}`));
    for (const [key, label] of [["artifact_id", "候选"], ["source_task_id", "源任务"], ["content_hash", "内容摘要"], ["plan_digest", "计划摘要"], ["source_event_hash", "来源事件"]]) {
      block.append(node("p", "mono break-word", `${label} ${str(item[key])}`));
    }
    block.append(roleSourceDisplay(candidateRoleSource(item), item.origin === "authenticated-worker-submission" ? "worker_submission" : "unavailable"));
    return block;
  }

  function renderArtifacts() {
    suites.sync();
    reviewDecisions.sync();
    const status = state.artifactStatus;
    $("artifacts-state").textContent = status === "loading" ? "正在读取候选产物…"
      : status === "error" ? `候选产物读取失败，当前数量未知。${state.artifactError}`
      : status === "ready" ? `${state.artifacts.length} 个候选${state.artifacts.length > 100 ? "，页面展示最新 100 个" : ""}。候选均不计成功。`
      : "选择目标并打开此页读取候选。";
    const list = $("artifact-list"); list.replaceChildren();
    if (status === "ready" && !state.artifacts.length) list.append(node("p", "inline-empty", "当前运行尚无候选产物。"));
    if (status === "ready") for (const item of state.artifacts.slice(-100).reverse()) {
      const article = node("article", "artifact-card");
      article.append(node("strong", "", `${label(item.kind)} · 修订 ${str(item.revision)}`), node("span", "badge attention-state", item.stale ? "已失效 · 未验证" : "候选 · 未验证"));
      article.append(node("p", "muted", `${item.origin === "model-gateway-response" ? "模型网关响应候选" : item.origin === "authenticated-worker-submission" ? "已认证 执行器 提交" : "来源待核对"} · 节点 ${str(item.source_node)} · 源 轮次 ${str(item.source_epoch)}`));
      article.append(roleSourceDisplay(candidateRoleSource(item), item.origin === "authenticated-worker-submission" ? "worker_submission" : "unavailable", true));
      article.append(node("p", "mono break-word", `内容摘要 ${str(item.content_hash)}`));
      const button = node("button", "artifact-select", item.artifact_id === state.artifactId ? "已选候选 · 重新读取" : "查看并选择候选");
      button.type = "button"; button.dataset.artifactId = item.artifact_id;
      if (item.artifact_id === state.artifactId) button.setAttribute("aria-current", "true");
      button.addEventListener("click", () => loadArtifactDetail(item.artifact_id)); article.append(button); list.append(article);
    }
    const detail = $("artifact-detail"); detail.replaceChildren();
    if (state.detailStatus === "loading") detail.append(node("p", "muted", "正在读取所选候选正文…"));
    else if (state.detailStatus === "error") detail.append(node("p", "inline-error", `候选详情读取失败，不能批准。${state.detailError}`));
    else if (state.artifactDetail && state.detailStatus === "ready") {
      detail.append(node("h3", "", "所选候选 · 未独立验证"), artifactMetadata(state.artifactDetail));
      if (state.artifactDetail.stale) detail.append(node("p", "inline-error", "此候选已失效，不能新批准。请重新读取并审阅新版本。"));
      detail.append(boundedBody(state.artifactDetail.body));
    }
    const tasks = Array.isArray(state.review?.tasks) ? state.review.tasks : [];
    $("input-target").replaceChildren(...tasks.map((task) => {
      const option = node("option", "", `${task.title || task.id} · 轮次 ${str(task.epoch)}`); option.value = task.id; return option;
    }));
    $("input-target").value = state.targetId;
    const task = targetTask(), selectors = task?.input_artifacts || [];
    $("input-selectors").textContent = !state.review ? "任务合同读取失败，允许输入未知。请刷新运行后重试。"
      : !task ? "暂无目标任务。" : !selectors.length ? "此目标未声明辅助输入选择器，不能批准候选给它。"
      : selectors.map((selector) => `${selector.source_node} / ${selector.kind} / ${selector.schema_version} · ${selector.required ? "必需" : "可选"}`).join("；");
    const item = state.artifactDetail;
    $("auxiliary-binding").textContent = item && task ? `目标 ${task.id} · 目标 轮次 ${task.epoch}\n源 轮次 ${item.source_epoch} · 修订 ${item.revision}\n内容 ${item.content_hash}\n计划 ${item.plan_digest}` : "先读取候选并选择目标任务。";
    renderContext(); refreshArtifactControls();
  }

  function renderContext() {
    const status = state.contextStatus, preview = state.context;
    $("context-state").textContent = status === "loading" ? "正在读取目标辅助输入预览…"
      : status === "error" ? `预览读取失败，输入状态未知。${state.contextError}`
      : status === "ready" ? `${preview.ready ? "辅助数据齐备" : "仍缺必需辅助输入"} · 目标 轮次 ${preview.target_epoch} · ${preview.bytes} / ${preview.max_bytes} UTF-8 字节 · 本次预览不会发起执行，实际绑定看人工待办中的执行记录`
      : "选择目标任务后读取预览。";
    const panel = $("context-preview"); panel.replaceChildren();
    if (status !== "ready") return;
    panel.append(node("p", "mono break-word", `上下文摘要 ${preview.context_digest}`));
    for (const missing of preview.missing_inputs.slice(0, 64)) panel.append(node("p", "field-help", `缺少${missing.required ? "必需" : "可选"}输入：${str(missing.source_node)} / ${str(missing.kind)} / ${str(missing.schema_version)}`));
    if (!preview.items.length) panel.append(node("p", "muted", "尚无已批准的辅助输入。"));
    for (const item of preview.items.slice(0, 64)) {
      const article = node("article", "artifact-card");
      article.append(node("h3", "", "辅助数据 · 不可信 · 未验证"), artifactMetadata(item), boundedBody(item.body)); panel.append(article);
    }
  }

  async function loadArtifacts(preferredId = "", preferredTarget = "") {
    if (!runChecked()) return;
    const session = state.session, generation = state.selection, runId = state.runId;
    resetArtifacts();
    const version = state.artifactVersion;
    state.artifactStatus = "loading"; renderArtifacts();
    const current = () => session === state.session && generation === state.selection && runId === state.runId && version === state.artifactVersion;
    try {
      const result = await request("GET", `${pathForRun()}/artifacts`);
      if (!current()) return;
      if (!Array.isArray(result.items) || result.items.some((item) => !item || typeof item.artifact_id !== "string" || item.run_id !== runId)) throw Object.assign(new Error(), {code: "BAD_JSON"});
      state.artifacts = result.items; state.artifactStatus = "ready"; state.artifactsSelection = generation;
      const tasks = state.review?.tasks || [];
      state.targetId = tasks.find((task) => task.id === preferredTarget)?.id || tasks.find((task) => task.input_artifacts?.length)?.id || tasks[0]?.id || "";
      renderArtifacts();
      if (state.targetId) loadContext();
      if (preferredId && state.artifacts.some((item) => item.artifact_id === preferredId)) loadArtifactDetail(preferredId);
    } catch (error) {
      if (!current() || error.code === "SESSION_CHANGED") return;
      state.artifactStatus = "error"; state.artifactError = publicError(error); renderArtifacts();
    }
  }

  async function loadArtifactDetail(id) {
    if (!runChecked() || state.artifactStatus !== "ready" || !state.artifacts.some((item) => item.artifact_id === id)) return;
    const session = state.session, generation = state.selection, runId = state.runId, version = ++state.detailVersion;
    state.artifactId = id; state.artifactDetail = null; state.detailStatus = "loading"; $("auxiliary-reviewed").checked = false; renderArtifacts();
    const current = () => session === state.session && generation === state.selection && runId === state.runId && version === state.detailVersion && id === state.artifactId;
    try {
      const result = await request("GET", `${pathForRun()}/artifacts/${encodeURIComponent(id)}`);
      if (!current()) return;
      if (!sameArtifact(result, state.artifacts.find((item) => item.artifact_id === id)) || result.run_id !== runId || !result.body || typeof result.body !== "object") throw Object.assign(new Error(), {code: "BAD_JSON"});
      state.artifactDetail = result; state.detailStatus = "ready"; renderArtifacts();
    } catch (error) {
      if (!current() || error.code === "SESSION_CHANGED") return;
      state.detailStatus = "error"; state.detailError = publicError(error); renderArtifacts();
    }
  }

  async function loadContext() {
    if (!runChecked() || !targetTask()) return;
    const session = state.session, generation = state.selection, runId = state.runId, targetId = state.targetId, version = ++state.contextVersion;
    state.context = null; state.contextStatus = "loading"; state.contextBindingChanged = false; $("auxiliary-reviewed").checked = false; renderContext(); refreshArtifactControls();
    const current = () => session === state.session && generation === state.selection && runId === state.runId && targetId === state.targetId && version === state.contextVersion;
    try {
      const result = await request("GET", `${pathForRun()}/tasks/${encodeURIComponent(targetId)}/context-preview`);
      if (!current()) return;
      if (result.target_epoch !== targetTask()?.epoch || result.plan_digest !== state.plan?.digest) {
        state.contextBindingChanged = true;
        throw Object.assign(new Error(), {code: "CONTEXT_BINDING_CHANGED"});
      }
      if (result.run_id !== runId || result.task_id !== targetId || !Array.isArray(result.items) || !Array.isArray(result.missing_inputs)
          || !hashPattern.test(result.context_digest) || typeof result.ready !== "boolean" || result.untrusted_data !== true || result.execution_connected !== false
          || !integer(result.bytes) || result.max_bytes !== 65536 || result.bytes > result.max_bytes) throw Object.assign(new Error(), {code: "BAD_JSON"});
      state.context = result; state.contextStatus = "ready"; renderContext(); refreshArtifactControls();
    } catch (error) {
      if (!current() || error.code === "SESSION_CHANGED") return;
      state.contextStatus = "error"; state.contextError = error.code === "CONTEXT_BINDING_CHANGED"
        ? "目标 轮次 或计划摘要已改变，批准已停用。请点击工作台“刷新”重新读取运行和合同后再审阅。" : publicError(error);
      renderContext(); refreshArtifactControls();
    }
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
        head.append(node("span", "mono", `#${str(event.seq)}`), node("strong", "", label(event.type)));
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
    sourcePanel.sync();
    if (name === "events") loadEvents();
    if (name === "artifacts" && state.artifactStatus === "idle") loadArtifacts();
    progression.sync();
    if (name === "progression") progression.load();
    localExecution.sync();
  }

  async function selectRun(runId, tab = state.tab) {
    const generation = ++state.selection;
    state.pendingRunId = runId;
    state.loading = true;
    resetArtifacts();
    refreshControls();
    $("run-content").classList.add("loading");
    $("control-reason").value = "";
    $("resume-preview-state").textContent = "检查不会恢复运行或发起模型调用。";
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
      if ((snapshot.value.id || snapshot.value.run_id) !== runId) throw Object.assign(new Error(), {code: "BAD_JSON"});
      state.runId = runId;
      state.loadedSelection = generation;
      state.goal = goal.status === "fulfilled" ? goal.value : null;
      state.plan = plan.status === "fulfilled" ? plan.value : null;
      state.snapshot = snapshot.value;
      state.review = review.status === "fulfilled" ? review.value : null;
      renderRun();
      switchTab(tab);
    } catch (error) {
      if (generation !== state.selection || error.code === "SESSION_CHANGED") return;
      state.runId = null;
      state.loadedSelection = 0;
      state.goal = state.snapshot = state.plan = state.review = null;
      renderRun();
      notify(publicError(error), true);
    } finally {
      if (generation === state.selection) {
        state.loading = false;
        state.pendingRunId = null;
        $("run-content").classList.remove("loading");
        renderGoals();
        refreshControls();
        if (state.tab === "artifacts" && state.artifactStatus === "idle") loadArtifacts();
      }
    }
  }

  async function refreshAll(selectId = state.runId) {
    const session = state.session;
    const generation = ++state.selection;
    state.loading = true;
    resetArtifacts();
    refreshControls();
    $("goals-state").textContent = "正在读取目标…";
    try {
      const [goals, interventions] = await Promise.allSettled([request("GET", "/v1/goals"), request("GET", "/v1/interventions")]);
      if (session !== state.session || generation !== state.selection) return;
      state.interventionsError = interventions.status !== "fulfilled" || !Array.isArray(interventions.value.items);
      if (!state.interventionsError) {
        state.interventions = interventions.value.items;
        if (state.noticeKind === "interventions") notify("人工待办已重新读取。");
      } else notify("人工待办读取失败，当前数量未知。其他运行信息仍可查看，请重新读取待办后再处理。", true, "interventions");
      renderInterventions();
      if (goals.status !== "fulfilled") throw goals.reason;
      state.goals = Array.isArray(goals.value.goals) ? goals.value.goals : [];
      renderGoals(); renderInterventions();
      if (selectId) await selectRun(selectId);
      else if (state.goals.length) await selectRun(state.goals[0].run_id);
      else { state.runId = null; state.loadedSelection = 0; state.snapshot = state.goal = state.plan = state.review = null; renderRun(); }
    } catch (error) {
      if (session !== state.session || generation !== state.selection || error.code === "SESSION_CHANGED") return;
      $("goals-state").textContent = "目标读取失败。";
      notify(publicError(error), true);
    } finally {
      if (session === state.session && generation === state.selection) {
        state.loading = false;
        refreshControls();
      }
    }
  }

  async function mutate(method, path, body, successMessage, after) {
    if (state.busy) return;
    const target = path.match(/^\/v1\/(?:runs|goals)\/([^/]+)(?:\/|$)/);
    const displayedId = state.snapshot?.id || state.snapshot?.run_id;
    if (state.loading || (target && (state.loadedSelection !== state.selection
        || displayedId !== state.runId || decodeURIComponent(target[1]) !== state.runId))) {
      notify("当前运行尚未完成核对，未发送写入请求。请等待加载完成后重新确认目标与操作。", true);
      return;
    }
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

  async function connect() {
    const session = ++state.session;
    $("login-button").disabled = true;
    notify("正在连接本机工作台…");
    try {
      const principal = await request("GET", "/v1/me");
      if (session !== state.session) return;
      if (!principal || !Array.isArray(principal.roles) || !Array.isArray(principal.capabilities)) throw Object.assign(new Error(), {code: "BAD_JSON"});
      state.principal = principal;
      window.LoomPreview?.configure(principal, () => mutate("POST", "/preview/examples", {}, "体验草案已创建，请在“计划”中审阅并批准，再按“推进指引”执行。", async result => { await refreshAll(result.run_id); }));
      $("identity-label").textContent = `${principal.authentication === "public-session" ? "受邀体验 · " : principal.authentication === "local-console" ? "本机开发 · " : ""}${principal.roles.map((role) => roleNames[role] || role).join(" / ")}`;
      $("login-panel").hidden = true;
      $("workspace").hidden = false;
      $("logout-button").hidden = $("refresh-button").hidden = false;
      refreshControls();
      notify(window.LoomPreview?.active ? "已连接公网体验工作台。共享空间最多 20 个目标，单目标预算上限 $1；保守预留，按实际用量记账。" : "已连接本机工作台。");
      await refreshAll();
      $("workspace").focus();
    } catch (error) {
      if (session !== state.session) return;
      logout(false);
      if (error.status === 401 && window.LoomPreview?.active) { window.location.assign("/preview/login"); return; }
      notify(publicError(error), true);
    } finally { if (session === state.session) $("login-button").disabled = false; }
  }
  $("login-form").addEventListener("submit", (event) => { event.preventDefault(); connect(); });
  $("logout-button").addEventListener("click", async () => {
    try { if (await window.LoomPreview?.logout()) return; logout(); }
    catch { notify("退出未确认，请重试；停止公网进程可使全部体验会话失效。", true); }
  });
  $("refresh-button").addEventListener("click", () => refreshAll());
  $("goal-search").addEventListener("input", renderGoals);
  $("artifacts-refresh").addEventListener("click", () => { if (!state.busy) loadArtifacts(); });
  $("context-refresh").addEventListener("click", () => { if (!state.busy) loadContext(); });
  $("input-target").addEventListener("change", () => {
    if (state.busy || !runChecked()) return;
    state.targetId = $("input-target").value;
    $("auxiliary-reviewed").checked = false; renderArtifacts(); loadContext();
  });
  $("auxiliary-reviewed").addEventListener("change", refreshArtifactControls);
  $("auxiliary-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const body = approvalBody();
    if (!body || !$("auxiliary-reviewed").checked) { notify("候选、目标或读取版本尚未核对，未发送辅助输入批准。请重新读取并审阅。", true); return; }
    mutate("POST", `${pathForRun()}/tasks/${encodeURIComponent(state.targetId)}/auxiliary-inputs`, body,
      "辅助输入批准已记录，仅作为不可信数据参考；未验证候选、未放行依赖，本操作不发起执行。实际绑定看人工待办中的执行记录。");
  });
  document.querySelectorAll("[data-tab]").forEach((button) => button.addEventListener("click", () => switchTab(button.dataset.tab)));
  $("show-interventions").addEventListener("click", () => {
    if (state.interventionsError) { notify("人工待办读取失败，当前数量未知。请点击工作台“刷新”重新读取。", true, "interventions"); return; }
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
    if (state.loading || state.loadedSelection !== state.selection) return;
    if (file.size > 262144) { notify("计划文件不能超过 256 KiB。", true); $("plan-file").value = ""; return; }
    const generation = state.selection;
    const session = state.session, runId = state.runId;
    try {
      const text = await file.text();
      const definition = JSON.parse(text);
      if (!definition || Array.isArray(definition) || typeof definition !== "object") throw new Error();
      if (session === state.session && generation === state.selection && runId === state.runId && !state.loading) {
        $("plan-definition").value = JSON.stringify(definition, null, 2); $("plan-reviewed").checked = false;
      }
    } catch {
      if (session === state.session && generation === state.selection && runId === state.runId) notify("计划文件不是有效的 JSON 对象。", true);
    }
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
    if (state.plan.definition?.nodes?.some((task) => task.development_suite) && !has("suite.approve")) {
      notify("此计划引用审阅套件，需要具名操作员的套件批准权限。", true); return;
    }
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
  $("resume-preview-button").addEventListener("click", async () => {
    if (!state.runId || !runChecked()) return;
    const runId = state.runId, selection = state.selection;
    $("resume-preview-state").textContent = "正在检查恢复条件…";
    try {
      const result = await request("GET", `/v1/runs/${encodeURIComponent(runId)}/resume-preview`);
      if (selection !== state.selection || runId !== state.runId) return;
      const reasons = {
        RUN_NOT_PAUSED: "当前运行不处于暂停或待人工处理状态。",
        PLAN_NOT_APPROVED: "工作流计划尚未批准。",
        UNKNOWN_EFFECTS: "存在结果未知的调用，请先核对。",
        UNSETTLED_RESERVATIONS: "存在未结算的预算预留，请先核对。",
        RUN_BUDGET_EXCEEDED: "运行实际用量超过已批准预算。",
        TASK_BUDGET_EXCEEDED: "任务实际用量超过已批准预算。",
        ACTIVE_OWNER: "任务执行者仍持有有效租约，请等待其停止。",
        RETRY_CONTRACT_MISSING: "缺少已批准的任务重试合同。",
        ATTEMPTS_EXHAUSTED: "任务已用尽批准的尝试次数。",
        PLAN_BINDING_INVALID: "计划或任务记录不一致，请检查审计记录。",
      };
      $("resume-preview-state").textContent = result.ready === true && Number.isInteger(result.retry_task_count)
        ? `目前满足恢复条件，恢复后 ${result.retry_task_count} 个任务可重新排队。实际操作时会再次检查；此检查不代表验收通过。`
        : `${reasons[result.reason] || "无法确认恢复条件，请刷新后重试。"} 此检查不会修改运行。`;
    } catch {
      if (selection === state.selection && runId === state.runId) $("resume-preview-state").textContent = "恢复条件读取失败，请刷新后重试。";
    }
  });
  $("terminate-button").addEventListener("click", () => control("terminate"));
  $("effect-decision").addEventListener("change", () => {
    if ($("effect-decision").value === "not_sent") { $("effect-cost").value = "0"; $("effect-tokens").value = "0"; }
  });
  $("effect-select").addEventListener("change", syncEffectEvidence);
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
  connect();
})();
