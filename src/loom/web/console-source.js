"use strict";
window.LoomSourcePanel = function ({state, node, request, publicError, runChecked}) {
  const root = document.getElementById("source-panel");
  let generation = 0, binding = "", task = null, data = null, fileHash = "", busy = false, refs = null;
  const hashPattern = /^[0-9a-f]{64}$/;
  const exact = (value, keys) => value && typeof value === "object" && !Array.isArray(value)
    && Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key));
  const stable = value => Array.isArray(value) ? value.map(stable) : value && typeof value === "object"
    ? Object.fromEntries(Object.keys(value).sort().map(key => [key, stable(value[key])])) : value;
  const canonical = value => JSON.stringify(stable(value));
  const isDocument=value=>value?.subject?.schema_version==='loom.review-subject-ref/v2';
  const filename=value=>isDocument(value)?'artifact.json':'main.py';
  async function hash(value) {
    const bytes = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
    return [...new Uint8Array(bytes)].map(byte => byte.toString(16).padStart(2, "0")).join("");
  }
  const selection = () => JSON.stringify([state.session, state.selection, state.runId]);
  const current = () => binding && binding === selection() && runChecked();
  function reset() {
    generation++; binding = ""; task = data = refs = null; fileHash = ""; busy = false;
    root.replaceChildren(); root.hidden = true;
    root.removeAttribute("aria-busy");
  }
  function controls() {
    if (!refs) return;
    root.setAttribute("aria-busy", busy ? "true" : "false");
    const enabled = current() && state.tab === "tasks" && !state.busy && !busy;
    refs.read.disabled = !enabled;
    refs.source.disabled = refs.record.disabled = !enabled || !data;
    refs.contextDownload.disabled = !enabled || !data?.files?.['review-context.json'];
  }
  function sync() { if (binding && binding !== selection()) reset(); else controls(); }
  async function validate(value) {
    const subject = value?.subject, summary = value?.execution_summary;
    const document=isDocument(value),file=filename(value);
    const keys=["schema_version", "run_id", "task_id", "epoch", "plan_digest", "content_hash", "source_sha256", "evaluation_digest", "evaluation_event_hash", "submission_event_hash"];
    if(document)keys.push('artifact_id','artifact_kind','artifact_schema_version','artifact_content_hash');
    const model=task.artifact?.model_provenance;
    const typed=document&&['requirements','design','public-checks','review-findings'].includes(subject.artifact_kind)
      &&subject.artifact_id===model?.artifact_id&&subject.artifact_kind===task.role_output?.kind
      &&subject.artifact_schema_version===task.role_output?.schema_version&&subject.artifact_content_hash===model?.artifact_content_hash;
    if (value?.schema_version !== "loom.review-input/v1" || value.source_included !== true
        || value.hidden_tests_included !== false || value.untrusted_source !== true
        || value.independently_verified !== false || value.counts_as_success !== false || value.execution_authorized !== false
        || !exact(subject, keys)
        || !exact(summary, ["cases_executed", "cases_passed", "hard_criteria_passed", "report_digest", "evidence_source"])
        || !(document?typed:subject.schema_version === "loom.review-subject-ref/v1") || subject.run_id !== state.runId || subject.task_id !== task.id
        || !Number.isSafeInteger(subject.epoch) || subject.epoch !== task.epoch
        || subject.plan_digest !== state.plan?.digest || subject.content_hash !== task.artifact?.content_hash
        || (!document&&subject.evaluation_digest !== task.evaluation_digest) || !hashPattern.test(value.input_digest)
        || ["plan_digest", "content_hash", "source_sha256", "evaluation_digest", "evaluation_event_hash", "submission_event_hash"].some(key => !hashPattern.test(subject[key]))
        || !value.files || !exact(value.files,subject.artifact_kind==='review-findings'?['artifact.json','review-context.json']:[file]) || typeof value.files[file] !== "string"
        || (subject.artifact_kind==='review-findings'&&(typeof value.files['review-context.json']!=='string'||!value.files['review-context.json'].trim()||new TextEncoder().encode(value.files['review-context.json']).length>65536))
        || !value.files[file].trim() || new TextEncoder().encode(value.files[file]).length > 65536
        || !Number.isSafeInteger(summary?.cases_executed) || summary.cases_executed < (document?0:1) || summary.cases_executed > (document?0:32)
        || !Number.isSafeInteger(summary.cases_passed) || summary.cases_passed < 0 || summary.cases_passed > summary.cases_executed
        || typeof summary.hard_criteria_passed !== "boolean" || !hashPattern.test(summary.report_digest)
        || summary.evidence_source !== (document?'local-executor-typed-schema':"local-executor-host-frozen-tests")) throw Object.assign(new Error(), {code: "BAD_JSON"});
    const context = {schema_version: value.schema_version, subject, execution_summary: summary, files: value.files};
    if (new TextEncoder().encode(canonical(context)).length>65536 || await hash(canonical(value.files)) !== subject.source_sha256 || await hash(canonical(context)) !== value.input_digest)
      throw Object.assign(new Error(), {code: "BAD_JSON"});
    return hash(value.files[file]);
  }
  function show(value) {
    const file=filename(value);refs.code.textContent = value.files[file];refs.source.textContent='下载 '+file;
    refs.contextSection.hidden=!value.files['review-context.json'];
    refs.contextCode.textContent=value.files['review-context.json']||'';
    refs.details.replaceChildren();
    const summary = value.execution_summary;
    refs.status.textContent = isDocument(value)?'精确版本已核对 · 结构格式有效，未执行测试用例 · 独立复核结果请在复核页核对':`精确版本已核对 · 宿主终验 ${summary.cases_passed}/${summary.cases_executed} · 独立复核结果请在复核页核对`;
    for (const [name, text] of [["任务", value.subject.task_id], ["轮次", value.subject.epoch], ["输入版本摘要", value.input_digest],
      ["文件合同摘要", value.subject.source_sha256], [file+" 文件 SHA256", fileHash]]) {
      refs.details.append(node("p", "mono break-word", `${name} ${text}`));
    }
  }
  function download(bytes, name, type) {
    const url = URL.createObjectURL(new Blob([bytes], {type}));
    const link = document.createElement("a"); link.href = url; link.download = name;
    document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  async function load(kind = null) {
    if (!current() || busy || state.busy || state.tab !== "tasks" || (kind && !data)) return;
    const version = ++generation, bound = binding, prior = data?.input_digest;
    const path = `/v1/runs/${encodeURIComponent(state.runId)}/tasks/${encodeURIComponent(task.id)}/review-input?include_source=true`;
    busy = true; refs.status.textContent = kind ? "正在重新核对导出版本…" : "正在读取并核对源码…"; controls();
    try {
      const value = await request("GET", path);
      if (version !== generation || bound !== binding || !current() || state.tab !== "tasks") return;
      const verifiedHash = await validate(value);
      if (version !== generation || bound !== binding || !current() || state.tab !== "tasks") return;
      if (kind && prior !== value.input_digest) throw Object.assign(new Error(), {code: "SOURCE_CHANGED"});
      data = value; fileHash = verifiedHash; show(value);
      if (kind === "source") download(value.files[filename(value)], filename(value), isDocument(value)?'application/json;charset=utf-8':"text/plain;charset=utf-8");
      else if (kind === 'context') {
        if(!value.files['review-context.json'])throw Object.assign(new Error(),{code:'BAD_JSON'});
        download(value.files['review-context.json'],'review-context.json','application/json;charset=utf-8');
      }
      else if (kind === "record") download(JSON.stringify({schema_version: isDocument(value)?'loom.source-export/v2':"loom.source-export/v1", subject: value.subject,
        ...(value.files['review-context.json']?{related_files:{'review-context.json':await hash(value.files['review-context.json'])}}:{}),
        input_digest: value.input_digest, execution_summary: value.execution_summary, file: filename(value), file_sha256: verifiedHash,
        independently_verified: false, counts_as_success: false, hidden_tests_included: false}, null, 2) + "\n", "loom-source-record.json", "application/json;charset=utf-8");
    } catch (error) {
      if (version === generation && bound === binding && error.code !== "SESSION_CHANGED") {
        data = null; fileHash = ""; refs.code.textContent = ""; refs.contextCode.textContent='';refs.contextSection.hidden=true;refs.details.replaceChildren();
        refs.status.textContent = error.code === "SOURCE_CHANGED" ? "源码或证据版本已变化，请重新读取并审阅后再导出。" : `源码核对失败，当前不能导出。${publicError(error)}`;
      }
    } finally { if (version === generation && bound === binding) { busy = false; controls(); } }
  }
  function open(value) {
    if (!runChecked() || state.busy || !['python-json-program','role-artifact'].includes(value.artifact?.kind)) return;
    const document=value.artifact.kind==='role-artifact';
    reset(); task = value; binding = selection(); root.hidden = false;
    const heading = node("div", "section-heading"), close = node("button", "quiet", document?'关闭正文':"关闭源码"); close.type = "button";
    heading.append(node("h3", "", `${document?'正文':'源码'}与导出 · ${value.title || value.id}`), close);
    const status = node("p", "field-help"), details = node("div"), pre = node("pre", "source-code"), code = node("code");
    status.setAttribute("role", "status"); status.setAttribute("aria-live", "polite"); pre.append(code);
    const actions = node("div", "source-actions"), read = node("button", "", document?'重新读取正文':"重新读取源码"), source = node("button", "", document?'下载 artifact.json':"下载 main.py"), record = node("button", "", "下载版本记录 JSON");
    for (const button of [read, source, record]) button.type = "button";
    actions.append(read, source, record);
    const contextSection=node('details',''),contextHeading=node('summary','','被审原始产物与冻结需求'),contextPre=node('pre','source-code'),contextCode=node('code');
    const contextDownload=node('button','','下载被审原始材料 JSON');contextDownload.type='button';contextDownload.id='source-context-download';
    contextCode.id='source-context-code';contextPre.append(contextCode);contextSection.append(contextHeading,contextDownload,contextPre);contextSection.hidden=true;
    root.append(heading, node("p", "field-help", "正文按当前任务、计划和证据核对。每次下载都会重新核对版本；原始文件不含验收授权，最终签名请在独立复核页查看。"), status, details, actions, pre);
    root.append(contextSection);
    refs = {status, details, code, read, source, record,contextSection,contextCode,contextDownload};
    status.id = "source-state"; code.id = "source-code"; source.id = "source-download"; record.id = "source-record-download"; read.id = "source-refresh";
    close.addEventListener("click", reset); read.addEventListener("click", () => load()); source.addEventListener("click", () => load("source")); record.addEventListener("click", () => load("record"));
    contextDownload.addEventListener('click',()=>load('context'));
    root.scrollIntoView({behavior: "auto", block: "nearest"}); void load();
  }
  return {open, reset, sync};
};
