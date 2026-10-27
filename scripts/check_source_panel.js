// Run with Playwright CLI against scripts/serve_source_fixture.py; no real provider calls.
async page => {
 const checks=[],errors=[],writes=[],downloads=[];const check=(ok,name)=>{if(!ok)throw Error(name);checks.push(name);};
 page.on('pageerror',e=>errors.push(e.message));page.on('request',r=>{if(['POST','PUT','DELETE'].includes(r.method()))writes.push(r.url());});page.on('download',d=>downloads.push(d));
 await page.reload();await page.waitForFunction(()=>!document.getElementById('workspace').hidden&&!document.getElementById('new-goal-button').disabled);
 const goals=await page.evaluate(async()=>(await(await fetch('/v1/goals',{headers:{'X-Loom-Console':'local'}})).json()).goals); const run=goals.find(g=>g.title==='审查测试').run_id,task=run+':work',other=goals.find(g=>g.title==='切换任务的草案').run_id;
 const match=url=>url.pathname.endsWith('/review-input');
 const select=async()=>{
   await page.locator('#goal-list button').filter({hasText:run.slice(0,8)}).click();
   await page.waitForFunction(id=>document.getElementById('run-id').textContent===id&&!document.querySelector('#run-content.loading'),run);
   await page.locator('[data-tab=tasks]').click();await page.locator('[data-source-task="'+task+'"]').click();
   await page.waitForFunction(()=>document.querySelector('#source-panel[aria-busy=false]'));
 };
 await select();check((await page.locator('#source-state').textContent()).includes('精确版本已核对'),'unicode_context_digest_verified');
 const body=await page.locator('#source-code').textContent();
 check(body.includes('中文 😀')&&body.includes('<img src=x')&&body.includes('</code><script>'),'source_preserves_unicode_and_markup_as_text');
 check(await page.locator('#source-panel img,#source-panel script').count()===0&&await page.evaluate(()=>window.loomInjected===undefined),'source_markup_never_executes');
 const [file]=await Promise.all([page.waitForEvent('download'),page.locator('#source-download').click()]);await file.saveAs('output/playwright/loom0174-fixture-main.py');
 const [record]=await Promise.all([page.waitForEvent('download'),page.locator('#source-record-download').click()]);await record.saveAs('output/playwright/loom0174-fixture-source-record.json');
 check(file.suggestedFilename()==='main.py'&&record.suggestedFilename()==='loom-source-record.json','unicode_source_and_record_download_with_safe_names');
 const contextHash=value=>page.evaluate(async value=>{
   const stable=v=>Array.isArray(v)?v.map(stable):v&&typeof v==='object'?Object.fromEntries(Object.keys(v).sort().map(k=>[k,stable(v[k])])):v;
   const context={schema_version:value.schema_version,subject:value.subject,execution_summary:value.execution_summary,files:value.files};
   const bytes=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(JSON.stringify(stable(context))));return [...new Uint8Array(bytes)].map(b=>b.toString(16).padStart(2,'0')).join('');
 },value);
 for(const mode of ['source-tamper','wrong-task','wrong-plan','wrong-flags','hidden-test-flag','extra-summary','extra-subject','http-failure','malformed-json']){
   await page.route(match,async route=>{
     if(mode==='http-failure')return route.fulfill({status:409,contentType:'application/json',body:'{"code":"CONFLICT"}'});
     if(mode==='malformed-json')return route.fulfill({status:200,contentType:'application/json',body:'not json'});
     const response=await route.fetch(),value=await response.json();
     if(mode==='source-tamper')value.files['main.py']+='\n# altered';
     if(mode==='wrong-task')value.subject.task_id=other+':work';
     if(mode==='wrong-plan')value.subject.plan_digest='0'.repeat(64);
     if(mode==='wrong-flags')value.independently_verified=true;
     if(mode==='hidden-test-flag')value.hidden_tests_included=true;
     if(mode==='extra-summary')value.execution_summary.hidden_expectation='must-not-export-marker';
     if(mode==='extra-subject')value.subject.private_token='must-not-export-marker';
     if(['extra-summary','extra-subject'].includes(mode))value.input_digest=await contextHash(value);
     await route.fulfill({response,json:value});
   });
   await page.locator('#source-refresh').click();await page.waitForFunction(()=>document.querySelector('#source-panel[aria-busy=false]'));
   check(await page.locator('#source-download').isDisabled()&&await page.locator('#source-code').textContent()==='','reject_'+mode+'_and_clear_stale_source');
   await page.unroute(match);await page.locator('#source-refresh').click();await page.waitForFunction(()=>document.querySelector('#source-panel[aria-busy=false]'));
   check(await page.locator('#source-download').isEnabled(),'recover_after_'+mode);
 }
 await page.route(match,async route=>{
   const response=await route.fetch(),value=await response.json();value.subject.submission_event_hash='a'.repeat(64);
   value.input_digest=await page.evaluate(async value=>{
     const stable=v=>Array.isArray(v)?v.map(stable):v&&typeof v==='object'?Object.fromEntries(Object.keys(v).sort().map(k=>[k,stable(v[k])])):v;
     const context={schema_version:value.schema_version,subject:value.subject,execution_summary:value.execution_summary,files:value.files};
     const bytes=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(JSON.stringify(stable(context))));return [...new Uint8Array(bytes)].map(b=>b.toString(16).padStart(2,'0')).join('');
   },value);
   await route.fulfill({response,json:value});
 });
 const beforeChanged=downloads.length;await page.locator('#source-download').click();await page.waitForFunction(()=>document.querySelector('#source-panel[aria-busy=false]'));
 check((await page.locator('#source-state').textContent()).includes('版本已变化')&&downloads.length===beforeChanged,'changed_evidence_pin_blocks_export_even_with_valid_digest');
 await page.unroute(match);await page.locator('#source-refresh').click();await page.waitForFunction(()=>document.querySelector('#source-panel[aria-busy=false]'));
 let release,seen;const gate=new Promise(r=>release=r),started=new Promise(r=>seen=r);
 await page.route(match,async route=>{seen();await gate;await route.continue();});
 const priorHidden=downloads.length;await page.locator('#source-download').click();await started;await page.locator('[data-tab=events]').click();
 release();await page.waitForFunction(()=>document.querySelector('#source-panel[aria-busy=false]'));
 check(downloads.length===priorHidden,'switching_tab_cancels_inflight_export');
 await page.unroute(match);await page.locator('[data-tab=tasks]').click();await page.locator('#source-refresh').click();await page.waitForFunction(()=>document.querySelector('#source-panel[aria-busy=false]'));
 let releaseLate,seenLate;const lateGate=new Promise(r=>releaseLate=r),lateStarted=new Promise(r=>seenLate=r);
 await page.route(match,async route=>{seenLate();await lateGate;await route.continue();});
 await page.locator('#source-refresh').click();await lateStarted;
 await page.locator('#goal-list button').filter({hasText:other.slice(0,8)}).click();
 await page.waitForFunction(id=>document.getElementById('run-id').textContent===id&&!document.querySelector('#run-content.loading'),other);
 const completed=page.waitForResponse(r=>new URL(r.url()).pathname.endsWith('/review-input'));releaseLate();await(await completed).finished();await page.waitForTimeout(100);
 check(await page.locator('#source-panel').isHidden()&&await page.locator('#source-panel').textContent()==='','late_response_cannot_restore_previous_run_source');
 await page.unroute(match);
 await page.setExtraHTTPHeaders({Authorization:'Bearer source-viewer-public-fixture-00000000'});await page.reload();
 await page.waitForFunction(()=>!document.getElementById('workspace').hidden);await select();
 check((await page.locator('#identity-label').textContent()).includes('只读')&&await page.locator('#source-download').isEnabled(),'viewer_can_read_and_export_authorized_source');
 check(await page.locator('#pause-button').isDisabled(),'source_export_does_not_grant_operator_control');
 await page.locator('#logout-button').click();
 check(await page.locator('#source-panel').isHidden()&&await page.locator('#source-panel').textContent()==='','disconnect_clears_source_from_memory_and_dom');
 check(writes.length===0,'fixture_browser_source_workflow_sends_no_writes');
 check(errors.length===0,'fixture_has_no_unhandled_javascript_errors');
 await page.setExtraHTTPHeaders({});
 return {passed:true,phase:'source_failure_unicode_and_stale_response_fixture',assertions_passed:checks.length,checks,real_provider_requests:0,fixture_provider_is_http_mock:true};
}
