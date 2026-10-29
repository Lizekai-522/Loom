"use strict";
window.LoomExecutionPanel=function({state,node,has,request,publicError,runChecked,refresh}){
 const names={queued:'等待本机执行器处理',running:'本机执行中',submitted:'候选已提交，等待独立验证',blocked:'执行前置条件变化，请重新核对',awaiting_human:'执行已停止，需人工处理',indeterminate:'结果未知，先核对调用与预算',cancelled:'排队请求已取消'};
 const reasons={EXECUTOR_NOT_CONFIGURED:'本机执行器尚未配置，请使用项目启动说明配置并启动。',REQUEST_ALREADY_ACTIVE:'已有请求，不会重复发起。',REQUEST_LIMIT_REACHED:'此任务已达到请求记录上限，请查看历史。',HOST_TESTS_NOT_REGISTERED:'此节点尚未登记本机冻结终验。请先配置独立宿主用例，再重新核对。',HOST_TESTS_CHANGED:'本机用例与批准摘要不一致，禁止执行。',INDEPENDENT_MODEL_PENDING:'独立模型仍待配置，不能从普通执行入口验收。',ADMISSION_NOT_READY:'计划、输入、预算或任务状态尚不满足准入条件。'};
 let generation=0,binding='',item=null,refs=null,data=null,pending=false,error='',attempt=null,receipt=null,timer=null,queuedObservation=null,heartbeat=null;
 const operator=()=>has('control')&&state.principal?.roles?.includes('operator');
 const current=()=>binding&&runChecked()&&state.tab==='progression'&&!state.busy&&refs?.root.isConnected;
 const path=()=>`/v1/runs/${encodeURIComponent(state.runId)}/tasks/${encodeURIComponent(item.task_id)}`;
 const cancelPath=id=>`/v1/runs/${encodeURIComponent(state.runId)}/execution-requests/${encodeURIComponent(id)}/cancel`;
 function reset(){generation++;clearTimeout(timer);timer=null;binding='';item=null;refs=null;data=null;pending=false;error='';attempt=receipt=null;queuedObservation=null;heartbeat=null;}
 function valid(r){return r?.schema_version==='loom.dispatch-preview/v1'&&r.run_id===state.runId&&r.task_id===item.task_id&&r.tenant_id===state.principal?.tenant_id
   &&r.model_dispatched===false&&r.counts_as_success===false&&typeof r.ready==='boolean'&&/^[0-9a-f]{64}$/.test(r.dispatch_digest);}
 function controls(){
   if(!refs)return;
   const heartbeatNames={recent:'执行器：本次读取在最近 15 秒内收到心跳。',stale:'执行器：超过 15 秒未收到心跳，请检查本机执行器；不要重复提交。',not_observed:'执行器：尚未观察到心跳，请确认本机执行器已启动。',not_configured:'执行器：尚未配置。',unknown:'执行器：状态暂时无法确认。'};
   refs.heartbeat.textContent=(heartbeatNames[heartbeat?.state]||'执行器：本次未能读取心跳。')+' 心跳不代表模型或容器已通过健康检查；排队、取消和执行仍按原有规则处理。';
   const latest=data?.latest_request,ready=current()&&!pending;
   if(latest?.state==='queued'){
     if(!queuedObservation||queuedObservation.id!==latest.request_id)queuedObservation={id:latest.request_id,since:performance.now()};
   }else queuedObservation=null;
   const queueWarning=queuedObservation&&performance.now()-queuedObservation.since>=10000?' · 本页面观察排队已超过 10 秒；若迟迟未开始，请检查本机执行器或继续等待，避免重复提交。':'';
   const retry=Boolean(attempt);
   const writable=ready&&operator()&&!error&&!receipt&&((data?.ready&&!retry)||(retry&&data));
   refs.form.hidden=!operator()||Boolean(latest&&!data?.ready&&!retry);
   refs.reason.disabled=refs.reviewed.disabled=!writable||retry;
   refs.submit.disabled=!writable||(!retry&&(!refs.reviewed.checked||!refs.reason.value.trim()));
   refs.submit.textContent=retry?'重试同一笔请求':'提交给本机执行器';
   refs.read.disabled=!ready;
   refs.cancel.hidden=!operator()||latest?.state!=='queued';refs.cancel.disabled=!ready||retry||Boolean(receipt);
   refs.refresh.disabled=!ready;
   refs.status.textContent=pending?'正在核对执行账本…':error||(receipt&&!data?'请求回执已收到，后续读取失败；请重新读取确认，勿重复发起。'
     :latest?`${names[latest.state]||'状态待核对'}${queueWarning}${retry?' · 上次回执未确认，字段已锁定；可读取核对或用同一编号重试':''}`
     :data?.ready?'预览已核对。勾选并显式提交后，本机执行器才会开始处理。':reasons[data?.reason]||'先读取本机执行条件。');
   refs.budget.textContent=data?.approved_preview?`开始执行时费用预留上界 ${data.approved_preview.initial_cost_reservation_upper.toLocaleString('zh-CN')} µUSD，词元预留上界 ${data.approved_preview.initial_token_reservation.toLocaleString('zh-CN')}。实际按已知用量结算；未知结果保留预留。`:'';
   refs.history.replaceChildren();
   if(latest){refs.history.append(node('p','mono break-word',`请求 ${latest.request_id}`),node('p','field-help',latest.reason),node('p','mono break-word',`最近事件 ${latest.last_event_hash||latest.event_hash}`));
     if(latest.result)refs.history.append(node('p','field-help',`已登记 ${latest.result.model_calls} 次模型发起，已知响应 ${latest.result.settled_calls} 次；此任务仍预留 ${latest.result.reserved_cost} µUSD / ${latest.result.reserved_tokens} 词元。`));}
 }
 function poll(){clearTimeout(timer);if(current()&&['queued','running'].includes(data?.latest_request?.state)&&!error)timer=setTimeout(()=>load(),1000);}
 async function readHeartbeat(version,bound){
   let value=null;
   try{
     const h=await request('GET','/v1/executor-status');
     if(h?.schema_version==='loom.executor-status/v1'&&h.tenant_id===state.principal?.tenant_id&&h.recent_window_seconds===15
       &&h.provider_health_checked===false&&h.sandbox_health_checked===false&&h.model_dispatched===false&&h.counts_as_success===false)value=h;
   }catch{}
   if(version===generation&&bound===binding){heartbeat=value;controls();}
 }
 async function load(){
   if(!current()||pending)return;
   const version=++generation,bound=binding,root=path();pending=true;error='';heartbeat=null;controls();void readHeartbeat(version,bound);
   try{const value=await request('GET',root+'/execution-preview');if(version!==generation||bound!==binding)return;
     if(!valid(value))throw Object.assign(new Error(),{code:'BAD_JSON'});
     data=value;
     const latest=value.latest_request;
     if(attempt&&latest&&((attempt.kind==='queue'&&latest.dispatch_digest===attempt.body.expected_dispatch_digest&&latest.reason===attempt.body.reason)
       ||(attempt.kind==='cancel'&&latest.request_id===attempt.id&&latest.state==='cancelled')))attempt=null;
     if(receipt&&(!latest||latest.request_id!==receipt))throw Object.assign(new Error(),{code:'BAD_JSON'});
     receipt=null;
   }catch(e){if(version===generation&&bound===binding&&e.code!=='SESSION_CHANGED'){data=null;error=publicError(e);}}
   finally{if(version===generation&&bound===binding){pending=false;controls();poll();}}
 }
 async function send(kind){
   if(!current()||pending||!operator())return;
   const bound=binding,version=generation;
   if(!attempt){
     if(kind==='queue'){
       if(!data?.ready||!refs.reviewed.checked||!refs.reason.value.trim())return;
       attempt={kind,body:{expected_dispatch_digest:data.dispatch_digest,reason:refs.reason.value.trim()},url:path()+'/execution-requests',key:'local-execution:'+crypto.randomUUID()};
     }else{
       const id=data?.latest_request?.request_id;if(data?.latest_request?.state!=='queued')return;
       attempt={kind,id,body:{reason:'工作台显式取消尚未开始的本机执行请求'},url:cancelPath(id),key:'local-cancel:'+crypto.randomUUID()};
     }
   }
   pending=true;error='';clearTimeout(timer);controls();
   try{const r=await request('POST',attempt.url,attempt.body,attempt.key);if(bound!==binding||version!==generation)return;
     if(!r?.request_id||r.task_id!==item.task_id||r.run_id!==state.runId||r.counts_as_success!==false)throw Object.assign(new Error(),{code:'BAD_JSON'});
     receipt=r.request_id;attempt=null;data=null;refs.reviewed.checked=false;refs.reason.value='';
   }catch(e){if(bound===binding&&version===generation&&e.code!=='SESSION_CHANGED'){
     error=publicError(e);if([401,403,404,409,422].includes(e.status)){attempt=null;data=null;}else error='';}}
   finally{if(bound===binding&&version===generation){pending=false;if(receipt)await load();else controls();}}
 }
 function open(value,container){
   reset();item=value;binding=JSON.stringify([state.session,state.selection,state.runId,value.task_id]);
   const root=node('section','suite-panel'),heading=node('div','section-heading'),read=node('button','','重新读取执行条件');read.type='button';
   heading.append(node('h3','','工作台执行'),read);root.append(heading,node('p','field-help','页面只登记有版本约束的请求，本机执行器读取私有配置后处理。排队不是已经调用；执行成功仍需独立验证。请勿把密钥写入理由。'));
   const status=node('p','field-help'),heartbeatStatus=node('p','field-help'),budget=node('p','field-help'),history=node('div');status.setAttribute('role','status');status.setAttribute('aria-live','polite');
   const form=node('form'),label=node('label','','执行理由'),reason=node('textarea');reason.rows=3;reason.maxLength=2000;reason.required=true;label.append(reason);
   const check=node('label','check-label'),reviewed=node('input');reviewed.type='checkbox';reviewed.required=true;
   check.append(reviewed,node('span','','我已核对精确计划、输入和预算，允许本机执行器调用模型；结果仍需独立验证'));
   const submit=node('button','primary','提交给本机执行器');submit.type='submit';form.append(label,check,submit);
   const cancel=node('button','','取消尚未开始的请求'),refreshButton=node('button','','刷新任务与证据');cancel.type=refreshButton.type='button';
   root.append(status,heartbeatStatus,budget,form,history,cancel,refreshButton);container.append(root);
   refs={root,read,status,heartbeat:heartbeatStatus,budget,history,form,reason,reviewed,submit,cancel,refresh:refreshButton};
   heartbeatStatus.dataset.executorHeartbeat='local';
   root.dataset.executionPanel='local';status.dataset.executionState='local';form.dataset.executionForm='local';reason.dataset.executionReason='local';reviewed.dataset.executionReviewed='local';submit.dataset.executionSubmit='local';cancel.dataset.executionCancel='local';
   read.addEventListener('click',load);reason.addEventListener('input',controls);reviewed.addEventListener('change',controls);
   form.addEventListener('submit',e=>{e.preventDefault();controls();if(!submit.disabled)void send('queue');});cancel.addEventListener('click',()=>send('cancel'));
   refreshButton.addEventListener('click',()=>{if(!refreshButton.disabled)refresh();});void load();
 }
 function sync(){if(!refs)return;if(binding!==JSON.stringify([state.session,state.selection,state.runId,item?.task_id])||!refs.root.isConnected){reset();return;}controls();if(!current()){clearTimeout(timer);queuedObservation=null;}else if(!pending)poll();}
 return {open,reset,sync};
};
