"use strict";

// Review data stays in this page's memory. The server owns compilation,
// canonical hashes, identity, source freshness and all authority decisions.
window.LoomSuitePanel = function ({state, $, node, has, request, mutate, notify, publicError,
  exactBodyText, sameArtifact, runChecked, label}) {
  const hash = /^[0-9a-f]{64}$/;
  let binding = "", generation = 0, pending = false, preview = null, frozen = null, draft = null;
  let selected = [], error = "", receiptId = "";
  const currentItem = () => state.artifactDetail;
  const operator = () => has("suite.approve") && state.principal?.roles?.includes("operator");
  const ready = () => runChecked() && !state.busy && state.detailStatus === "ready"
    && state.artifactStatus === "ready" && state.artifactsSelection === state.selection
    && state.snapshot?.status === "open" && currentItem()?.stale === false
    && sameArtifact(currentItem(), state.artifacts.find((item) => item.artifact_id === state.artifactId));
  const root = () => `/v1/runs/${encodeURIComponent(state.runId)}`;
  const candidatePath = () => `${root()}/artifacts/${encodeURIComponent(state.artifactId)}`;
  const suitePath = (id) => `${root()}/development-suites/${encodeURIComponent(id)}?include_body=true`;
  const sourceMatches = (source) => {
    const item = currentItem();
    return source && item && source.tenant_id === state.principal?.tenant_id && source.run_id === state.runId
      && source.artifact_id === item.artifact_id && source.task_id === item.source_task_id
      && source.node === item.source_node && source.epoch === item.source_epoch && source.revision === item.revision
      && source.plan_digest === item.plan_digest && source.body_hash === item.content_hash
      && source.event_hash === item.source_event_hash && source.schema_version === item.schema_version
      && source.origin === item.origin;
  };
  const sameNames = (a, b) => Array.isArray(a) && Array.isArray(b) && JSON.stringify(a) === JSON.stringify(b);
  const exactCasesAvailable = () => {
    try {
      const body = currentItem()?.body;
      if (body?.schema_version !== "loom.public-checks/v1" || body.visibility !== "development"
          || !Array.isArray(body.cases) || !body.cases.length || body.cases.length > 32) return false;
      return body.cases.every((item) => typeof item.name === "string" && typeof exactBodyText(item) === "string");
    } catch { return false; }
  };
  const validReview = (value, names) => value?.schema_version === "loom.development-suite-review/v1"
    && sourceMatches(value.source) && sameNames(value.selected_case_names, names) && value.case_count === names.length
    && value.compiler_version === "loom.public-checks-compiler/v1"
    && ["review_digest", "development_digest", "compilation_digest", "selected_body_hash"].every((key) => hash.test(value[key]))
    && ["execution_connected", "confers_authority", "producer_verified", "independently_verified", "counts_as_success"].every((key) => value[key] === false);

  function invalidateDraft() {
    draft = null;
    $("suite-draft-result").hidden = true;
    $("suite-draft-json").textContent = "";
  }
  function reset() {
    generation++;
    binding = ""; pending = false; preview = frozen = null; selected = []; error = ""; receiptId = "";
    invalidateDraft();
    $("suite-panel").hidden = true;
    for (const id of ["suite-cases", "suite-review", "suite-frozen", "suite-history-list"]) $(id).replaceChildren();
    for (const id of ["suite-state", "suite-history-state"]) $(id).textContent = "";
    $("suite-reviewed").checked = false;
    $("suite-draft-form").reset();
  }
  function controls() {
    const available = ready() && exactCasesAvailable() && !pending;
    $("suite-preview").disabled = !available || !selected.length || Boolean(receiptId);
    $("suite-history").disabled = !available;
    $("suite-freeze-form").hidden = !operator();
    const canFreeze = available && operator() && preview && !frozen && !receiptId;
    $("suite-reviewed").disabled = !canFreeze;
    $("suite-freeze").disabled = !canFreeze || !$("suite-reviewed").checked;
    const canPrepare = available && operator() && frozen?.stale === false && Boolean(frozen.execution_reference);
    $("suite-draft-form").hidden = !operator() || !frozen;
    $("suite-prepare").disabled = !canPrepare;
    $("suite-download").disabled = !canPrepare || !draft;
    for (const input of $("suite-cases").querySelectorAll("input")) input.disabled = !available;
    for (const button of $("suite-history-list").querySelectorAll("button")) button.disabled = !available;
    for (const input of $("suite-draft-form").querySelectorAll("input, textarea, select")) input.disabled = !canPrepare;
    $("suite-state").textContent = pending ? "正在核对服务端记录…" : error || (!exactCasesAvailable()
      ? "当前浏览器无法原样显示用例 JSON，审阅冻结已禁用。请更新浏览器并重新读取候选。"
      : !ready() ? "候选读取中、已失效或运行不开放，请重新读取核对。"
      : receiptId && !frozen?.source ? "冻结回执已确认，当前记录尚未重新核对，请读取冻结记录。"
      : frozen ? "已读取冻结记录。冻结不授予执行权，也不表示检查已经运行。"
      : preview ? "服务端摘要已核对，请先逐项审阅，再由具名操作员显式冻结。"
      : "勾选后按原始顺序审阅用例，再核对服务端摘要。只读身份可预览，冻结需要具名操作员。");
  }
  function displayReview(value, container) {
    container.replaceChildren();
    container.append(node("p", "mono break-word", `来源运行 ${value.source.run_id} · 节点 ${value.source.node} · 轮次 ${value.source.epoch} · 修订 ${value.source.revision}`));
    container.append(node("p", "field-help", `来源 ${label(value.source.origin)} · 选中 ${value.case_count} 项：${value.selected_case_names.join(" / ")}`));
    for (const [key, label] of [["plan_digest", "原计划摘要"], ["body_hash", "原始正文摘要"], ["event_hash", "原始来源事件"]]) {
      container.append(node("p", "mono break-word", `${label} ${value.source[key]}`));
    }
    for (const [key, label] of [["review_digest", "审阅摘要"], ["compiler_version", "编译版本"], ["selected_body_hash", "选中正文摘要"],
      ["development_digest", "开发套件摘要"], ["compilation_digest", "编译摘要"]]) {
      container.append(node("p", "mono break-word", `${label} ${value[key]}`));
    }
  }
  function sync() {
    const item = currentItem();
    const next = state.detailStatus === "ready" && item?.kind === "public-checks"
      ? JSON.stringify([state.session, state.selection, state.detailVersion, item.artifact_id, item.content_hash]) : "";
    if (next !== binding) {
      reset(); binding = next;
      if (binding) {
        selected = (item.body?.cases || []).map((item) => item.name);
        const cases = $("suite-cases");
        if (exactCasesAvailable()) for (const [index, test] of item.body.cases.entries()) {
          const row = node("article", "suite-case"), label = node("label", "check-label");
          const input = node("input"); input.type = "checkbox"; input.checked = true; input.dataset.caseName = test.name;
          label.append(input, node("span", "", `${index + 1}. ${test.name}`));
          row.append(label, node("pre", "", exactBodyText(test))); cases.append(row);
          input.addEventListener("change", () => {
            if (input.disabled) return;
            selected = Array.from(cases.querySelectorAll("input:checked"), (input) => input.dataset.caseName);
            generation++; preview = frozen = null; error = ""; receiptId = ""; invalidateDraft();
            $("suite-reviewed").checked = false; $("suite-review").replaceChildren(); $("suite-frozen").replaceChildren(); controls();
          });
        }
      }
    }
    $("suite-panel").hidden = !binding;
    controls();
  }
  async function read(action) {
    if (!ready() || !exactCasesAvailable() || pending) return;
    const ticket = ++generation, currentBinding = binding;
    pending = true; error = ""; controls();
    try {
      await action(() => generation === ticket && binding === currentBinding && runChecked());
    } catch (failure) {
      if (generation === ticket && binding === currentBinding) {
        preview = frozen = null; invalidateDraft();
        $("suite-reviewed").checked = false;
        $("suite-review").replaceChildren(); $("suite-frozen").replaceChildren();
        if (receiptId) $("suite-frozen").append(node("p", "field-help", `冻结回执已记录：${receiptId}。当前记录尚未重新核对，可从冻结记录恢复，不必重复冻结。`));
        if ($("suite-history-state").textContent.startsWith("正在读取台账")) $("suite-history-state").textContent = "冻结记录读取失败，数量未知；请重新读取。";
        error = publicError(failure); notify(error, true);
      }
    } finally { if (generation === ticket) { pending = false; controls(); } }
  }
  function badJSON() { throw Object.assign(new Error(), {code: "BAD_JSON"}); }
  async function getFrozen(id) {
    const value = await request("GET", suitePath(id));
    const names = value.selected_case_names;
    const original = currentItem()?.body?.cases || [];
    if (!Array.isArray(names) || !names.length || !validReview(value, names) || value.frozen !== true
        || value.suite_id !== id || !hash.test(value.suite_digest) || !hash.test(value.freeze_event_hash)
        || typeof value.stale !== "boolean" || typeof value.actor !== "string"
        || !value.actor.startsWith("subject:") || !sameNames(original.filter((test) => names.includes(test.name)).map((test) => test.name), names)
        || !sameNames(value.body?.cases?.map((test) => test.name), names)) badJSON();
    exactBodyText(value.body);
    if (!value.stale) {
      const ref = value.execution_reference;
      const expected = {schema_version: "loom.development-suite-ref/v1", source_run_id: state.runId};
      for (const key of ["suite_id", "suite_digest", "freeze_event_hash", "review_digest", "compiler_version", "development_digest", "selected_body_hash"]) expected[key] = value[key];
      if (!ref || Object.keys(ref).length !== Object.keys(expected).length || Object.keys(expected).some((key) => ref[key] !== expected[key])) badJSON();
    }
    return value;
  }
  function showFrozen(value) {
    frozen = value; receiptId = value.suite_id; invalidateDraft(); $("suite-reviewed").checked = false;
    const display = $("suite-frozen"); displayReview(value, display);
    display.prepend(node("h3", "", value.stale ? "冻结记录 · 来源已失效" : "冻结记录 · 仅开发检查"));
    display.append(node("p", "mono break-word", `套件 ${value.suite_id} · 审阅者 ${value.actor}`));
    display.append(node("p", "mono break-word", `冻结事件 ${value.freeze_event_hash}`));
    display.append(node("pre", "", exactBodyText(value.body)));
    if (value.stale) display.append(node("p", "inline-error", "来源已失效，不能生成新的执行草案。重新读取并审阅当前候选。"));
    else display.append(node("pre", "", JSON.stringify(value.execution_reference, null, 2)));
  }
  $("suite-preview").addEventListener("click", () => read(async (current) => {
    if (receiptId) return;
    preview = frozen = null; invalidateDraft(); $("suite-reviewed").checked = false;
    $("suite-review").replaceChildren(); $("suite-frozen").replaceChildren();
    const names = [...selected]; if (!names.length) return;
    const query = new URLSearchParams(); for (const name of names) query.append("case", name);
    const value = await request("GET", `${candidatePath()}/development-preview?${query}`);
    if (!current()) return;
    if (!validReview(value, names) || value.frozen !== false) badJSON();
    preview = value; displayReview(value, $("suite-review"));
  }));
  $("suite-reviewed").addEventListener("change", controls);
  $("suite-freeze-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!ready() || pending || !operator() || !preview || frozen || receiptId || !$("suite-reviewed").checked || !exactCasesAvailable()) return;
    const reviewed = preview, ticket = generation, currentBinding = binding;
    $("suite-reviewed").checked = false;
    let acknowledged = false;
    await mutate("POST", `${candidatePath()}/development-suites`, {expected_review_digest: reviewed.review_digest, selected_case_names: [...selected]},
      "审阅冻结已记录。未批准执行计划，未调用模型。", async (result) => {
        if (ticket !== generation || binding !== currentBinding) return;
        acknowledged = true;
        receiptId = result.suite_id;
        frozen = {suite_id: result.suite_id, stale: true};
        $("suite-frozen").replaceChildren(node("p", "field-help", `冻结回执已记录：${result.suite_id}。正在重新核对来源；失败时可从冻结记录重新读取，不必重复冻结。`));
        // The POST receipt can be replayed after the source changes. Always GET
        // the authoritative current view before enabling draft preparation.
        const value = await getFrozen(result.suite_id);
        if (ticket === generation && binding === currentBinding) showFrozen(value);
      });
    if (ticket === generation && binding === currentBinding && !acknowledged) {
      preview = null; $("suite-review").replaceChildren();
      error = "冻结结果尚未确认，请先读取冻结记录；重新核对相同用例后重提会沿用本页面原幂等键。";
    }
    controls();
  });
  $("suite-history").addEventListener("click", () => read(async (current) => {
    $("suite-history-list").replaceChildren(); $("suite-history-state").textContent = "正在读取台账…";
    const events = await request("GET", `${root()}/events`); if (!current()) return;
    if (!Array.isArray(events.events)) badJSON();
    const records = events.events.filter((event) => event.type === "development.suite_frozen" && event.payload?.source?.artifact_id === state.artifactId).slice(-100).reverse();
    $("suite-history-state").textContent = records.length ? `最近 ${records.length} 条冻结记录，点击后重新核对当前来源。` : "此候选尚无冻结记录。";
    for (const record of records) {
      const button = node("button", "small", `${record.payload.suite_id} · ${record.payload.case_count} 项`); button.type = "button";
      button.addEventListener("click", () => read(async (current) => {
        const value = await getFrozen(record.payload.suite_id); if (current()) showFrozen(value);
      })); $("suite-history-list").append(button);
    }
  }));
  $("suite-draft-form").addEventListener("input", invalidateDraft);
  $("suite-draft-form").addEventListener("change", invalidateDraft);
  $("suite-draft-form").addEventListener("submit", (event) => {
    event.preventDefault();
    if (!operator() || !frozen || frozen.stale) return;
    const title = $("suite-draft-title").value.trim(), instruction = $("suite-draft-instruction").value.trim();
    const host = $("suite-host-digest").value.trim(), cost = Number($("suite-cost").value), tokens = Number($("suite-tokens").value), repairs = Number($("suite-repairs").value);
    if (!title || title.length > 500 || !instruction || instruction.length > 16000 || !hash.test(host) || /^0+$/.test(host)
        || host === frozen.development_digest || !Number.isSafeInteger(cost) || cost < 0 || cost > 999999999000
        || !Number.isSafeInteger(tokens) || tokens < 1 || tokens > 10000000 || ![1, 2].includes(repairs)) {
      notify("请填写新任务需求、不同于开发套件的真实 宿主终验 摘要，以及有效的总预算和修复次数。", true); return;
    }
    const inputSnapshot = JSON.stringify([title, instruction, host, cost, tokens, repairs]);
    read(async (current) => {
      invalidateDraft(); const value = await getFrozen(frozen.suite_id); if (!current()) return;
      if (value.stale) { showFrozen(value); return; }
      if (inputSnapshot !== JSON.stringify([$("suite-draft-title").value.trim(), $("suite-draft-instruction").value.trim(), $("suite-host-digest").value.trim(), Number($("suite-cost").value), Number($("suite-tokens").value), Number($("suite-repairs").value)])) return;
      // Role contracts contain strings only. Do not copy source nodes, body,
      // model provenance, approval or original acceptance into the new plan.
      const roles = state.plan?.definition?.roles;
      if (!Array.isArray(roles) || roles.length !== 7) badJSON();
      draft = {schema_version: "loom.workflow/v1", pack: "software-dev", scope: "local-workspace", roles,
        nodes: [{id: "implement", title, instruction, role: "developer", depends: [], priority: 10, channel: "business",
          evaluation_digest: host, development_suite: value.execution_reference,
          repair: {schema_version: "loom.rebind/v1", development_digest: value.development_digest, max_repairs: repairs, stop_on_duplicate_source: true},
          limits: {cost_limit_microusd: cost, token_limit: tokens, max_attempts: 1 + repairs}},
        {id: "independent_review", title: "独立验收复核", instruction: "交付前独立审阅原始证据，未取得独立验证前不得判定成功。", role: "validator", depends: ["implement"], priority: 0, channel: "verification",
          limits: {cost_limit_microusd: 1000, token_limit: 1000, max_attempts: 1}}]};
      $("suite-draft-json").textContent = JSON.stringify(draft, null, 2);
      $("suite-draft-help").textContent = `未保存、未批准的草案。新目标预算至少覆盖任务及独立复核：${cost + 1000} µUSD / ${tokens + 1000} 词元。下载后在“新建”目标的“计划”页导入、保存并单独审阅批准。宿主终验 用例仍须由本机执行器持有并核对摘要；当前双签待配置，不能最终成功。`;
      $("suite-draft-result").hidden = false;
    });
  });
  $("suite-download").addEventListener("click", () => {
    if (!draft || !operator() || !ready() || pending || frozen?.stale !== false) return;
    const blob = new Blob([JSON.stringify(draft, null, 2) + "\n"], {type: "application/json"});
    const url = URL.createObjectURL(blob), link = node("a");
    link.href = url; link.download = "loom-reviewed-plan-draft.json"; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  return {sync, reset};
};
