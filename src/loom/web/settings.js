"use strict";
// Settings hub. Every private-configuration step of the executor becomes a form:
// model/secrets/search/mail/verification/quotas/members/sandbox/host-tests/logs.
// Secrets are written server-side and never echoed; structural saves are followed
// by the one-click executor restart this page offers.
(() => {
  const $ = (id) => document.getElementById(id);
  const state = {overview: null, logTimer: null};

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = String(text);
    return e;
  }
  const key = () => (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2));

  function notice(message, error = false) {
    $("notice").textContent = message || "";
    $("notice").className = error ? "notice error" : "notice";
    $("notice").hidden = !message;
    window.scrollTo({top: 0, behavior: "smooth"});
  }

  async function request(method, path, body) {
    const headers = {Accept: "application/json", "X-Loom-Console": "local"};
    if (body !== undefined) headers["Content-Type"] = "application/json";
    headers["Idempotency-Key"] = key();
    const response = await fetch(path, {method, headers, body: body === undefined ? undefined : JSON.stringify(body),
                                        credentials: "same-origin", cache: "no-store"});
    const text = await response.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch { data = null; }
    if (!response.ok) {
      const error = new Error(String(data?.code || response.status));
      error.status = response.status; error.data = data; error.code = data?.code;
      throw error;
    }
    return data;
  }

  function explain(error) {
    const code = error.code || "";
    const MESSAGES = {
      INVALID_MODEL_CONFIG: "模型配置无效：只支持已验证的服务商与模型组合，请按表单选择。",
      MODEL_CONFIG_INVALID: "模型配置读取失败，请重新保存。",
      PRIVATE_CONFIG_INVALID: "密钥文件无效（权限或格式）：请到主机上核对该私有文件的权限（600）。",
      CONFIG_WOULD_BE_INVALID: "修改后的执行器配置未通过校验，已放弃写入，请检查输入。",
      HOST_TESTS_INVALID: "宿主用例格式无效：需要 {\"tests\":[...]}，每条包含输入与期望输出。",
      HOST_TESTS_CHANGED: "用例内容与已冻结摘要不一致，请重新核对。",
      SEARCH_CONFIG_INVALID: "搜索配置无效：bocha/tavily 需要 API key，searxng 需要可访问的 base_url。",
      MAIL_CONFIG_INVALID: "邮箱配置无效：请核对服务商、地址与身份文件中的用户名。",
      QUOTA_POLICY_INVALID: "配额策略无效，请刷新后按当前修订号重试。",
      RESTART_FAILED: "重启失败：本机没有 docker CLI 或容器不在运行，请手动重启。",
      VERIFICATION_CONFIGURATION_FAILED: "复核配置生成失败，请换一个新目录再试。",
    };
    if (MESSAGES[code]) return MESSAGES[code];
    if (error.status === 401) return "本机入口未启用，请用项目启动器启动服务。";
    if (error.status === 403) return "设置修改需要你本人（具名用户身份）来做。";
    if (error.status === 409) return error.data?.detail ? String(error.data.detail) : "状态已变化或与现有配置冲突，请核对后重试。";
    if (error.status === 422) return error.data?.detail ? "输入不符合要求：" + JSON.stringify(error.data.detail).slice(0, 200) : "输入不符合要求。";
    return "暂时连不上 Loom 服务，请稍后重试。";
  }

  async function act(method, path, body, done) {
    try {
      const result = await request(method, path, body);
      if (done) done(result);
      await load();
      return result;
    } catch (error) {
      notice(explain(error), true);
      return null;
    }
  }

  function form(parent) { const f = el("form", "as-form"); parent.append(f); return f; }
  function field(parent, label, input) {
    const l = el("label");
    l.append(el("span", null, label), input);
    parent.append(l);
    return input;
  }
  function select(options, value) {
    const s = el("select");
    for (const [v, text] of options) {
      const o = el("option", null, text);
      o.value = v;
      if (v === value) o.selected = true;
      s.append(o);
    }
    return s;
  }
  function button(parent, text, cls, handler) {
    const b = el("button", cls, text);
    b.type = "button";
    b.addEventListener("click", handler);
    parent.append(b);
    return b;
  }
  const dot = (level) => el("span", "dot " + level);
  const PROBE_TEXT = {ok: "正常", error: "异常", unknown: "未知"};

  // ---------- overview ----------
  function renderOverview(o) {
    const cards = $("overview-cards");
    cards.replaceChildren();
    const card = (id, title, level, hint) => {
      const c = el("div", "card");
      const h = el("h3");
      const a = el("a", null, title);
      a.href = "#section-" + id;
      h.append(dot(level), a);
      c.append(h, el("p", "hint", hint));
      cards.append(c);
    };
    const model = o.model || {};
    const probe = model.probe || {};
    card("model", "模型与密钥", model.configured ? (probe.state === "error" ? "warn" : "ok") : "err",
         model.configured ? `已配置 ${model.detail ? model.detail.model : ""}（${model.detail ? model.detail.provider : ""}）` +
           (probe.state ? `；探测：${PROBE_TEXT[probe.state] || probe.state}` : "") : "未配置：对话与编程都无法调用模型");
    card("sandbox", "沙箱镜像", o.sandbox.image_set ? ((o.sandbox.probe || {}).state === "error" ? "warn" : "ok") : "warn",
         o.sandbox.image_set ? "已登记 " + (o.sandbox.image || "") +
           ((o.sandbox.probe || {}).state ? `；校验：${PROBE_TEXT[o.sandbox.probe.state] || o.sandbox.probe.state}` +
             (o.sandbox.probe.code ? "（" + o.sandbox.probe.code + "）" : "") : "") : "未登记：真实代码执行需要冻结沙箱镜像");
    card("search", "联网搜索", o.search.configured ? (o.search.key_present || o.search.provider === "searxng" ? "ok" : "warn") : "warn",
         o.search.provider === "none" || !o.search.provider ? "未配置：助手只能读取你给出的 https 链接" :
           (o.search.configured && !o.search.key_present && o.search.provider !== "searxng" ? "已选 " + o.search.provider + " 但密钥缺失" : "已配置 " + o.search.provider));
    card("mail", "域名邮箱", o.mail.configured ? (o.mail.password_present ? "ok" : "warn") : "warn",
         o.mail.configured ? (o.mail.address + (o.mail.password_present ? "" : "；密码文件缺失")) + (o.mail.send ? "；发信已启用" : "；只收不发") : "未配置：助手不收发邮件");
    card("verification", "独立双模型复核", o.verification.configured ? "ok" : "warn",
         o.verification.configured ? "已配置（双厂商独立复核）" : "未配置：编程结果只到“提交”，不出独立验收");
    card("quotas", "持久配额", o.quotas.enabled ? "ok" : "warn",
         o.quotas.enabled ? "已启用，修订 r" + o.quotas.revision : "未启用：不限流（单机默认可接受）");
    card("members", "成员与访问", o.members.count > 0 ? "ok" : "err",
         "共 " + o.members.count + " 个本机身份");
    card("hosttests", "宿主用例", o.host_tests.count > 0 ? "ok" : "warn",
         o.host_tests.count > 0 ? "已登记 " + o.host_tests.count + " 组冻结用例" : "未登记：编程执行到终验时需要");
    $("restart-banner").hidden = !o.needs_restart;
  }

  // ---------- model ----------
  const MODEL_OPTIONS = {
    deepseek: [["deepseek-flash", "DeepSeek Flash"], ["deepseek-v4-pro", "DeepSeek V4 Pro"]],
    dashscope: [["qwen3.8-max-0902", "千问 qwen3.8-max-0902（固定快照）"]],
  };
  function renderModel(o) {
    const body = $("model-body");
    body.replaceChildren();
    const f = form(body);
    const provider = select([["deepseek", "DeepSeek"], ["dashscope", "阿里云百炼（千问）"]],
                            o.model?.detail?.provider || "deepseek");
    const model = select(MODEL_OPTIONS.deepseek, "deepseek-flash");
    const onProvider = () => {
      model.replaceChildren();
      for (const [v, t] of MODEL_OPTIONS[provider.value]) { const o2 = el("option", null, t); o2.value = v; model.append(o2); }
    };
    provider.addEventListener("change", onProvider);
    onProvider();
    const apiKey = el("input"); apiKey.type = "password"; apiKey.placeholder = "sk-…（保存后不回显）";
    field(f, "服务商", provider);
    field(f, "模型", model);
    field(f, "API Key", apiKey);
    f.append(el("p", "kv", "Flash 用于生成；V4 Pro 和千问用于审查。保存后请重启执行器。"));
    const row = el("div", "row");
    const save = el("button", "primary", "保存模型与密钥"); save.type = "submit";
    row.append(save);
    button(row, "测试真实连通性（小额真实调用）", "ghost", () => act("POST", "/v1/settings/model/test", {},
      () => notice("已请求真实连通性测试，几秒后刷新本页查看结果。")));
    f.append(row);
    f.addEventListener("submit", (event) => {
      event.preventDefault();
      if (!apiKey.value.trim()) return notice("请先填入 API Key。", true);
      act("PUT", "/v1/settings/model", {provider: provider.value, model: model.value, api_key: apiKey.value.trim()},
          (r) => notice(r.detail));
      apiKey.value = "";
    });
    const probe = o.model?.probe;
    if (probe) {
      body.append(el("p", "statusline",
        `配置探测：${PROBE_TEXT[probe.state] || probe.state}` + (probe.code ? "（" + probe.code + "）" : "") +
        (probe.alias ? " · 别名 " + probe.alias : "")));
      const real = probe.real_call;
      if (real) body.append(el("p", "statusline",
        `真实调用：${PROBE_TEXT[real.state] || real.state}` + (real.code ? "（" + real.code + "）" : "") +
        (real.cost_microusd !== undefined ? ` · 花费 $${(real.cost_microusd / 1e6).toFixed(4)}` : "") +
        (real.detail ? " · 回复「" + real.detail + "」" : "")));
    }
    if (o.model && o.model.configured && !o.model.config_live)
      body.append(el("p", "kv", "已保存配置与 API 启动时的配置路径不同，请重启 API 与执行器后核对状态。"));
  }

  // ---------- search ----------
  function renderSearch(o) {
    const body = $("search-body");
    body.replaceChildren();
    const current = o.search || {provider: "none"};
    const f = form(body);
    const provider = select([["none", "不使用（只读用户给的链接）"], ["bocha", "博查 Bocha"], ["tavily", "Tavily"], ["searxng", "自建 SearXNG"]],
                            current.provider || "none");
    const apiKey = el("input"); apiKey.type = "password"; apiKey.placeholder = "搜索服务的 API key（不回显）";
    const baseUrl = el("input"); baseUrl.placeholder = "https://你的-searxng host（仅 searxng 需要）";
    const maxResults = el("input"); maxResults.type = "number"; maxResults.min = "1"; maxResults.max = "10"; maxResults.value = "5";
    field(f, "搜索来源", provider);
    field(f, "API Key（bocha/tavily）", apiKey);
    field(f, "SearXNG 地址", baseUrl);
    field(f, "单次结果数上限", maxResults);
    const row = el("div", "row");
    const save = el("button", "primary", "保存搜索配置"); save.type = "submit";
    row.append(save);
    f.append(row);
    f.addEventListener("submit", (event) => {
      event.preventDefault();
      const payload = {provider: provider.value, max_results: Number(maxResults.value)};
      if (apiKey.value.trim()) payload.key = apiKey.value.trim();
      if (baseUrl.value.trim()) payload.base_url = baseUrl.value.trim();
      act("PUT", "/v1/settings/search", payload, (r) => notice(r.detail));
      apiKey.value = "";
    });
    if (current.provider && current.provider !== "none")
      body.append(el("p", "kv", "当前：" + current.provider + (current.key_present ? "（密钥已配置）" : "（密钥缺失）")));
  }

  // ---------- mail ----------
  function renderMail(o) {
    const body = $("mail-body");
    body.replaceChildren();
    const f = form(body);
    const provider = select([["aliyun-qiye", "阿里云企业邮箱（大陆）"], ["aliyun-qiye-hk", "阿里云企业邮箱（香港）"]],
                            o.mail?.address ? undefined : "aliyun-qiye");
    const address = el("input"); address.placeholder = "agent@你的域名";
    const subject = el("input"); subject.placeholder = "身份文件里的唯一具名用户名";
    const owners = el("textarea"); owners.rows = "2"; owners.placeholder = "你本人的邮箱，多个用逗号分隔";
    const password = el("input"); password.type = "password"; password.placeholder = "三方客户端安全密码（不回显）";
    const send = el("input"); send.type = "checkbox";
    field(f, "服务商", provider);
    field(f, "邮箱地址", address);
    field(f, "所属具名用户（subject）", subject);
    field(f, "主人邮箱（收进谁的会话）", owners);
    field(f, "客户端密码", password);
    const sendLabel = el("label");
    sendLabel.append(el("span", null, "允许发信（每封仍需批准草稿）"), send);
    f.append(sendLabel);
    const row = el("div", "row");
    const save = el("button", "primary", "保存邮箱配置"); save.type = "submit";
    row.append(save);
    button(row, "移除邮箱配置", "danger", () => { if (confirm("确定移除邮箱配置？")) act("DELETE", "/v1/settings/mail", {}, (r) => notice(r.detail)); });
    f.append(row);
    f.addEventListener("submit", (event) => {
      event.preventDefault();
      const payload = {provider: provider.value, address: address.value.trim(), subject: subject.value.trim(),
                       owner_addresses: owners.value.split(/[,，\s]+/).filter(Boolean), password: password.value, send: send.checked};
      act("PUT", "/v1/settings/mail", payload, (r) => notice(r.detail));
      password.value = "";
    });
    if (o.mail && o.mail.configured)
      body.append(el("p", "kv", "当前：" + o.mail.address + (o.mail.send ? "（发信已启用）" : "（只收不发）") +
        (o.mail.password_present ? "" : "；密码文件缺失")));
  }

  // ---------- verification ----------
  function renderVerification(o) {
    const body = $("verification-body");
    body.replaceChildren();
    const f = form(body);
    const pool = el("input"); pool.type = "number"; pool.min = "0"; pool.value = "2000000";
    const dashKey = el("input"); dashKey.type = "password"; dashKey.placeholder = "DASHSCOPE_API_KEY（千问复核用；不回显）";
    field(f, "复核池总上限（微美元）", pool);
    field(f, "千问 API Key（可选，也可只配 DeepSeek）", dashKey);
    const row = el("div", "row");
    const save = el("button", "primary", o.verification.configured ? "在新目录重新生成（会拒绝覆盖）" : "生成复核配置与签名密钥");
    save.type = "submit";
    row.append(save);
    f.append(row);
    f.addEventListener("submit", (event) => {
      event.preventDefault();
      const payload = {pool_limit_microusd: Number(pool.value)};
      if (dashKey.value.trim()) payload.dashscope_key = dashKey.value.trim();
      act("POST", "/v1/settings/verification", payload, (r) => notice(r.detail));
      dashKey.value = "";
    });
    if (o.verification.configured)
      body.append(el("p", "kv", "状态：已配置" + (o.verification.workspace_ready ? "，工作目录就绪" : "，工作目录缺失")));
  }

  // ---------- sandbox ----------
  function renderSandbox(o) {
    const body = $("sandbox-body");
    body.replaceChildren();
    const f = form(body);
    const image = el("input"); image.placeholder = "sha256:a690…（docker image inspect 输出的 Id）";
    field(f, "沙箱镜像 ID", image);
    const row = el("div", "row");
    const save = el("button", "primary", "登记镜像"); save.type = "submit";
    row.append(save);
    f.append(row);
    f.addEventListener("submit", (event) => {
      event.preventDefault();
      act("PUT", "/v1/settings/sandbox-image", {image: image.value.trim()}, (r) => notice(r.detail));
      image.value = "";
    });
    const probe = o.sandbox.probe;
    if (probe) body.append(el("p", "statusline",
      `冻结校验：${PROBE_TEXT[probe.state] || probe.state}` + (probe.code ? "（" + probe.code + "）" : "") +
      (probe.docker_version ? " · Docker " + probe.docker_version : "") + (probe.image_id ? " · " + probe.image_id.slice(7, 26) : "")));
    if (o.sandbox.image_set) body.append(el("p", "kv", "当前登记：" + o.sandbox.image + "…"));
  }

  // ---------- host tests ----------
  function renderHostTests() {
    const body = $("hosttests-body");
    body.replaceChildren();
    const f = form(body);
    const area = el("textarea"); area.rows = "8"; area.placeholder = '{"tests": [{"name": "示例", "input": "…", "expected": "…"}]}';
    field(f, "用例文件内容（JSON）", area);
    const row = el("div", "row");
    const save = el("button", "primary", "冻结并登记"); save.type = "submit";
    row.append(save);
    f.append(row);
    f.addEventListener("submit", (event) => {
      event.preventDefault();
      let payload;
      try { payload = JSON.parse(area.value); } catch { return notice("用例不是合法 JSON。", true); }
      act("POST", "/v1/settings/host-tests", payload, (r) => notice(`已登记 ${r.host_case_count} 条，摘要 ${r.evaluation_digest.slice(0, 12)}…；重启执行器后生效。`));
      area.value = "";
    });
  }

  // ---------- quotas ----------
  function renderQuotas(o) {
    const body = $("quotas-body");
    body.replaceChildren();
    const current = (o.quotas && o.quotas.policy) || {window_seconds: 60, tenant: {max_calls: 60, max_in_flight: 8},
                                                      model: {max_calls: 30, max_in_flight: 4}, tool: {max_calls: 60, max_in_flight: 8}};
    const f = form(body);
    const num = (value, min, max) => { const i = el("input"); i.type = "number"; i.min = String(min); i.max = String(max); i.value = String(value); return i; };
    const window = num(current.window_seconds, 1, 3600);
    const tenantCalls = num(current.tenant.max_calls, 0, 10000), tenantFlight = num(current.tenant.max_in_flight, 0, 256);
    const modelCalls = num(current.model.max_calls, 0, 10000), modelFlight = num(current.model.max_in_flight, 0, 256);
    const toolCalls = num(current.tool.max_calls, 0, 10000), toolFlight = num(current.tool.max_in_flight, 0, 256);
    const reason = el("input"); reason.placeholder = "为什么调整（写入配额台账）";
    field(f, "窗口（秒）", window);
    const grid1 = el("div", "inline"); field(grid1, "租户 max_calls", tenantCalls); field(grid1, "租户 max_in_flight", tenantFlight);
    const grid2 = el("div", "inline"); field(grid2, "模型 max_calls", modelCalls); field(grid2, "模型 max_in_flight", modelFlight);
    const grid3 = el("div", "inline"); field(grid3, "工具 max_calls", toolCalls); field(grid3, "工具 max_in_flight", toolFlight);
    f.append(grid1, grid2, grid3);
    field(f, "理由", reason);
    const row = el("div", "row");
    const save = el("button", "primary", o.quotas.enabled ? "更新配额（追加修订）" : "启用配额"); save.type = "submit";
    row.append(save);
    f.append(row);
    f.addEventListener("submit", async (event) => {
      event.preventDefault();
      let currentRevision = 0;
      try { currentRevision = (await request("GET", "/v1/settings/quotas")).revision; } catch { /* fall through */ }
      const payload = {window_seconds: Number(window.value),
                       tenant: {max_calls: Number(tenantCalls.value), max_in_flight: Number(tenantFlight.value)},
                       model: {max_calls: Number(modelCalls.value), max_in_flight: Number(modelFlight.value)},
                       tool: {max_calls: Number(toolCalls.value), max_in_flight: Number(toolFlight.value)},
                       expected_revision: currentRevision, reason: reason.value.trim() || "settings-ui"};
      act("PUT", "/v1/settings/quotas", payload, (r) => notice(r.changed ? `配额已保存为修订 r${r.revision}。` : "与上次提交相同，未产生新修订。"));
      reason.value = "";
    });
    body.append(el("p", "kv", o.quotas.enabled ? `当前已启用，修订 r${o.quotas.revision}。` : "当前未启用配额（不限流）。"));
  }

  // ---------- members ----------
  function renderMembers(o) {
    const body = $("members-body");
    body.replaceChildren();
    body.append(el("p", "kv", "此处管理执行器等本机服务身份。团队成员体验请使用独立的公网邀请管理，不能共用此处令牌。"));
    const table = el("table", "members");
    const head = el("tr");
    for (const text of ["身份", "角色", "操作"]) head.append(el("th", null, text));
    table.append(head);
    for (const m of (o.members.subjects || [])) {
      const tr = el("tr");
      tr.append(el("td", null, m.subject));
      const rolesCell = el("td");
      for (const role of (m.roles || [])) rolesCell.append(el("span", "pill", role));
      tr.append(rolesCell);
      const ops = el("td");
      if (!(m.roles || []).includes("user")) {
        button(ops, "换令牌", "ghost", async () => {
          if (confirm("确定更换 " + m.subject + " 的令牌？旧令牌重启后失效。"))
            act("POST", `/v1/settings/members/${encodeURIComponent(m.subject)}/rotate`, {}, (r) => notice("已换发令牌，邀请文件已更新；重启后生效。"));
        });
        button(ops, "移除", "danger", async () => {
          if (confirm("确定移除 " + m.subject + "？历史审计保留。"))
            act("POST", `/v1/settings/members/${encodeURIComponent(m.subject)}/remove`, {}, (r) => notice("已移除 " + r.removed + "；重启后生效。"));
        });
      } else ops.append(el("span", "muted", "控制台用户（保护）"));
      tr.append(ops);
      table.append(tr);
    }
    body.append(table);
    const f = form(body);
    const subject = el("input"); subject.placeholder = "xiaoming（小写字母/数字/-/_）";
    const display = el("input"); display.placeholder = "显示名（可选）";
    field(f, "新成员 ID", subject);
    field(f, "显示名", display);
    const rolesRow = el("div", "inline");
    const boxes = {};
    for (const role of ["operator", "worker", "viewer"]) {
      const b = el("input"); b.type = "checkbox"; b.checked = role === "worker";
      boxes[role] = b;
      const l = el("label"); l.append(el("span", null, role), b);
      rolesRow.append(l);
    }
    f.append(rolesRow);
    const row = el("div", "row");
    const save = el("button", "primary", "添加成员"); save.type = "submit";
    row.append(save);
    f.append(row);
    f.addEventListener("submit", (event) => {
      event.preventDefault();
      const roles = Object.entries(boxes).filter(([, b]) => b.checked).map(([r]) => r);
      act("POST", "/v1/settings/members", {subject: subject.value.trim(), roles, display: display.value.trim()},
          (r) => notice(`已添加 ${r.added}；令牌写入 ${r.invite_file}。API 与执行器重启后生效。`));
      subject.value = ""; display.value = "";
    });
  }

  // ---------- executor ----------
  function renderExecutor(o) {
    const body = $("executor-body");
    body.replaceChildren();
    const ex = o.executor || {};
    body.append(el("p", "statusline",
      `心跳：${{recent: "正常", stale: "过期", not_observed: "未观测", unknown: "未知", not_configured: "未配置"}[ex.state] || ex.state}` +
      (ex.observed_age_seconds !== null && ex.observed_age_seconds !== undefined ? `（${ex.observed_age_seconds} 秒前）` : "") +
      (ex.started_at ? ` · 进程启动于 ${new Date(ex.started_at * 1000).toLocaleTimeString()}` : "")));
    const row = el("div", "row");
    button(row, "重启执行器", "ghost", () => doRestart("executor"));
    button(row, "重启 API 与执行器", "danger", () => {
      if (confirm("重启 API 会短暂断开页面（几秒后自动恢复）。继续？")) doRestart("all");
    });
    button(row, "刷新日志", "ghost", refreshLogs);
    body.append(row);
    const logs = el("pre", "logs"); logs.id = "executor-logs"; logs.textContent = "（加载中…）";
    body.append(logs);
    refreshLogs();
  }

  async function refreshLogs() {
    const logs = $("executor-logs");
    if (!logs) return;
    try {
      const data = await request("GET", "/v1/settings/executor-logs?tail=200");
      logs.textContent = data.exists ? (data.lines.join("\n") || "（暂无日志）") : "（尚无日志文件：执行器产生活动后出现）";
    } catch (error) {
      logs.textContent = "日志读取失败：" + explain(error);
    }
  }

  async function doRestart(scope) {
    const result = await act("POST", "/v1/settings/restart", {scope}, (r) => notice(r.detail));
    if (result) setTimeout(load, 4000);
  }

  async function load() {
    let o;
    try {
      o = await request("GET", "/v1/settings/overview");
    } catch (error) {
      notice(explain(error), true);
      return;
    }
    state.overview = o;
    renderOverview(o);
    renderModel(o);
    renderSearch(o);
    renderMail(o);
    renderVerification(o);
    renderQuotas(o);
    renderMembers(o);
    renderSandbox(o);
    renderHostTests();
    renderExecutor(o);
  }

  $("restart-now").addEventListener("click", () => doRestart("executor"));
  load();
})();
