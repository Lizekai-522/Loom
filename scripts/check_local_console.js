// Real Chromium functional check; disposable PUBLIC fixture on port 8798 only.
async (page) => {
  const checks=[],errors=[],requests=[];
  const check=(ok,name)=>{if(!ok)throw Error(name);checks.push(name);};
  await page.unrouteAll({behavior:'wait'});await page.setExtraHTTPHeaders({});
  page.on('pageerror',e=>errors.push(e.message));page.on('request',r=>{if(r.url().includes('/v1/'))requests.push(r);});
  await page.goto('http://127.0.0.1:8798/console');
  await page.waitForFunction(()=>!document.getElementById('workspace').hidden);
  check(await page.locator('input[type=password],#access-token').count()===0,'credential_screen_removed');
  check((await page.locator('#identity-label').textContent()).includes('本机开发 · 操作员'),'automatic_local_operator_displayed_in_chinese');
  check(requests.every(r=>!r.headers().authorization&&r.headers()['x-loom-console']==='local'),'browser_sends_no_bearer_credentials');
  check(await page.evaluate(()=>localStorage.length===0&&sessionStorage.length===0&&document.cookie===''),'no_browser_credential_storage');
  await page.locator('#new-goal-button').click();
  const title='中文功能测试 '+Date.now();
  await page.locator('#goal-title').fill(title);await page.locator('#goal-description').fill('只验证本机中文界面与计划流程，不调用模型。');
  await page.locator('#goal-token-limit').fill('2000000');await page.locator('#create-goal-button').click();
  await page.waitForFunction(t=>document.getElementById('run-title').textContent===t,title);
  check((await page.locator('#run-description').textContent()).includes('中文界面'),'chinese_goal_text_round_trips');
  const run=await page.locator('#run-id').textContent();
  await page.locator('[data-tab=plan]').click();
  const source=await page.evaluate(async id=>{
    const headers={'X-Loom-Console':'local'};
    const plan=await(await fetch('/v1/goals/'+id+'/plan',{headers})).json();
    return plan;
  },run);
  check(source.status==='draft'&&source.definition===null,'new_goal_does_not_implicitly_approve_or_execute');
  const definition={schema_version:'loom.workflow/v1',pack:'software-dev',scope:'local-workspace',
    roles:['analyst','architect','tester','developer','reviewer','fixer','validator'].map((id,i)=>({id,
      skills:[['requirements.analysis','architecture.design','tests.design','code.implement','code.review','code.fix','acceptance.verify'][i]],
      tools:['workspace.read','artifacts.read','artifacts.write',...(['developer','fixer'].includes(id)?['workspace.write','tests.run']:['tester','validator'].includes(id)?['tests.run']:[])],model_alias:'deepseek-flash'})),
    nodes:[{id:'work',title:'开发实现功能测试',instruction:'只保存冻结合同，不执行模型。',role:'developer',depends:[],limits:{cost_limit_microusd:1000,token_limit:1000,max_attempts:1}}]};
  await page.locator('#plan-definition').fill(JSON.stringify(definition));await page.locator('#save-plan-button').click();
  await page.waitForFunction(()=>/^[a-f0-9]{64}$/.test(document.getElementById('plan-digest').textContent)&&!document.getElementById('approve-plan-button').disabled);
  check((await page.locator('#plan-definition').inputValue()).includes('开发实现功能测试'),'chinese_plan_saved_without_corruption');
  await page.locator('#plan-reviewed').check();await page.locator('#approve-plan-button').click();
  await page.waitForFunction(()=>document.getElementById('plan-status').textContent==='已批准');
  await page.locator('[data-tab=tasks]').click();
  check((await page.locator('#task-list').textContent()).includes('开发实现')&&(await page.locator('#task-list').textContent()).includes('业务任务'),'roles_and_channels_display_in_chinese');
  await page.locator('#control-reason').fill('验证中文暂停流程');await page.locator('#pause-button').click();
  await page.waitForFunction(()=>document.getElementById('run-status').textContent==='已暂停');
  check(await page.locator('#resume-button').isEnabled(),'pause_operation_still_available_without_login');
  await page.locator('#control-reason').fill('验证中文恢复流程');await page.locator('#resume-button').click();
  await page.waitForFunction(()=>document.getElementById('run-status').textContent==='运行开放');
  await page.locator('[data-tab=events]').click();
  await page.waitForFunction(()=>document.querySelectorAll('#event-list .event-head strong').length>0);
  const labels=await page.locator('#event-list .event-head strong').allTextContents();
  check(labels.includes('计划已批准')&&labels.includes('目标已创建'),'audit_event_headings_are_chinese');
  const review=await page.evaluate(async id=>(await(await fetch('/v1/runs/'+id+'/review',{headers:{'X-Loom-Console':'local'}})).json()),run);
  check(review.effects.length===0&&review.run.spent===0&&review.tasks[0].status==='pending','ui_controls_do_not_start_paid_execution');
  await page.reload();await page.waitForFunction(()=>!document.getElementById('workspace').hidden&&!document.getElementById('new-goal-button').disabled);
  check(await page.locator('#new-goal-button').isEnabled(),'reload_reenters_automatically');
  await page.setViewportSize({width:390,height:844});
  check(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'automatic_chinese_workbench_fits_mobile');
  await page.screenshot({path:'output/playwright/loom015-auto-chinese.png',fullPage:true});
  check(errors.length===0,'no_unhandled_javascript_exceptions');
  return {passed:true,assertions_passed:checks.length,checks,fixture_run_id:run,real_provider_requests:0};
}
