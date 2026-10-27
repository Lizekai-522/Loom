// Run through playwright-cli run-code --filename scripts/check_console_suites.js.
// Requires serve_console_fixture.py on 127.0.0.1:8798. PUBLIC fixture tokens only.
async (page) => {
  const origin = 'http://127.0.0.1:8798', checks = [], suffix = Date.now().toString();
  try {
  const token = role => 'suite-console-public-fixture-only-' + role + '-00000000';
  const headers = role => ({Authorization: 'Bearer ' + token(role)});
  const expect = (value, name) => { if (!value) throw Error(name); checks.push(name); };
  const api = async (method, path, data, role = 'operator') => {
    const response = await page.request.fetch(origin + path, {method, data,
      headers: {...headers(role), 'Idempotency-Key': 'browser-' + suffix + '-' + Math.random(), 'Content-Type': 'application/json'}});
    if (!response.ok()) throw Error('fixture_api_' + response.status());
    return response.json();
  };
  const errors = []; page.on('pageerror', error => errors.push(error.message));
  page.setDefaultTimeout(12000);
  await page.unrouteAll({behavior: 'wait'});
  await page.setViewportSize({width: 1440, height: 1000});
  await page.setExtraHTTPHeaders({});
  await page.goto(origin + '/console');
  await page.waitForFunction(() => !document.getElementById('workspace').hidden);
  expect(await page.locator('input[type=password]').count() === 0, 'automatic_local_entry_has_no_credential_form');
  // Create genuine API records, without a gateway, model response, test result,
  // sandbox or forged provenance. JSON numeric lexemes are sent as raw text.
  const goal = await api('POST', '/v1/goals', {title: 'Browser suite ' + suffix, description: 'Public fixture only.', acceptance: ['tests.pass'], cost_limit_microusd: 1000000, token_limit: 2000000});
  const base = '/v1/runs/' + goal.run_id, goalPath = '/v1/goals/' + goal.run_id;
  const plan = await api('GET', goalPath + '/plan');
  const skills = ['requirements.analysis', 'architecture.design', 'tests.design', 'code.implement', 'code.review', 'code.fix', 'acceptance.verify'];
  const definition = {schema_version: 'loom.workflow/v1', pack: 'software-dev', scope: 'local-workspace',
    roles: ['analyst', 'architect', 'tester', 'developer', 'reviewer', 'fixer', 'validator'].map((id, i) => ({id, skills: [skills[i]],
      tools: ['workspace.read', 'artifacts.read', 'artifacts.write', ...(['developer', 'fixer'].includes(id) ? ['workspace.write', 'tests.run'] : ['tester', 'validator'].includes(id) ? ['tests.run'] : [])], model_alias: 'deepseek-flash'}))};
  definition.nodes = [{id: 'checks', title: 'Untrusted cases', instruction: 'Data only.', role: 'tester', depends: [],
    limits: {cost_limit_microusd: 1000, token_limit: 1000, max_attempts: 1}}];
  const saved = await api('PUT', goalPath + '/plan', {expected_revision: plan.revision, definition});
  await api('POST', goalPath + '/plan/approve', {expected_revision: saved.revision, expected_digest: saved.digest});
  const claim = (await api('POST', base + '/claim', {worker_id: 'worker', lease_seconds: 3600}, 'worker')).claim;
  const rawBody = '{"schema_version":"loom.public-checks/v1","visibility":"development","cases":[{"name":"large-integer","input":9007199254740993,"expected":9007199254740993},{"name":"float-and-markup","input":{"x":1.0,"text":"<img src=x onerror=alert(1)>"},"expected":[false,null,1.0]}]}';
  const candidate = await api('POST', base + '/tasks/' + claim.task_id + '/artifacts', '{"worker_id":"worker","epoch":' + claim.epoch + ',"kind":"public-checks","body":' + rawBody + '}', 'worker');
  const before = await api('GET', goalPath + '/plan');
  const login = async role => {
    await page.setExtraHTTPHeaders(headers(role));
    await page.goto(origin + '/console');
    await page.waitForFunction(() => !document.getElementById('workspace').hidden);
    await page.locator('#goal-list button').filter({hasText: goal.title || 'Browser suite ' + suffix}).click();
    await page.waitForFunction(id => document.getElementById('run-id').textContent === id && !document.querySelector('#run-content.loading'), goal.run_id);
    await page.locator('[data-tab=artifacts]').click();
    await page.waitForFunction(() => document.querySelectorAll('.artifact-select').length > 0 && !document.querySelector('.artifact-select').disabled);
    await page.locator('.artifact-select').filter({hasText: '查看并选择候选'}).first().click();
    await page.locator('#suite-panel').waitFor({state: 'visible'});
  };
  await login('operator');
  expect((await page.locator('#suite-cases').textContent()).includes('9007199254740993'), 'large_integer_display_is_exact');
  expect((await page.locator('#suite-cases').textContent()).includes('1.0'), 'float_lexeme_remains_float');
  expect(await page.locator('#suite-cases img,#suite-cases script').count() === 0, 'case_markup_is_text_only');
  expect(await page.locator('#suite-freeze').isDisabled(), 'freeze_requires_server_preview_and_explicit_review');
  await page.locator('input[data-case-name="float-and-markup"]').uncheck();
  await page.locator('#suite-preview').click(); await page.locator('#suite-review').getByText(/开发套件摘要/).waitFor();
  const digestText = await page.locator('#suite-review').textContent();
  expect(digestText.includes('选中 1 项：large-integer') && digestText.includes(saved.digest), 'ordered_subset_and_source_are_visible');
  await page.locator('#suite-reviewed').check();
  // Lose a successful receipt. Retrying the same reviewed request must retain
  // its original idempotency key and append exactly one frozen record.
  const posts = [];
  const observe = req => { if (req.method() === 'POST' && req.url().endsWith('/development-suites')) posts.push({key: req.headers()['idempotency-key'], data: req.postData()}); };
  page.on('request', observe);
  await page.route('**/artifacts/*/development-suites', async route => { await route.fetch(); await route.abort('failed'); }, {times: 1});
  await page.locator('#suite-freeze').click();
  await page.waitForFunction(() => document.getElementById('suite-state').textContent.includes('结果尚未确认'));
  expect(await page.locator('#suite-freeze').isDisabled(), 'lost_receipt_blocks_blind_repeat');
  await page.locator('#suite-preview').click(); await page.locator('#suite-review').getByText(/开发套件摘要/).waitFor();
  await page.locator('#suite-reviewed').check();
  await page.evaluate(() => { const form = document.getElementById('suite-freeze-form'); form.dispatchEvent(new Event('submit', {cancelable: true})); form.dispatchEvent(new Event('submit', {cancelable: true})); });
  await page.locator('#suite-frozen').getByText('冻结记录 · 仅开发检查', {exact: true}).waitFor();
  expect(posts.length === 2 && posts[0].key === posts[1].key && posts[0].data === posts[1].data, 'retry_reuses_key_and_double_submit_is_blocked');
  let events = (await api('GET', base + '/events')).events;
  expect(events.filter(e => e.type === 'development.suite_frozen').length === 1, 'one_freeze_event_after_unknown_outcome_retry');
  expect((await page.locator('#suite-frozen').textContent()).includes('9007199254740993'), 'frozen_body_preserves_large_integer');
  expect((await page.locator('#suite-frozen').textContent()).includes('已认证执行器提交'), 'worker_origin_is_not_forged_as_model_origin');
  expect((await api('GET', base + '/review')).counts_as_success === false, 'review_freeze_does_not_confer_success');
  // A GET failure after an acknowledged POST must not allow a new freeze.
  await page.locator('input[data-case-name="large-integer"]').uncheck();
  await page.locator('input[data-case-name="float-and-markup"]').check();
  expect(await page.locator('#suite-freeze').isDisabled(), 'selection_change_invalidates_old_review');
  await page.locator('#suite-preview').click(); await page.locator('#suite-review').getByText(/开发套件摘要/).waitFor();
  await page.locator('#suite-reviewed').check();
  await page.route('**/development-suites/*?include_body=true', route => route.abort('failed'), {times: 1});
  await page.locator('#suite-freeze').click();
  await page.waitForFunction(() => document.getElementById('suite-frozen').textContent.includes('冻结回执已记录') && !document.getElementById('artifacts-refresh').disabled);
  await page.route('**/events', route => route.abort('failed'), {times: 1});
  await page.locator('#suite-history').click();
  await page.waitForFunction(() => !document.getElementById('suite-history').disabled && document.getElementById('notice').textContent.includes('连接中断'));
  expect(await page.locator('#suite-freeze').isDisabled() && await page.locator('#suite-prepare').isDisabled() && await page.locator('#suite-preview').isDisabled()
    && (await page.locator('#suite-frozen').textContent()).includes('冻结回执已记录'), 'acknowledged_post_read_failure_blocks_duplicate_and_draft');
  await page.locator('#suite-history').click();
  await page.waitForFunction(() => document.querySelectorAll('#suite-history-list button').length === 2 && !document.querySelector('#suite-history-list button').disabled);
  await page.locator('#suite-history-list button').first().click();
  await page.locator('#suite-frozen').getByText('冻结记录 · 仅开发检查', {exact: true}).waitFor();
  expect((await page.locator('#suite-frozen').textContent()).includes('float-and-markup'), 'freeze_history_restores_acknowledged_record');
  await page.locator('#suite-draft-title').fill('New bounded task'); await page.locator('#suite-draft-instruction').fill('Implement public JSON transformation.');
  const suiteId = (await api('GET', base + '/events')).events.filter(e => e.type === 'development.suite_frozen').at(-1).payload.suite_id;
  const suite = await api('GET', base + '/development-suites/' + suiteId + '?include_body=true');
  await page.locator('#suite-host-digest').fill(suite.development_digest);
  await page.locator('#suite-draft-form').dispatchEvent('submit');
  expect(await page.locator('#suite-draft-result').isHidden(), 'host_digest_cannot_equal_development_digest');
  await page.locator('#suite-host-digest').fill('c'.repeat(64)); await page.locator('#suite-repairs').selectOption('2');
  await page.locator('#suite-prepare').click(); await page.locator('#suite-draft-result').waitFor({state: 'visible'});
  const draftText = await page.locator('#suite-draft-json').textContent(), draft = JSON.parse(draftText);
  expect(JSON.stringify(draft.nodes[0].development_suite) === JSON.stringify(suite.execution_reference), 'draft_pins_exact_server_reference');
  expect(draft.nodes[0].limits.max_attempts === 3 && draft.nodes[0].repair.max_repairs === 2, 'draft_repairs_share_bounded_task_contract');
  expect(draft.nodes[1].depends[0] === 'implement' && draft.nodes[1].channel === 'verification', 'draft_preserves_independent_review_dependency');
  expect(!draftText.includes('float-and-markup') && !draftText.includes('<img') && !draftText.includes('model_provenance'), 'draft_contains_reference_without_copied_candidate_origin_or_cases');
  expect((await page.locator('#suite-draft-help').textContent()).includes('501000 µUSD / 1101000 词元'), 'draft_displays_total_goal_budget_requirement');
  await page.locator('#suite-prepare').scrollIntoViewIfNeeded();
  await page.screenshot({path: 'output/playwright/suite-console-desktop.png'});
  const downloadPromise = page.waitForEvent('download'); await page.locator('#suite-download').click();
  const download = await downloadPromise;
  expect(download.suggestedFilename() === 'loom-reviewed-plan-draft.json' && await download.failure() === null, 'draft_download_works_under_console_csp');
  await page.locator('#suite-cost').fill('600000');
  expect(await page.locator('#suite-draft-result').isHidden(), 'budget_edit_invalidates_export');
  const after = await api('GET', goalPath + '/plan');
  expect(after.digest === before.digest && after.revision === before.revision, 'source_approved_plan_unchanged');
  expect((await api('GET', base + '/events')).events.every(e => !['tool.intent', 'execution.evaluated'].includes(e.type)), 'review_and_draft_never_execute_models_or_cases');
  // Import/save and approve are separate visible actions on a NEW goal.
  await page.locator('#new-goal-button').click();
  await page.locator('#goal-title').fill('Imported plan ' + suffix); await page.locator('#goal-description').fill('Fixture plan approval only; host digest is a fixture, no executor.');
  await page.locator('#goal-token-limit').fill('2000000'); await page.locator('#create-goal-button').click();
  await page.waitForFunction(id => document.getElementById('run-id').textContent && document.getElementById('run-id').textContent !== id && !document.querySelector('#run-content.loading'), goal.run_id);
  const targetId = await page.locator('#run-id').textContent();
  await page.locator('[data-tab=plan]').click();
  await page.locator('#plan-definition').fill(draftText); await page.locator('#save-plan-button').click();
  await page.waitForFunction(() => /^[0-9a-f]{64}$/.test(document.getElementById('plan-digest').textContent) && !document.getElementById('approve-plan-button').disabled);
  const targetPlan = await api('GET', '/v1/goals/' + targetId + '/plan');
  expect(targetPlan.status === 'draft' && targetPlan.definition.nodes[0].development_suite.suite_id === suiteId, 'generated_plan_saves_on_new_goal_without_approval');
  expect((await api('GET', '/v1/runs/' + targetId + '/review')).tasks.length === 0, 'draft_save_creates_no_execution_tasks');
  // Legacy may save a draft, but this suite plan requires a named operator.
  await page.setExtraHTTPHeaders(headers('legacy')); await page.goto(origin + '/console');
  await page.waitForFunction(() => !document.getElementById('workspace').hidden);
  await page.locator('#goal-list button').filter({hasText: 'Imported plan ' + suffix}).click();
  await page.waitForFunction(id => document.getElementById('run-id').textContent === id && !document.querySelector('#run-content.loading'), targetId);
  await page.locator('[data-tab=plan]').click();
  expect(await page.locator('#approve-plan-button').isDisabled(), 'legacy_cannot_approve_suite_plan_in_ui');
  let legacyWrites = 0; const countLegacy = req => { if (req.method() === 'POST') legacyWrites++; }; page.on('request', countLegacy);
  await page.locator('#plan-reviewed').evaluate(input => { input.checked = true; }); await page.locator('#approve-plan-form').dispatchEvent('submit');
  expect(legacyWrites === 0, 'forced_legacy_plan_approval_sends_no_write'); page.off('request', countLegacy);
  await page.setExtraHTTPHeaders(headers('operator')); await page.goto(origin + '/console');
  await page.waitForFunction(() => !document.getElementById('workspace').hidden);
  await page.locator('#goal-list button').filter({hasText: 'Imported plan ' + suffix}).click();
  await page.waitForFunction(id => document.getElementById('run-id').textContent === id && !document.querySelector('#run-content.loading'), targetId);
  await page.locator('[data-tab=plan]').click(); await page.locator('#plan-reviewed').check(); await page.locator('#approve-plan-button').click();
  await page.waitForFunction(() => document.getElementById('plan-status').textContent === '已批准');
  const targetReview = await api('GET', '/v1/runs/' + targetId + '/review');
  expect(targetReview.tasks.length === 2 && targetReview.tasks.every(task => task.status === 'pending') && targetReview.effects.length === 0 && targetReview.run.spent === 0, 'separate_operator_approval_does_not_execute_or_mark_done');
  await login('operator');
  // A server response for another source must never enable freezing.
  await page.route('**/development-preview?*', async route => {
    const response = await route.fetch(), data = await response.json(); data.source.revision += 1;
    await route.fulfill({response, json: data});
  }, {times: 1});
  await page.locator('#suite-preview').click();
  await page.waitForFunction(() => document.getElementById('suite-state').textContent.includes('无法解析'));
  expect(await page.locator('#suite-freeze').isDisabled(), 'mismatched_server_source_rejected');
  // A late GET for the previous selection must not populate another run.
  await page.route('**/development-preview?*', async route => { const response = await route.fetch(); await new Promise(resolve => setTimeout(resolve, 350)); await route.fulfill({response}); }, {times: 1});
  await page.locator('#suite-preview').click();
  await page.locator('#goal-list button').filter({hasText: 'Imported plan ' + suffix}).click();
  await page.waitForFunction(id => document.getElementById('run-id').textContent === id && !document.querySelector('#run-content.loading'), targetId);
  expect(await page.locator('#suite-panel').isHidden() && await page.locator('#suite-review').textContent() === '', 'late_preview_cannot_cross_run_selection');
  // All non-operator identities can read but cannot force a suite write/draft.
  page.off('request', observe);
  for (const role of ['viewer', 'worker', 'legacy']) {
    await login(role); await page.locator('#suite-preview').click(); await page.locator('#suite-review').getByText(/开发套件摘要/).waitFor();
    expect(await page.locator('#suite-freeze-form').isHidden(), role + '_freeze_controls_hidden');
    let writes = 0; const observer = req => { if (req.method() === 'POST') writes++; }; page.on('request', observer);
    await page.locator('#suite-freeze-form').dispatchEvent('submit'); await page.locator('#suite-draft-form').dispatchEvent('submit');
    expect(writes === 0 && await page.locator('#suite-draft-result').isHidden(), role + '_forced_actions_send_no_writes'); page.off('request', observer);
  }
  await login('operator');
  await page.evaluate(() => { JSON.rawJSON = undefined; });
  await page.locator('.artifact-select').first().click();
  await page.waitForFunction(() => document.getElementById('suite-state').textContent.includes('无法原样'));
  expect(await page.locator('#suite-preview').isDisabled() && await page.locator('#suite-freeze').isDisabled(), 'unsupported_exact_json_fails_closed');
  await page.reload(); await login('operator');
  await page.locator('#suite-history').click();
  await page.waitForFunction(() => document.querySelectorAll('#suite-history-list button').length === 2 && !document.querySelector('#suite-history-list button').disabled);
  await page.locator('#suite-history-list button').first().click(); await page.locator('#suite-frozen').getByText('冻结记录 · 仅开发检查', {exact: true}).waitFor();
  // Source revision changes on the real server, after the browser read.
  const replacement = JSON.parse(rawBody); replacement.cases = [{name: 'replacement', input: true, expected: false}];
  await api('POST', base + '/tasks/' + claim.task_id + '/artifacts', {worker_id: 'worker', epoch: claim.epoch, kind: 'public-checks', body: replacement}, 'worker');
  await page.locator('#suite-draft-title').fill('Stale task'); await page.locator('#suite-draft-instruction').fill('Never execute stale cases.'); await page.locator('#suite-host-digest').fill('c'.repeat(64));
  await page.locator('#suite-prepare').click();
  await page.locator('#suite-frozen').getByText('冻结记录 · 来源已失效', {exact: true}).waitFor();
  expect(await page.locator('#suite-prepare').isDisabled() && await page.locator('#suite-draft-result').isHidden(), 'actual_source_revision_change_blocks_draft');
  await page.setViewportSize({width: 390, height: 844});
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'suite_mobile_layout_has_no_page_overflow');
  await page.locator('#suite-frozen').scrollIntoViewIfNeeded();
  await page.screenshot({path: 'output/playwright/suite-console-mobile.png'});
  expect(await page.evaluate(() => localStorage.length === 0 && sessionStorage.length === 0 && document.querySelector('input[type=password]') === null), 'no_credential_persistence');
  await page.locator('#logout-button').click();
  expect(await page.locator('#suite-panel').isHidden() && await page.locator('#suite-draft-json').textContent() === '', 'logout_clears_review_and_draft');
  expect(errors.length === 0, 'no_unhandled_browser_exceptions');
  const cleanup = await api('POST', base + '/tasks/' + claim.task_id + '/submit', {worker_id: 'worker', epoch: claim.epoch, output: {fixture_only: true}}, 'worker');
  if (cleanup.status !== 'submitted') throw Error('Fixture claim not released');
  return {passed: true, assertions_passed: checks.length, checks, fixture_run_id: goal.run_id,
    version: (await api('GET', '/health')).version, real_provider_requests: 0, real_docker_executions: 0,
    numeric_body_sent_as_raw_json: true, fixture_claim_released: true, fixture_origin: 'authenticated-worker-submission'};
  } catch (error) { throw Error('After ' + (checks.at(-1) || 'fixture setup') + ': ' + error.message); }
}
