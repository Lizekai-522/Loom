"use strict";
window.LoomVerificationPanel = function ({state,$,node,has,request,publicError,runChecked,refresh}) {
  let binding="",version=0,busy=false,taskId="",preview=null,history=null,caps=null,timer=null,commandKey="";
  const hash=/^[0-9a-f]{64}$/;
  const names={queued:"等待复核执行器",running:"双模型复核中",verified:"独立复核通过",rejected:"模型发现问题，待人工处理",blocked:"复核已阻断，待人工核对"};
  const money=value=>`$${(value/1000000).toFixed(6)}`;
  const selection=()=>runChecked()?JSON.stringify([state.session,state.selection,state.runId]):"";
  const current=()=>Boolean(binding&&binding===selection()&&state.tab==='verification');
  const tasks=()=> (state.review?.tasks||[]).filter(t=>t.artifact?.kind==='python-json-program'&&t.artifact.trusted_evaluation===true&&['submitted','done'].includes(t.status));
  function reset(){version++;binding="";taskId="";busy=false;preview=history=caps=null;commandKey="";clearTimeout(timer);timer=null;$("verification-detail").replaceChildren();$("verification-task").replaceChildren();$("verification-reviewed").checked=false;$("verification-reason").value="";}
  function controls(){
    const locked=!current()||state.busy||busy;
    $("verification-read").disabled=locked||!taskId;$("verification-task").disabled=locked;
    const p=preview?.project_pool;
    const budget=p&&p.limit_microusd-p.spent_microusd-p.reserved_microusd>=preview.max_cost_microusd;
    $("verification-submit").disabled=locked||!has('control')||!preview?.ready||!budget||caps?.executor?.state!=='recent'||!$("verification-reviewed").checked||!$("verification-reason").value.trim();
    $("verification-form").hidden=!preview?.ready;
  }
  function render(){
    const root=$("verification-detail");root.replaceChildren();
    if(caps){const p=caps.project_pool;root.append(node('p','field-help',caps.configured?`复核执行器：${caps.executor?.state==='recent'?'近期在线':caps.executor?.state==='stale'?'最近未响应':'尚未观察到'}。状态检查不会调用模型。`:'本实例尚未配置双模型复核。'));
      if(p)root.append(node('p','field-help',`独立复核项目总额度 ${money(p.limit_microusd)} · 已记账 ${money(p.spent_microusd)} · 占额 ${money(p.reserved_microusd)}。生成任务预算另行计算。`));}
    if(preview){root.append(node('p','field-help',`本次使用 Flash 和 Pro，最多预留 ${money(preview.max_cost_microusd)}；按实际用量结算。结果未知继续占额，不自动重发。`),node('p','mono break-word',`源版本 ${preview.binding.content_hash}`));}
    for(const job of history?.requests||[]){
      const card=node('article','artifact-card');card.append(node('h3','',names[job.state]||'状态待核对'),node('p','mono break-word',`请求 ${job.job_id}`),node('p','field-help',`复核已记账 ${money(job.actual_cost_microusd)}${job.billing_uncertain?' · 账单未明，保留额度':''}`));
      if(job.reason)card.append(node('p','field-help',({VERIFICATION_PROJECT_BUDGET_EXHAUSTED:'独立复核额度不足。',VERIFICATION_RUN_NOT_OPEN:'运行已暂停、终止或进入人工处理。',VERIFICATION_SOURCE_CHANGED:'来源版本已变化。',MODEL_REVIEW_REJECTED:'至少一个模型发现问题。'})[job.reason]||'来源、模型结果或账单需人工核对；系统不会自动重试。'));
      if(job.task_verified===true&&job.signatures_authenticated===true)card.append(node('p','field-help','当前程序已通过两个不同模型的签名复核。目标还需全部任务及冻结判据通过。'));
      for(const proof of job.attestations||[])card.append(node('p','mono break-word',`${proof.body.receipt.model_id} · ${proof.body.passed?'通过':'发现问题'} · Ed25519 签名 ${job.signatures_authenticated?'已核对':'待核对'} · 回执 ${proof.body.receipt.response_hash}`));
      if(job.attestations?.length){const button=node('button','','下载签名证据');button.type='button';button.dataset.verificationExport='';button.addEventListener('click',()=>{if(!current()||busy)return;const u=URL.createObjectURL(new Blob([JSON.stringify({schema_version:'loom.verification-export/v1',run_id:state.runId,task_id:taskId,request:job,public_keys:history.public_keys},null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=u;a.download='loom-verification.json';a.click();setTimeout(()=>URL.revokeObjectURL(u),1000);});card.append(button);}
      root.append(card);
    }
    if(history&&!history.requests.length)root.append(node('p','field-help','此程序还没有独立复核请求。请先核对预算、源码和宿主测试，再明确申请。'));
    controls();
  }
  async function load(){
    if(!current()||busy||!taskId)return;
    clearTimeout(timer);const generation=++version,bound=binding,id=taskId,old=preview?.preview_digest;
    busy=true;preview=null;$("verification-state").textContent='正在核对复核条件与历史…';controls();
    const base=`/v1/runs/${encodeURIComponent(state.runId)}/tasks/${encodeURIComponent(id)}`;
    try{
      const c=await request('GET','/v1/verification-capabilities');
      if(generation!==version||bound!==binding||!current())return;
      if(c?.schema_version!=='loom.verification-capabilities/v1'||c.tenant_id!==state.principal.tenant_id||c.algorithm!=='Ed25519'||c.model_dispatched!==false)throw Object.assign(new Error(),{code:'BAD_JSON'});
      caps=c;history=null;
      if(c.configured){
        history=await request('GET',base+'/verification');
        if(generation!==version||bound!==binding||!current())return;
        if(history?.schema_version!=='loom.verification-history/v1'||history.run_id!==state.runId||history.task_id!==id||!Array.isArray(history.requests))throw Object.assign(new Error(),{code:'BAD_JSON'});
        if(state.snapshot.status==='open'&&!history.requests.length){
          preview=await request('GET',base+'/verification-preview');
          if(generation!==version||bound!==binding||!current())return;
          if(preview?.schema_version!=='loom.verification-preview/v1'||!hash.test(preview.preview_digest)||preview.binding.task_id!==id||preview.binding.run_id!==state.runId||preview.generation_budget_unchanged!==true||preview.model_dispatched!==false)throw Object.assign(new Error(),{code:'BAD_JSON'});
        }
      }
      if(preview?.preview_digest!==old){$("verification-reviewed").checked=false;commandKey='verification:'+crypto.randomUUID();}
      $("verification-state").textContent='已核对当前条件。读取与下载不会调用模型。';
    }catch(e){if(generation===version&&bound===binding){preview=history=null;$("verification-state").textContent=publicError(e);}}
    finally{if(generation===version&&bound===binding){busy=false;render();if(current()&&history?.requests.some(j=>['queued','running'].includes(j.state)))timer=setTimeout(load,2500);}}
  }
  async function submit(event){
    event.preventDefault();controls();if($("verification-submit").disabled)return;
    const generation=++version,bound=binding,id=taskId,p=preview;
    busy=true;controls();$("verification-state").textContent='正在提交独立复核请求…';
    try{await request('POST',`/v1/runs/${encodeURIComponent(state.runId)}/tasks/${encodeURIComponent(id)}/verification-requests`,{expected_preview_digest:p.preview_digest,reason:$("verification-reason").value.trim()},commandKey);
      if(generation===version&&bound===binding)$("verification-state").textContent='请求已入队，两个模型按冻结来源复核。';
    }catch(e){if(generation===version&&bound===binding)$("verification-state").textContent=publicError(e);}
    finally{if(generation===version&&bound===binding){busy=false;$("verification-reviewed").checked=false;await load();}}
  }
  function sync(){
    const next=selection();if(next!==binding){reset();binding=next;const list=tasks();taskId=list[0]?.id||'';for(const task of list){const option=node('option','',task.title||task.id);option.value=task.id;$("verification-task").append(option);}$("verification-state").textContent=list.length?'选择程序后读取复核条件。':'当前没有可复核的已测试程序。';}
    if(!current())clearTimeout(timer);else if(taskId&&!caps&&!busy)void load();controls();
  }
  $("verification-read").addEventListener('click',load);$("verification-form").addEventListener('submit',submit);
  $("verification-reviewed").addEventListener('change',controls);$("verification-reason").addEventListener('input',controls);
  $("verification-task").addEventListener('change',()=>{version++;busy=false;preview=history=caps=null;taskId=$("verification-task").value;$("verification-reviewed").checked=false;void load();});
  $("verification-workspace-refresh").addEventListener('click',()=>{if(!busy)void refresh();});
  return {reset,sync,load};
};
