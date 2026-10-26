"use strict";
window.LoomProgression = function ({state, $, node, request, publicError, runChecked, label, navigate, executionPanel}) {
  const stages={contract_unavailable:"执行合同缺失或不受支持",plan_approval_required:"先审阅并批准计划",verified:"已通过受信验收",
    independent_verification_required:"候选已提交，等待独立验证",review_routing_required:"审查意见待处理",awaiting_operator:"待操作员处理",
    awaiting_independent_models:"等待独立模型复核",fixer_input_approved:"已批准单次修复输入",review_source_unavailable:"审查来源无法核对",
    running:"任务执行中",attempt_requires_review:"已有尝试，需人工核对",run_control_required:"运行未开放，请核对控制状态",
    effect_reconciliation_required:"先核对未知调用或未结算预留",independent_model_pending:"独立模型待配置",
    hard_dependencies_blocked:"等待上游独立验收",input_approval_required:"先审阅并批准辅助输入",budget_insufficient:"预算不足，禁止发起",
    local_execution_ready:"可准备本机执行",host_tests_required:"准备执行并核对冻结终验",source_or_contract_changed:"输入或合同已变化，请重新核对",
    no_fix_findings:"审查未发现问题，不批准修复"};
  const actions={view_evidence:"查看任务证据",view_plan:"审阅计划",view_candidate:"审阅候选",review_decision:"处理审查意见",
    view_inputs:"核对目标输入",view_interventions:"查看人工待办",view_execution:"准备本机执行"};
  let binding="",generation=0,pending=false,data=null,error="";
  const ready=()=>runChecked()&&!state.busy;
  function reset(){executionPanel.reset();generation++;binding="";pending=false;data=null;error="";$("progression-list").replaceChildren();$("progression-roles").replaceChildren();$("progression-execution").replaceChildren();}
  function controls(){
    $("progression-refresh").disabled=!ready()||pending;
    for(const button of $("progression-list").querySelectorAll('button'))button.disabled=!ready()||pending||Boolean(error);
    $("progression-state").textContent=pending?"正在核对七角色任务与下一步…":error||(!data?"选择运行后读取推进指引。":`${data.nodes.length} 个实际任务 · ${data.nodes.filter(n=>n.status==='done').length} 个已验证；点击操作可进入对应证据或准备环节。`);
  }
  function render(){
    $("progression-list").replaceChildren();$("progression-roles").replaceChildren();
    if(data){
      for(const role of data.roles)$("progression-roles").append(node('span','badge',`${label(role.role)} ${role.task_count} 项`));
      if(!data.nodes.length)$("progression-list").append(node('p','field-help','当前计划尚未创建任务，请先保存并批准计划。'));
      for(const item of data.nodes){
        const row=node('article','artifact-card');row.dataset.taskId=item.task_id;
        row.append(node('h3','',`${label(item.role)||'未分配角色'} · ${item.title}`),node('p','field-help',stages[item.stage]||'状态待核对'));
        if(item.blocked_by.length)row.append(node('p','muted',`仍有 ${item.blocked_by.length} 个硬依赖未通过验收；批准输入不会放行依赖。`));
        const button=node('button','',actions[item.action]||'查看任务证据');button.type='button';
        button.addEventListener('click',()=>{if(button.disabled)return;if(item.action==='view_execution')execution(item);else navigate(item);});
        row.append(button);$("progression-list").append(row);
        if(item.role&&item.role!=='validator'&&item.action!=='view_execution'){
          const history=node('button','','查看执行请求');history.type='button';history.addEventListener('click',()=>{if(!history.disabled)execution(item);});row.append(history);
        }
      }
    }
    controls();
  }
  function execution(item){
    const panel=$("progression-execution");panel.replaceChildren();
    panel.append(node('h3','',`${label(item.role)} · 本机执行准备`),node('p','field-help',item.requires_host_tests
      ?'此节点需要本机独立冻结的宿主终验文件。先读取条件，再显式提交给本机执行器；页面不会加载用例正文或模型密钥。'
      :'此节点只生成结构化候选。读取准备不会调用模型，下方显式提交后由本机执行器处理。'));
    const preparation={schema_version:'loom.node-preparation/v1',run_id:data.run_id,node_id:item.node_id,role:item.role,
      expected_plan_digest:data.plan_digest,evaluation_digest:item.evaluation_digest,max_output_tokens:1024,
      preview_digest:item.execution_preview_digest,requires_host_tests:item.requires_host_tests,requires_explicit_execute:true,counts_as_success:false};
    panel.append(node('pre','',JSON.stringify(preparation,null,2)),node('p','field-help','统一入口：scripts/run_approved_node.py。准备清单不含凭据，不是执行授权或成功回执；执行前会重新核对计划、依赖、输入和预算。'));
    executionPanel.open(item,panel);
    panel.scrollIntoView({behavior:'auto',block:'nearest'});
  }
  async function load(){
    if(!ready()||pending||!binding)return;
    const version=++generation,bound=binding,runId=state.runId;pending=true;error="";executionPanel.reset();$("progression-execution").replaceChildren();controls();
    try{
      const result=await request('GET',`/v1/runs/${encodeURIComponent(runId)}/progression`);
      if(bound!==binding||version!==generation)return;
      if(result?.schema_version!=='loom.progression/v1'||result.run_id!==runId||result.tenant_id!==state.principal?.tenant_id
        ||result.model_dispatched!==false||result.counts_as_success!==false||!Array.isArray(result.roles)||result.roles.length!==7
        ||!Array.isArray(result.nodes)||result.nodes.length>128||result.nodes.some(n=>!Object.hasOwn(actions,n.action)||!Array.isArray(n.blocked_by)||!Array.isArray(n.suggested_inputs)))throw Object.assign(new Error(),{code:'BAD_JSON'});
      data=result;
    }catch(e){if(bound===binding&&version===generation&&e.code!=='SESSION_CHANGED'){data=null;error=publicError(e);}}
    finally{if(bound===binding&&version===generation){pending=false;render();}}
  }
  function sync(){
    const next=state.runId&&state.loadedSelection===state.selection?JSON.stringify([state.session,state.selection,state.runId]):"";
    if(next!==binding){reset();binding=next;}
    if(binding&&state.tab==='progression'&&!data&&!pending&&!error&&ready())void load();
    controls();
  }
  $("progression-refresh").addEventListener('click',load);
  return {reset,sync,load};
};
