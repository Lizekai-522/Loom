"use strict";
(async () => {
 const $=id=>document.getElementById(id), api=window.VeriloWorkspace.request;
 const page=location.pathname.split('/').pop(), titles={projects:'项目',automations:'自动化',market:'插件市场'};
 const el=(tag,text,cls)=>{const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(cls)e.className=cls;return e;};
 const notice=text=>{$('notice').textContent=String(text||'');$('notice').hidden=!text;};
 const button=(text,action)=>{const b=el('button',text);b.type='button';b.addEventListener('click',async()=>{b.disabled=true;try{await action();notice('');}catch(e){notice(e.message);}finally{b.disabled=false;}});return b;};
 let items=[],projects=[],models=[],editing=null,submitEditor=null,query='',autoFilter='all',selectedAuto=null;
 $('hub-title').textContent=titles[page]||'工作区';document.title='Verilo · '+$('hub-title').textContent;
 document.querySelector('a[href="'+location.pathname+'"]')?.setAttribute('aria-current','page');
 $('editor-close').onclick=()=>$('editor').close();$('details-close').onclick=()=>$('details').close();
 function field(label,name,type,value,options){const l=el('label',label),i=document.createElement(type==='textarea'?'textarea':type==='select'?'select':'input');i.name=name;i.id='edit-'+name;if(type!=='textarea'&&type!=='select')i.type=type;if(options)for(const [v,t] of options){const o=el('option',t);o.value=v;i.append(o);}if(value!==undefined&&value!==null)i.value=value;l.append(i);$('editor-fields').append(l);return i;}
 function editor(title,render,submit){$('editor-title').textContent=title;$('editor-fields').replaceChildren();render();submitEditor=submit;$('editor').showModal();}
 $('editor-form').addEventListener('submit',async e=>{e.preventDefault();$('editor-save').disabled=true;try{await submitEditor(new FormData(e.target));$('editor').close();await load();notice('');}catch(error){notice(error.message);$('editor').close();}finally{$('editor-save').disabled=false;}});
 function detail(title,data){$('details-title').textContent=title;$('details-body').replaceChildren(el('pre',typeof data==='string'?data:JSON.stringify(data,null,2)));$('details').showModal();}
 function editProject(item){editor(item?'重命名项目':'新建项目',()=>{const i=field('项目名称','name','text',item?.name||'');i.required=true;i.maxLength=120;},f=>api(item?'PATCH':'POST','/v1/workspace/projects'+(item?'/'+item.id:''),{name:f.get('name')}));}
 function editAutomation(item){editing=item;editor(item?'编辑自动化':'新建自动化',()=>{
   let i=field('名称','name','text',item?.name||'');i.required=true;i.maxLength=120;
   i=field('任务内容','prompt','textarea',item?.prompt||'');i.required=true;i.maxLength=4000;
   field('项目','project_id','select',item?.project_id||'',[['','不指定项目'],...projects.map(p=>[p.id,p.name])]);
   field('模型','model','select',item?.model||'',[['','使用当前默认模型'],...models.map(m=>[m.alias,m.alias])]);
   field('重复间隔（分钟，留空为单次）','interval_minutes','number',item?.interval_minutes||'').min=1;
   const local=item?.due_at?new Date(item.due_at*1000-new Date().getTimezoneOffset()*60000).toISOString().slice(0,16):'';
   field('首次运行时间','due_at','datetime-local',local);
   field('准入','mode','select',item?.mode||'observe',[['off','off'],['observe','observe'],['auto','auto']]);
 },f=>{const interval=f.get('interval_minutes'),due=f.get('due_at');if(!interval&&!due)throw Error('请设置运行时间或重复间隔');return api(item?'PATCH':'POST','/v1/workspace/automations'+(item?'/'+item.id:''),{name:f.get('name'),prompt:f.get('prompt'),project_id:f.get('project_id')||null,model:f.get('model')||null,interval_minutes:interval?Number(interval):null,due_at:due?new Date(due).toISOString():null,mode:f.get('mode')||'observe'});});}
 function needle(item){const q=query.trim().toLowerCase();if(!q)return true;return [item.name,item.path,item.prompt,item.description,item.id].some(value=>String(value||'').toLowerCase().includes(q));}
 function ensureTools(){const host=$('hub-tools');if(host.childElementCount)return;const input=el('input');input.type='search';input.className='app-search';input.placeholder=page==='market'?'搜索插件':'搜索名称';input.setAttribute('aria-label',input.placeholder);input.value=query;input.addEventListener('input',()=>{query=input.value;load();});host.append(input);if(page!=='automations')return;const filters=el('div',undefined,'hub-filters');filters.setAttribute('role','group');filters.setAttribute('aria-label','状态');for(const [id,label] of [['all','全部'],['enabled','已启用'],['paused','已停用']]){const b=el('button',label);b.type='button';b.dataset.hubFilter=id;b.setAttribute('aria-pressed',String(autoFilter===id));b.addEventListener('click',()=>{autoFilter=id;for(const item of filters.querySelectorAll('button'))item.setAttribute('aria-pressed',String(item===b));load();});filters.append(b);}host.append(filters);}
 function rowCard(title,summary,actions,mark){const row=el('article',undefined,'hub-row');if(mark)row.append(el('span',mark,'hub-icon'));const text=el('div',undefined,'hub-maintext');text.append(el('h2',title),el('p',summary));row.append(text);if(actions)row.append(actions);return row;}
 async function renderProjects(){
   $('hub-detail').hidden=true;const d=await api('GET','/v1/workspace/projects');items=d.items.filter(needle);const list=el('div',undefined,'hub-list');
   for(const p of items){const actions=el('div',undefined,'row');actions.append(button('使用项目',async()=>{await api('PATCH','/v1/workspace/preferences',{selected_project_id:p.id});location.assign('/console');}),button('重命名',()=>editProject(p)),button('移除',async()=>{await api('DELETE','/v1/workspace/projects/'+p.id);await load();}));list.append(rowCard(p.name,p.path,actions,p.name.slice(0,1)));}
   if(!items.length)list.append(el('p',query?'没有匹配的项目。':'创建一个项目，终端将在该项目目录中打开。','hub-empty'));$('hub-content').replaceChildren(list);
 }
 function paintAutomation(item){const host=$('hub-detail');host.hidden=false;host.replaceChildren();if(!item){host.append(el('p','选择一条自动化查看详情。','hub-empty'));return;}host.append(el('h2',item.name),el('p',item.prompt),el('p',(item.enabled?'已启用':'已停用')+' · '+(item.mode||'observe')+(item.next_run_at?' · '+new Date(item.next_run_at*1000).toLocaleString():'')+(item.interval_minutes?' · 每 '+item.interval_minutes+' 分钟':'')));const modes=el('div',undefined,'row');for(const mode of ['off','observe','auto']){const b=button(mode,async()=>{await api('PATCH','/v1/workspace/automations/'+item.id,{mode});await load();});if((item.mode||'observe')===mode)b.setAttribute('aria-pressed','true');modes.append(b);}host.append(modes);const row=el('div',undefined,'row');row.append(button(item.enabled?'停用':'启用',async()=>{await api('POST','/v1/workspace/automations/'+item.id+'/'+(item.enabled?'disable':'enable'),{});await load();}),button('立即运行',async()=>{const r=await api('POST','/v1/workspace/automations/'+item.id+'/run-now',{});detail('已加入队列',r);}),button('编辑',()=>editAutomation(item)),button('运行记录',async()=>{detail('运行记录',await api('GET','/v1/workspace/automations/'+item.id+'/runs'));}),button('删除',async()=>{await api('DELETE','/v1/workspace/automations/'+item.id);selectedAuto=null;await load();}));host.append(row);}
 async function renderAutomations(){
   const d=await api('GET','/v1/workspace/automations');items=d.items.filter(item=>needle(item)&&(autoFilter==='all'||(autoFilter==='enabled')===!!item.enabled));const list=el('div',undefined,'hub-list');
   if(!items.some(item=>item.id===selectedAuto))selectedAuto=items[0]?.id||null;
   for(const a of items){const summary=(a.enabled?'已启用':'已停用')+' · '+(a.mode||'observe')+(a.next_run_at?' · '+new Date(a.next_run_at*1000).toLocaleString():'');const open=el('button',undefined,'hub-row');open.type='button';open.setAttribute('aria-current',String(a.id===selectedAuto));const text=el('div',undefined,'hub-maintext');text.append(el('h2',a.name),el('p',summary));open.append(text);open.addEventListener('click',()=>{selectedAuto=a.id;renderAutomations();});list.append(open);}
   if(!items.length)list.append(el('p',query?'没有匹配的自动化。':'暂无自动化。新建后启用，后端将在设定时间派发任务。','hub-empty'));$('hub-content').replaceChildren(list);paintAutomation(items.find(item=>item.id===selectedAuto)||null);
 }
 async function renderMarket(){
   $('hub-detail').hidden=true;const d=await api('GET','/v1/market/catalog'),list=el('div',undefined,'hub-list');
   for(const p of d.items.filter(needle)){const actions=el('div',undefined,'row');
    if(!p.installed)actions.append(button('安装',async()=>{await api('POST','/v1/market/'+p.id+'/install',{});await load();}));
    else {actions.append(button(p.enabled?'停用':'启用',async()=>{await api('PATCH','/v1/market/'+p.id,{enabled:!p.enabled});await load();}));if(p.enabled)actions.append(button('调用',()=>{editor('调用 '+p.name,()=>{for(const name of p.parameters)field(name,name,'text','').required=true;},async f=>{const argumentsValue=Object.fromEntries(p.parameters.map(name=>[name,f.get(name)]));const result=await api('POST','/v1/market/'+p.id+'/invoke',{arguments:argumentsValue});setTimeout(()=>detail(p.name,result),0);});}));}
    list.append(rowCard(p.name,p.description||'',actions,(p.name||'?').slice(0,1)));const status=el('p',p.installed?(p.enabled?'已启用 · 手动调用':'已停用'):'未安装');list.lastElementChild.querySelector('.hub-maintext').append(status);
   }
   if(!list.childElementCount)list.append(el('p',query?'没有匹配的插件。':'插件市场暂无条目。','hub-empty'));
   const advanced=el('a','MCP 与已登记插件');advanced.href='/console/settings#plugins';list.append(advanced);$('hub-content').replaceChildren(list);
 }
 async function load(){ensureTools();if(page==='projects')return renderProjects();if(page==='automations')return renderAutomations();if(page==='market')return renderMarket();}
 try{const [p,m]=await Promise.all([api('GET','/v1/workspace/projects'),api('GET','/v1/workspace/models')]);projects=p.items;models=m.items;if(page!=='market'){$('add').hidden=false;$('add').textContent=page==='projects'?'新建项目':'新建自动化';$('add').onclick=()=>page==='projects'?editProject():editAutomation();}await load();}catch(e){notice(e.message);}
})();
