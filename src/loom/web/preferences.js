"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const NAV = [
    ['基础设置','general','常规','⚙'],['','appearance','外观','◐'],['','models','模型设置','◇'],
    ['','browser','浏览器控制','◎'],['','devices','远程设备','▱'],['','computer','电脑控制','▣'],['','shortcuts','键盘快捷键','⌨'],
    ['Agent 能力','memory','记忆','◌'],['','agents','子智能体','♧'],['','plugins','插件','▦'],['','mcp','MCP 服务器','⑂'],
    ['','skills','技能','✧'],['','commands','命令','›_'],['','hooks','钩子','↳'],
    ['数据与统计','usage','使用统计','▥'],['','archives','归档管理','▤'],['','guide','引导','↗']
  ];
  let current = '', prefs = {}, renderToken = 0;
  function el(tag, cls, text) { const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=String(text);if(tag==='form')n.addEventListener('submit',e=>e.preventDefault());return n; }
  function button(text, action, cls='') { const n=el('button',cls,text);n.type='button';n.addEventListener('click',async()=>{n.disabled=true;try{await action();}catch(e){notice(e.message,true);}finally{n.disabled=false;}});return n; }
  function notice(text,error=false){$('notice').textContent=text;$('notice').hidden=!text;$('notice').className=error?'error':'';}
  async function request(method,path,body) {
    const headers={Accept:'application/json','X-Loom-Console':'local'};
    if(body!==undefined)headers['Content-Type']='application/json';
    if(method!=='GET')headers['Idempotency-Key']=crypto.randomUUID();
    const r=await fetch(path,{method,headers,credentials:'same-origin',cache:'no-store',body:body===undefined?undefined:JSON.stringify(body)});
    if(!r.ok){const error=new Error(r.status===401?'请重新打开 Verilo。':r.status===403?'当前身份没有操作权限。':r.status===409?'内容已变化或额度已满，请刷新后重试。':r.status===422?'请核对输入内容。':'暂时无法完成，请稍后重试。');error.status=r.status;throw error;}
    return r.status===204?null:r.json();
  }
  function section(title,help){const n=el('section','section');if(title)n.append(el('h2',null,title));if(help)n.append(el('p','help',help));return n;}
  function field(title,input){const n=el('label',null,title);input.setAttribute('aria-label',title);n.append(input);return n;}
  function input(value='',placeholder=''){const n=el('input');n.value=value;n.placeholder=placeholder;return n;}
  function select(options,value){const n=el('select');for(const [id,title]of options){const o=el('option',null,title);o.value=id;n.append(o);}n.value=value;return n;}
  function checkbox(value,title){const n=el('input');n.type='checkbox';n.checked=!!value;const l=el('label','check');l.append(n,el('span',null,title));return {input:n,label:l};}
  function applyAppearance(){const appearance=prefs.appearance||'light';document.documentElement.dataset.theme=appearance==='system'?(matchMedia('(prefers-color-scheme:dark)').matches?'dark':'light'):appearance;document.documentElement.style.setProperty('--size',(prefs.font_size||14)+'px');document.body.style.fontFamily=prefs.font==='mono'?'ui-monospace,Consolas,monospace':'system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif';}
  async function savePrefs(body){prefs=await request('PATCH','/v1/workspace/preferences',body);applyAppearance();notice('已保存。');}
  function buildNav(){for(const [group,id,title,icon]of NAV){if(group)$('navigation').append(el('div','nav-group',group));const a=el('a','nav-item');a.href='#'+id;a.dataset.page=id;a.append(el('span','nav-icon',icon),el('span',null,title));$('navigation').append(a);}}
  function empty(parent,text='暂无记录'){parent.append(el('p','empty',text));}
  function download(name,value){const blob=new Blob([JSON.stringify(value,null,2)],{type:'application/json;charset=utf-8'}),url=URL.createObjectURL(blob),a=el('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
  async function render(){
    const raw=location.hash.slice(1)||'general',id=raw==='model'?'models':raw,nav=NAV.find(n=>n[1]===id);if(!nav){location.hash='general';return;}
    current=id;const token=++renderToken;notice('');$('page-title').textContent=nav[2];document.title='Verilo · '+nav[2];
    for(const a of document.querySelectorAll('[data-page]')){if(a.dataset.page===id)a.setAttribute('aria-current','page');else a.removeAttribute('aria-current');}
    const host=el('div');$('content').replaceChildren(host);
    try{
      if(id==='general')general(host);
      else if(id==='appearance')appearance(host);
      else if(id==='models')await models(host);
      else if(id==='shortcuts')shortcuts(host);
      else if(['memory','skills','commands','hooks'].includes(id))await entryPage(host,id);
      else if(id==='agents')await agents(host);
      else if(id==='plugins')await plugins(host);
      else if(id==='mcp')await mcp(host);
      else if(id==='usage')await usage(host);
      else if(id==='archives')await archives(host);
      else if(id==='guide')guide(host);
      else await capability(host,id);
    }catch(e){if(token===renderToken)notice(e.message,true);}
  }
  function general(host){
    const s=section('任务执行');const mode=select([['ask','变更前确认'],['read_only','只读']],prefs.confirmation_mode||'ask');
    const f=el('form','form');f.append(field('默认权限',mode),el('p','help','只读模式阻止终端输入和新建编程任务。更改权限不会取消预算、隔离或独立复核要求。'));
    f.append(button('保存',()=>savePrefs({confirmation_mode:mode.value}),'primary'));s.append(f);host.append(s);
    const account=section('当前账户');account.append(el('p','help','个人对话、记忆、模板与归档按账户隔离。模型由服务端统一提供。'),el('a',null,'返回工作区'));account.lastChild.href='/console';host.append(account);
  }
  function appearance(host){
    const s=section('显示');const form=el('form','form');const theme=select([['light','浅色'],['dark','深色'],['system','跟随系统']],prefs.appearance||'light');
    const font=select([['system','系统字体'],['sans','无衬线'],['mono','等宽']],prefs.font||'system');const size=input(String(prefs.font_size||14));size.type='number';size.min='12';size.max='24';
    form.append(field('主题',theme),field('字体',font),field('字号',size),button('保存',()=>savePrefs({appearance:theme.value,font:font.value,font_size:Number(size.value)}),'primary'));s.append(form);host.append(s);
  }
  async function models(host){
    const list=await request('GET','/v1/workspace/models');const s=section('可用模型','由服务端统一配置，密钥不会发送到客户端。');
    if(!list.items?.length){empty(s,'服务端尚未配置可用模型。');host.append(s);return;}
    const model=select(list.items.map(m=>[m.alias,m.alias]),prefs.preferred_model||list.items[0].alias);const f=el('div','form');f.append(field('默认对话模型',model),button('保存',()=>savePrefs({preferred_model:model.value}),'primary'));s.append(f);host.append(s);
  }
  function shortcuts(host){const s=section('快捷键');const layout=select([['default','默认'],['vim','Vim（基础导航）'],['emacs','Emacs（基础编辑）']],prefs.shortcuts||'default');const form=el('div','form');form.append(field('编辑模式',layout),el('p','help','编辑模式提供消息输入框的基础键位。Ctrl+N 新建任务，Ctrl+` 切换终端，Enter 发送，Shift+Enter 换行。'),button('保存',()=>savePrefs({shortcuts:layout.value}),'primary'));s.append(form);host.append(s);}
  async function entryPage(host,kind){
    const labels={memory:['个人记忆','保存希望助手长期记住的事实和偏好。'],skills:['技能模板','保存可复用的指令，使用时发送到当前账户的助手对话。'],commands:['命令模板','这些命令发送给助手执行，不会直接运行系统 Shell。'],hooks:['通知钩子','只支持回复或任务完成时的浏览器通知，页面需保持打开并允许通知。']};
    const [title,help]=labels[kind],s=section(title,help);const items=(await request('GET','/v1/product/entries/'+kind)).items||[],list=el('div');s.append(list);host.append(s);
    const editor=section('新增');const form=el('form','form');const titleInput=input();titleInput.maxLength=120;titleInput.required=true;const content=el('textarea');content.maxLength=4000;content.rows=5;
    const event=select([['reply.completed','回复完成'],['task.completed','任务完成']],'reply.completed'),enabled=checkbox(kind!=='hooks','启用');let editing=null;
    form.append(field('名称',titleInput));if(kind==='hooks')form.append(field('事件',event));else form.append(field(kind==='memory'?'记忆内容':'指令',content));form.append(enabled.label);
    form.append(button('保存',async()=>{const body={title:titleInput.value.trim(),content:kind==='hooks'?'':content.value,enabled:enabled.input.checked,config:kind==='hooks'?{event:event.value,action:'browser.notification'}:{}};if(!body.title||(!body.content&&kind!=='hooks'))throw Error('请填写名称和内容。');if(editing)await request('PATCH',`/v1/product/entries/${kind}/${editing.id}`,{...body,expected_revision:editing.revision});else await request('POST','/v1/product/entries/'+kind,body);await render();notice('已保存。');},'primary'));
    editor.append(form);host.append(editor);
    if(!items.length)empty(list);
    for(const item of items){
      const row=el('div','item'),head=el('div','row between'),main=el('div','item-main');main.append(el('h3',null,item.title),el('p','help',kind==='hooks'?({ 'reply.completed':'回复完成', 'task.completed':'任务完成'}[item.config.event]+' · 浏览器通知'):item.content));
      const actions=el('div','item-actions');actions.append(button(item.enabled?'停用':'启用',async()=>{await request('PATCH',`/v1/product/entries/${kind}/${item.id}`,{expected_revision:item.revision,enabled:!item.enabled});await render();}));
      actions.append(button('编辑',()=>{editing=item;titleInput.value=item.title;content.value=item.content;enabled.input.checked=item.enabled;if(kind==='hooks')event.value=item.config.event;editor.querySelector('h2').textContent='编辑';titleInput.focus();}));
      if(['commands','skills'].includes(kind))actions.append(button('使用',async()=>{await request('POST',`/v1/product/entries/${kind}/${item.id}/run`,{input:''});location.assign('/console');}));
      actions.append(button('删除',async()=>{await request('DELETE',`/v1/product/entries/${kind}/${item.id}?expected_revision=${item.revision}`);await render();},'danger'));head.append(main,actions);row.append(head);list.append(row);
    }
    if(kind==='hooks')s.append(button('允许浏览器通知',async()=>{if(!('Notification'in window))throw Error('当前浏览器不支持通知。');const permission=await Notification.requestPermission();notice(permission==='granted'?'已允许通知。':'通知未获允许。');}));
  }
  async function capability(host,id){const c=await request('GET','/v1/product/capabilities');const item=c[id==='devices'?'remote_devices':id]||{};const s=section('连接状态');const box=el('div','unavailable');box.append(el('span','pill',item.available?'已连接':'未接入'),el('p','help',item.reason||'需要对应的设备桥接器。'));s.append(box);host.append(s);
    if(id==='browser'){const f=el('div','form'),url=input('','https://');url.type='url';const list=el('div');f.append(field('预览地址',url),button('读取并点选',async()=>{const preview=await request('POST','/v1/assistant/browser/preview',{url:url.value});list.replaceChildren();for(const element of preview.elements||[]){list.append(button((element.text||element.selector),async()=>{const chosen=await request('POST','/v1/assistant/browser/pick',{preview_id:preview.id,selector:element.selector});notice(chosen.selector+' '+chosen.text);}));}if(!(preview.elements||[]).length)list.append(el('p','help',preview.text||'没有可点选的元素。'));},'primary'),list);host.append(f);}
    if(id==='computer'){const box=el('div');host.append(box);const status=await request('GET','/v1/computer/status').catch(error=>({enabled:false,available:false,reason:error.message}));box.append(el('p','help',status.reason||(status.available?'可以采集本机画面。':'当前系统不能采集画面。')));const row=el('div','row');let session=status.session_id||null;row.append(button('启用预览',async()=>{const next=await request('POST','/v1/computer/enable',{allow_input:false});session=next.session_id;notice('预览已启用。');}),button('启用键鼠',async()=>{const next=await request('POST','/v1/computer/enable',{allow_input:true});session=next.session_id;notice('键鼠已启用，紧急停止 '+ (next.emergency_stop||''));}),button('停止',async()=>{await request('POST','/v1/computer/disable',{});session=null;notice('已停止。');}),button('刷新画面',async()=>{if(!session)throw Error('先启用预览。');const response=await fetch('/v1/computer/frame',{headers:{'X-Loom-Console':'local','X-Verilo-Computer-Session':session,Accept:'image/png'},credentials:'same-origin',cache:'no-store'});if(!response.ok)throw Error('无法读取画面。');const img=el('img');img.alt='当前画面';img.src=URL.createObjectURL(await response.blob());const old=box.querySelector('img');if(old)old.remove();box.append(img);}));box.append(row);}
    if(id==='devices')host.append(el('p','help','当前为本机安装。尚无可配对的远端服务，不会把本机工作台直接暴露到公网。'));
  }
  async function agents(host){const c=await request('GET','/v1/product/capabilities'),s=section('编程流水线角色','以下角色由编程任务调度器执行。');for(const name of c.subagents.roles){const row=el('div','item row between');row.append(el('span',null,name),el('span','pill','内置'));s.append(row);}host.append(s);const unsupported=section('独立子会话');unsupported.append(el('p','help',c.subagents.reason));host.append(unsupported);const registered=await request('GET','/v1/agents');if(registered.items?.length){const list=section('已注册角色');for(const a of registered.items)list.append(el('div','item',a.agent_id+' · '+a.base_role));host.append(list);}}
  async function plugins(host){const data=await request('GET','/v1/plugins'),s=section('HTTP 插件','只有已登记的 GET 接口可以按现有权限被调用。');if(!data.items?.length)empty(s,'尚无已注册的 HTTP 插件。');for(const item of data.items||[]){const row=el('div','item row between');const main=el('div','item-main');main.append(el('h3',null,item.plugin_id),el('p','help',item.base_url));row.append(main,el('span','pill',item.enabled?'启用':'停用'));s.append(row);}host.append(s);const a=el('a',null,'打开插件市场');a.href='/console/market';host.append(a);}
  async function mcp(host){const data=await request('GET','/v1/mcp-servers'),s=section('已注册服务器','登记后可发现工具；工具执行仍由现有权限控制。');if(!data.items?.length)empty(s);for(const item of data.items||[]){const row=el('div','item');row.append(el('h3',null,item.name),el('p','help',item.url));row.append(button('发现工具',async()=>{const result=await request('GET',`/v1/mcp-servers/${encodeURIComponent(item.name)}/tools`);const old=row.querySelector('pre');if(old)old.remove();row.append(el('pre',null,JSON.stringify(result,null,2)));}));s.append(row);}host.append(s);const add=section('注册服务器'),f=el('div','form'),name=input(),url=input();name.maxLength=48;url.maxLength=500;f.append(field('名称',name),field('HTTPS 地址',url),button('注册',async()=>{await request('POST','/v1/mcp-servers',{name:name.value,url:url.value});await render();notice('已注册。');},'primary'));add.append(f);host.append(add);}
  async function usage(host){const d=await request('GET','/v1/product/usage'),s=section('当前账户','根据已落盘的执行账本统计，费用为美元。');const grid=el('div','stats');for(const [title,value]of [['已结算','$'+(d.spent_microusd/1e6).toFixed(4)],['预留预算','$'+(d.reserved_microusd/1e6).toFixed(4)],['Token',d.tokens],['消息',d.messages],['任务',d.tasks],['编程会话',d.coding_sessions]]){const stat=el('div','stat');stat.append(el('strong',null,value),el('span',null,title));grid.append(stat);}s.append(grid);host.append(s);}
  async function archives(host){const data=await request('GET','/v1/product/archives'),s=section('对话快照','保存当前账户完整对话的只读副本。删除快照不会删除原对话。');const f=el('div','form'),title=input();title.maxLength=120;f.append(field('快照名称',title),button('保存当前对话',async()=>{if(!title.value.trim())throw Error('请填写快照名称。');await request('POST','/v1/product/archives',{title:title.value.trim()});await render();notice('快照已保存。');},'primary'));s.append(f);host.append(s);const list=section('已保存');if(!data.items?.length)empty(list);for(const item of data.items||[]){const row=el('div','item row between'),main=el('div','item-main');main.append(el('h3',null,item.title),el('p','help',new Date(item.created_at*1000).toLocaleString()));const actions=el('div','item-actions');actions.append(button('导出',async()=>download('verilo-conversation.json',await request('GET','/v1/product/archives/'+item.id))),button('删除',async()=>{await request('DELETE','/v1/product/archives/'+item.id);await render();},'danger'));row.append(main,actions);list.append(row);}host.append(list);}
  function guide(host){const s=section('开始使用');const steps=[['新建任务','从工作区侧栏创建编程任务。'],['选择项目和模型','项目决定本机终端目录，模型使用服务端已配置的选项。'],['描述需求','输入目标，也可以添加文本或代码文件。'],['查看执行结果','任务面板展示进度、需要确认的变更和交付结果。']];steps.forEach(([title,body],i)=>{const row=el('div','step'),text=el('div');text.append(el('h3',null,title),el('p','help',body));row.append(el('b',null,String(i+1).padStart(2,'0')),text);s.append(row);});host.append(s);const a=el('a',null,'返回工作区');a.href='/console';host.append(a);}
  document.addEventListener('DOMContentLoaded',async()=>{buildNav();try{prefs=await request('GET','/v1/workspace/preferences');applyAppearance();await render();}catch(e){notice(e.message,true);}});
  window.addEventListener('hashchange',render);
})();
