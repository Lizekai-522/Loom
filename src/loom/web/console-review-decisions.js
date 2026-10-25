"use strict";

window.LoomReviewDecisions = function ({state, $, node, has, request, publicError, sameArtifact, runChecked}) {
  const names = {defer: "保存待办", await_independent_review: "登记独立复核待办", approve_fixer: "批准单次修复输入"};
  const states = {awaiting_operator: "待操作员处理", awaiting_independent_models: "等待独立模型复核", fixer_input_approved: "已批准修复输入"};
  let binding = "", generation = 0, pending = false, preview = null, history = [], error = "", receipt = null, attempt = null;
  const item = () => state.artifactDetail;
  const operator = () => has("control") && state.principal?.roles?.includes("operator");
  const ready = () => runChecked() && !state.busy && state.detailStatus === "ready" && state.artifactStatus === "ready"
    && state.artifactsSelection === state.selection && sameArtifact(item(), state.artifacts.find((c) => c.artifact_id === state.artifactId));
  const path = () => `/v1/runs/${encodeURIComponent(state.runId)}/artifacts/${encodeURIComponent(state.artifactId)}`;
  const matches = (ref) => ref && ref.tenant_id === state.principal?.tenant_id && ref.run_id === state.runId
    && ref.artifact_id === item()?.artifact_id && ["source_task_id", "source_epoch", "revision", "plan_digest", "content_hash", "source_event_hash"].every((k) => ref[k] === item()?.[k]);
  const safeFlags = (v) => ["counts_as_success", "independently_verified", "model_dispatched", "task_completed"].every((k) => v?.[k] === false);
  const valid = (v) => v?.schema_version === "loom.review-decision-preview/v1" && matches(v.reference)
    && safeFlags(v) && /^[0-9a-f]{64}$/.test(v.preview_digest) && Array.isArray(v.allowed_decisions)
    && v.allowed_decisions.every((k) => Object.hasOwn(names, k)) && Array.isArray(v.fixer_targets);

  function reset() {
    generation++; binding = ""; pending = false; preview = null; history = []; error = ""; receipt = attempt = null;
    $("review-decision-panel").hidden = true; $("review-decision-form").reset();
    $("review-decision-history").replaceChildren(); $("review-decision-binding").textContent = "";
  }
  function controls() {
    const available = ready() && !pending;
    const writable = available && operator() && preview && !error && !receipt && preview.ready && preview.allowed_decisions.length > 0;
    $("review-decision-refresh").disabled = !available;
    $("review-decision-form").hidden = !operator() || Boolean(preview && !preview.allowed_decisions.length);
    const retry = Boolean(attempt && !receipt);
    for (const id of ["review-decision-action", "review-decision-target", "review-decision-reason", "review-decision-reviewed"]) $(id).disabled = !writable || retry;
    const fixing = $("review-decision-action").value === "approve_fixer";
    $("review-decision-target-row").hidden = !fixing;
    $("review-decision-submit").disabled = !writable || (!retry && (!$("review-decision-reviewed").checked || !$("review-decision-reason").value.trim()
      || (fixing && !$("review-decision-target").value)));
    $("review-decision-submit").textContent = retry ? "重试同一笔处置" : "记录处置";
    $("review-decision-state").textContent = pending ? "正在核对审查处置记录…" : error || (receipt && !preview
      ? "已收到处置回执；重新读取失败，请刷新处置记录确认。"
      : preview ? `${states[preview.state] || "状态未知"} · ${preview.history_count} / ${preview.max_decisions} 条记录${preview.stale ? " · 当前绑定已失效，禁止新处置" : ""}${attempt ? " · 上次回执未确认，可刷新核对或用同一编号重试" : ""}`
      : "请选择审查候选后读取处置记录。");
  }
  function display() {
    const action = $("review-decision-action"), selected = action.value;
    action.replaceChildren(...(preview?.allowed_decisions || []).map((value) => {const o = node("option", "", names[value]); o.value = value; return o;}));
    if (preview?.allowed_decisions.includes(selected)) action.value = selected;
    const target = $("review-decision-target"), selectedTarget = target.value;
    target.replaceChildren(...(preview?.fixer_targets || []).filter((t) => t.eligible).map((t) => {const o = node("option", "", `${t.title} · 轮次 ${t.epoch}`); o.value = t.task_id; return o;}));
    if ((preview?.fixer_targets || []).some((t) => t.eligible && t.task_id === selectedTarget)) target.value = selectedTarget;
    $("review-decision-binding").textContent = preview ? `审查源轮次 ${preview.reference.source_epoch} · 修订 ${preview.reference.revision}\n候选正文 ${preview.reference.content_hash}\n处置核对摘要 ${preview.preview_digest}` : "";
    const list = $("review-decision-history"); list.replaceChildren();
    if (!history.length) list.append(node("p", "muted", "尚无处置记录。"));
    for (const record of history) {
      const row = node("article", "artifact-card");
      row.append(node("h3", "", `第 ${record.revision} 次 · ${names[record.decision] || "未知处置"}`),
        node("p", "task-detail", `操作员 ${record.actor} · ${states[record.state] || "未知状态"}`), node("p", "", record.reason),
        node("p", "mono break-word", `事件 ${record.event_hash}`));
      if (record.target_task_id) row.append(node("p", "mono break-word", `修复目标 ${record.target_task_id} · 输入批准 ${record.grant_id}`));
      list.append(row);
    }
    controls();
  }
  async function load() {
    if (!binding || !ready() || pending) return;
    const version = ++generation, bound = binding, root = path();
    pending = true; error = ""; controls();
    try {
      const [p, h] = await Promise.all([request("GET", `${root}/review-decision-preview`), request("GET", `${root}/review-decisions`)]);
      if (version !== generation || bound !== binding) return;
      if (!valid(p) || !safeFlags(h) || !Array.isArray(h.items) || h.items.length > 32 || p.history_count !== h.items.length
          || p.latest_decision_id !== (h.items.at(-1)?.decision_id || null)
          || h.items.some((r) => !matches(r.reference) || !Object.hasOwn(names, r.decision))) throw Object.assign(new Error(), {code: "BAD_JSON"});
      preview = p; history = h.items;
      if (attempt && history.some((r) => r.preview_digest === attempt.body.expected_preview_digest && r.decision === attempt.body.decision
          && r.reason === attempt.body.reason && r.target_task_id === (attempt.body.target_task_id || null))) attempt = null;
      if (receipt && !history.some((r) => r.decision_id === receipt.decision_id)) throw Object.assign(new Error(), {code: "BAD_JSON"});
      receipt = null;
      $("review-decision-reviewed").checked = false;
    } catch (e) {
      if (version === generation && bound === binding && e.code !== "SESSION_CHANGED") {preview = null; error = publicError(e);}
    } finally {
      if (version === generation && bound === binding) {pending = false; display();}
    }
  }
  function sync() {
    const next = state.detailStatus === "ready" && item()?.kind === "review-findings"
      ? JSON.stringify([state.session, state.selection, state.detailVersion, item().artifact_id, item().content_hash]) : "";
    if (next !== binding) {reset(); binding = next; if (binding) {$("review-decision-panel").hidden = false; void load();}}
    controls();
  }
  $("review-decision-refresh").addEventListener("click", load);
  for (const id of ["review-decision-action", "review-decision-target", "review-decision-reviewed", "review-decision-reason"]) $(id).addEventListener("input", controls);
  $("review-decision-form").addEventListener("submit", async (e) => {
    e.preventDefault(); controls();
    if ($("review-decision-submit").disabled) return;
    const bound = binding, version = generation, root = path();
    if (!attempt) {
      const body = {expected_preview_digest: preview.preview_digest, decision: $("review-decision-action").value, reason: $("review-decision-reason").value.trim()};
      if (body.decision === "approve_fixer") body.target_task_id = $("review-decision-target").value;
      attempt = {body, key: `review-decision:${crypto.randomUUID()}`};
    }
    pending = true; error = ""; controls();
    try {
      const r = await request("POST", `${root}/review-decisions`, attempt.body, attempt.key);
      if (bound !== binding || version !== generation) return;
      if (!safeFlags(r) || !matches(r.reference) || !r.decision_id) throw Object.assign(new Error(), {code: "BAD_JSON"});
      receipt = r; attempt = null; preview = null;
      $("review-decision-reviewed").checked = false;
      $("review-decision-reason").value = "";
    } catch (err) {
      if (bound === binding && version === generation && err.code !== "SESSION_CHANGED") {
        error = publicError(err);
        // Known rejection can be re-read. Unknown transport results keep the
        // exact request and key, with fields locked until a receipt is found.
        if ([401, 403, 404, 409, 422].includes(err.status)) {attempt = null; preview = null;}
        else error = "上次提交结果尚未确认。可刷新核对或重试同一笔处置；请勿重载页面后盲目重发。";
      }
    } finally {
      if (bound === binding && version === generation) {
        pending = false;
        if (receipt) await load();
        else {if (attempt) error = ""; display();}
      }
    }
  });
  return {reset, sync};
};
