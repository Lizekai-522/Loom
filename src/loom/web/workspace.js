"use strict";
(async () => {
 const $=id=>document.getElementById(id), api=window.VeriloWorkspace.request;
 const page=location.pathname.split('/').pop(), titles={projects:'项目',automations:'自动化',market:'插件市场'};
 const el=(tag,text,cls)=>{const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(cls)e.className=cls;return e;};
 const notice=text=>{$('notice').textContent=String(text||'');$('notice').hidden=!text;};
 const button=(text,action)=>{const b=el('button',text);b.type='button';b.addEventListener('click',async()=>{b.disabled=true;try{await action();notice('');}catch(e){notice(e.message);}finally{b.disabled=false;}});return b;};
 let items=[],projects=[],models=[],editing=null,submitEditor=null;
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
 },f=>{const interval=f.get('interval_minutes'),due=f.get('due_at');if(!interval&&!due)throw Error('请设置运行时间或重复间隔');return api(item?'PATCH':'POST','/v1/workspace/automations'+(item?'/'+item.id:''),{name:f.get('name'),prompt:f.get('prompt'),project_id:f.get('project_id')||null,model:f.get('model')||null,interval_minutes:interval?Number(interval):null,due_at:due?new Date(due).toISOString():null});});}
 async function renderProjects(){
   const d=await api('GET','/v1/workspace/projects');items=d.items;const list=el('div',undefined,'hub-list');
   for(const p of items){const c=el('article',undefined,'hub-card');c.append(el('h2',p.name),el('p',p.path,'hub-path'));const row=el('div',undefined,'row');row.append(button('使用项目',async()=>{await api('PATCH','/v1/workspace/preferences',{selected_project_id:p.id});location.assign('/console');}),button('重命名',()=>editProject(p)),button('移除',async()=>{await api('DELETE','/v1/workspace/projects/'+p.id);await load();}));c.append(row);list.append(c);}
   if(!items.length)list.append(el('p','创建一个项目，终端将在该项目目录中打开。','hub-empty'));$('hub-content').replaceChildren(list);
 }
 async function renderAutomations(){
   const d=await api('GET','/v1/workspace/automations');items=d.items;const list=el('div',undefined,'hub-list');
   for(const a of items){const c=el('article',undefined,'hub-card');c.append(el('h2',a.name),el('p',a.prompt),el('p',(a.enabled?'已启用':'已停用')+(a.next_run_at?' · '+new Date(a.next_run_at*1000).toLocaleString():'')+(a.interval_minutes?' · 每 '+a.interval_minutes+' 分钟':''),'hub-status'));const row=el('div',undefined,'row');row.append(button(a.enabled?'停用':'启用',async()=>{await api('POST','/v1/workspace/automations/'+a.id+'/'+(a.enabled?'disable':'enable'),{});await load();}),button('立即运行',async()=>{const r=await api('POST','/v1/workspace/automations/'+a.id+'/run-now',{});detail('已加入队列',r);}),button('编辑',()=>editAutomation(a)),button('运行记录',async()=>{detail('运行记录',await api('GET','/v1/workspace/automations/'+a.id+'/runs'));}),button('删除',async()=>{await api('DELETE','/v1/workspace/automations/'+a.id);await load();}));c.append(row);list.append(c);}
   if(!items.length)list.append(el('p','暂无自动化。新建后启用，后端将在设定时间派发任务。','hub-empty'));$('hub-content').replaceChildren(list);
 }
 async function renderMarket(){
   const d=await api('GET','/v1/market/catalog'),list=el('div',undefined,'hub-list');
   for(const p of d.items){const c=el('article',undefined,'hub-card');c.append(el('h2',p.name),el('p',p.description),el('p',p.installed?(p.enabled?'已启用 · 手动调用':'已停用'):'未安装','hub-status'));const row=el('div',undefined,'row');
    if(!p.installed)row.append(button('安装',async()=>{await api('POST','/v1/market/'+p.id+'/install',{});await load();}));
    else {row.append(button(p.enabled?'停用':'启用',async()=>{await api('PATCH','/v1/market/'+p.id,{enabled:!p.enabled});await load();}));if(p.enabled)row.append(button('调用',()=>{editor('调用 '+p.name,()=>{for(const name of p.parameters)field(name,name,'text','').required=true;},async f=>{const argumentsValue=Object.fromEntries(p.parameters.map(name=>[name,f.get(name)]));const result=await api('POST','/v1/market/'+p.id+'/invoke',{arguments:argumentsValue});setTimeout(()=>detail(p.name,result),0);});}));}
    c.append(row);list.append(c);
   }
   const advanced=el('a','MCP 与已登记插件');advanced.href='/console/settings#plugins';list.append(advanced);$('hub-content').replaceChildren(list);
 }
 async function load(){if(page==='projects')return renderProjects();if(page==='automations')return renderAutomations();if(page==='market')return renderMarket();}
 try{const [p,m]=await Promise.all([api('GET','/v1/workspace/projects'),api('GET','/v1/workspace/models')]);projects=p.items;models=m.items;if(page!=='market'){$('add').hidden=false;$('add').textContent=page==='projects'?'新建项目':'新建自动化';$('add').onclick=()=>page==='projects'?editProject():editAutomation();}await load();}catch(e){notice(e.message);}
})();
