"use strict";
(() => {
  window.VeriloStyleElement = element => { const nonce=document.querySelector('meta[name="verilo-style-nonce"]')?.content; if(nonce)element.nonce=nonce; return element; };
  const state = {preferences: {}, projects: [], models: [], terminal: null, terminalId: null, terminalWorktreeId: null, cursor: 0, poll: null, fit: null, writing: Promise.resolve(), hooks: [], notified: new Set()};
  const $ = id => document.getElementById(id);
  const node = (tag, text, cls) => { const e=document.createElement(tag); if(text!==undefined)e.textContent=text; if(cls)e.className=cls; return e; };
  const key = () => crypto.randomUUID();
  async function request(method,path,body) {
    const headers={'Accept':'application/json','X-Loom-Console':'local'};
    if(body!==undefined)headers['Content-Type']='application/json';
    if(method!=='GET')headers['Idempotency-Key']=key();
    const r=await fetch(path,{method,headers,body:body===undefined?undefined:JSON.stringify(body),credentials:'same-origin',cache:'no-store'});
    const data=await r.json().catch(()=>({}));
    if(!r.ok)throw new Error(data.message || (Array.isArray(data.detail)?data.detail.map(d=>d.loc.join('.')+': '+d.msg).join('；'):data.detail) || data.code || ('请求失败 '+r.status));
    return data;
  }
  function notice(text) {
    const e=$('workspace-notice');if(e){e.textContent=String(text);e.hidden=!text;}
  }
  function applyPreferences(prefs) {
    state.preferences=prefs;
    const dark=prefs.appearance==='dark'||prefs.appearance==='system'&&matchMedia('(prefers-color-scheme: dark)').matches;
    document.documentElement.dataset.appearance=dark?'dark':'light';
    document.documentElement.style.setProperty('--user-font-size',(prefs.font_size||14)+'px');
    const textFont='-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Helvetica Neue", Helvetica, Arial, sans-serif';
    document.documentElement.style.setProperty('--user-font',prefs.font==='mono'?'ui-monospace, "SF Mono", Consolas, "Liberation Mono", Menlo, Courier':'system-ui, '+textFont);
    window.dispatchEvent(new CustomEvent('verilo:preferences'));
    for(const [id,field] of [['workspace-project','selected_project_id'],['workspace-model','preferred_model'],['workspace-confirm','confirmation_mode']]) {
      if($(id))$(id).value=prefs[field]||($(id).options[0]?.value||'');
    }
  }
  async function savePreference(field,value) {
    try {applyPreferences(await request('PATCH','/v1/workspace/preferences',{[field]:value||null})); notice('');}
    catch(e){applyPreferences(state.preferences);notice(e.message);}
  }
  async function reload() {
    const [prefs,projects,models]=await Promise.all([request('GET','/v1/workspace/preferences'),request('GET','/v1/workspace/projects'),request('GET','/v1/workspace/models')]);
    state.projects=projects.items;state.models=models.items;
    if($('workspace-project')) {
      const s=$('workspace-project');s.replaceChildren();const empty=node('option','选择项目');empty.value='';s.append(empty);
      for(const p of projects.items){const o=node('option',p.name);o.value=p.id;s.append(o);}
    }
    if($('workspace-model')) {
      const s=$('workspace-model');s.replaceChildren();
      for(const m of models.items){const o=node('option',m.alias);o.value=m.alias;s.append(o);}
      if(!models.items.length){const o=node('option','暂无已配置模型');o.value='';s.append(o);s.disabled=true;}
    }
    applyPreferences(prefs);
  }
  function selections() {
    const result={confirmation_mode:$('workspace-confirm')?.value||state.preferences.confirmation_mode||'ask'};
    const model=$('workspace-model')?.value, project=$('workspace-project')?.value;
    if(model)result.model_alias=model;if(project)result.project_id=project;
    const extra=window.VeriloHarness?.extras?.()||{};
    if(extra.mentions?.length)result.mentions=extra.mentions;
    if(extra.linked_session_id)result.linked_session_id=extra.linked_session_id;
    return result;
  }
  function terminalVisible(open) {
    $('workspace-terminal').hidden=!open;
    $('toggle-terminal')?.setAttribute('aria-expanded',String(open));
    if(open)setTimeout(()=>{state.fit?.fit();state.terminal?.focus();},0);
  }
  async function output() {
    clearTimeout(state.poll);if(!state.terminalId)return;
    const id=state.terminalId;
    try {
      const result=await request('GET','/v1/terminals/'+encodeURIComponent(id)+'/output?after='+state.cursor);
      if(state.terminalId!==id)return;
      if(result.truncated)state.terminal.write('\r\n[较早输出已清理]\r\n');
      for(const event of result.events)state.terminal.write(event.text);
      state.cursor=result.cursor;
      if(result.state!=='running'){$('terminal-state').textContent='已结束';return;}
      state.poll=setTimeout(output,200);
    }catch(e){$('terminal-state').textContent=e.message;state.poll=setTimeout(output,2000);}
  }
  async function openTerminal() {
    const targetWorktree=window.VeriloWorkbench?.worktreeId||null;
    if(state.terminalId&&state.terminalWorktreeId===targetWorktree){terminalVisible(true);return;}
    if(state.terminalId&&state.terminalWorktreeId!==targetWorktree){
      try{await request('DELETE','/v1/terminals/'+encodeURIComponent(state.terminalId));}catch(e){notice(e.message);}
      state.terminalId=null;state.cursor=0;clearTimeout(state.poll);
    }
    state.terminalWorktreeId=targetWorktree;
    try {
      let projectId=$('workspace-project').value;
      if(!projectId){
        const p=state.projects[0]||await request('POST','/v1/workspace/projects',{name:'我的项目'});
        projectId=p.id;await savePreference('selected_project_id',projectId);await reload();
      }
      const body={project_id:projectId,cols:100,rows:20};
      if(window.VeriloWorkbench?.worktreeId)body.worktree_id=window.VeriloWorkbench.worktreeId;
      const item=await request('POST','/v1/terminals',body);
      state.terminalId=item.id;state.terminalWorktreeId=targetWorktree;state.cursor=0;
      $('terminal-project').textContent=state.projects.find(p=>p.id===projectId)?.name||'项目';
      $('terminal-state').textContent=item.shell;
      if(!state.terminal){
        state.terminal=new Terminal({cursorBlink:true,fontSize:13,fontFamily:'Consolas, "Cascadia Code", monospace',scrollback:3000,theme:{background:'#142219',foreground:'#e9f3ec',cursor:'#ecfff1'},allowProposedApi:false});
        state.fit=new FitAddon.FitAddon();state.terminal.loadAddon(state.fit);state.terminal.open($('terminal-screen'));
        state.terminal.onData(data=>{if(state.terminalId)state.writing=state.writing.then(()=>request('POST','/v1/terminals/'+encodeURIComponent(state.terminalId)+'/input',{data})).catch(e=>notice(e.message));});
        state.terminal.onResize(size=>{if(state.terminalId&&size.cols>=20&&size.rows>=5&&size.cols<=300&&size.rows<=120)request('POST','/v1/terminals/'+encodeURIComponent(state.terminalId)+'/resize',size).catch(e=>notice(e.message));});
        new ResizeObserver(()=>{if(!$('workspace-terminal').hidden)state.fit.fit();}).observe($('terminal-screen'));
      }else state.terminal.clear();
      terminalVisible(true);output();
    }catch(e){notice(e.message);}
  }
  function mount() {
    const composer=$('composer');if(!composer)return;
    const bar=node('div',undefined,'workspace-toolbar');bar.id='workspace-toolbar';
    const project=node('select');project.id='workspace-project';project.setAttribute('aria-label','选择项目');
    const manage=node('a','项目');manage.href='/console/projects';manage.setAttribute('aria-label','管理项目');
    const confirm=node('select');confirm.id='workspace-confirm';confirm.setAttribute('aria-label','执行方式');
    for(const [value,label] of [['ask','变更前确认'],['plan','先出计划'],['read_only','仅对话']]){const o=node('option',label);o.value=value;confirm.append(o);}
    const branch=node('select');branch.id='workspace-branch';branch.setAttribute('aria-label','工作树分支');
    const model=node('select');model.id='workspace-model';model.setAttribute('aria-label','模型选择');
    const modelSettings=node('a','模型');modelSettings.href='/console/settings#models';
    if(location.pathname.includes('/sessions')){model.hidden=true;modelSettings.textContent='生成模型';modelSettings.setAttribute('aria-label','编程流水线使用服务端冻结的角色模型');}
    bar.append(project,manage,confirm,branch,model,modelSettings);composer.append(bar);
    const msg=node('div',undefined,'workspace-notice');msg.id='workspace-notice';msg.setAttribute('role','status');msg.hidden=true;bar.before(msg);
    const button=node('button','>_','ghost terminal-toggle');button.id='toggle-terminal';button.type='button';button.setAttribute('aria-label','打开终端');button.setAttribute('aria-expanded','false');
    const actions=document.querySelector('.as-links,.head-actions');actions?.prepend(button);
    const panel=node('section',undefined,'workspace-terminal');panel.id='workspace-terminal';panel.hidden=true;panel.setAttribute('aria-label','终端');
    const handle=node('div',undefined,'terminal-resize');handle.setAttribute('role','separator');handle.setAttribute('aria-label','调整终端高度');
    const head=node('div',undefined,'terminal-head');head.append(node('strong','终端'));
    const shell=node('span');shell.id='terminal-state';head.append(shell);
    const name=node('span');name.id='terminal-project';head.append(name);
    const stop=node('button','结束终端','ghost');stop.type='button';
    const close=node('button','×','ghost');close.type='button';close.setAttribute('aria-label','收起终端');head.append(stop,close);
    const screen=node('div');screen.id='terminal-screen';panel.append(handle,head,screen);(composer.closest('.as-main,.main')||composer.parentElement).append(panel);
    button.addEventListener('click',()=>{if(!panel.hidden)terminalVisible(false);else openTerminal();});close.addEventListener('click',()=>terminalVisible(false));
    stop.addEventListener('click',async()=>{try{if(state.terminalId)await request('DELETE','/v1/terminals/'+encodeURIComponent(state.terminalId));state.terminalId=null;state.terminalWorktreeId=null;clearTimeout(state.poll);terminalVisible(false);}catch(e){notice(e.message);}});
    project.addEventListener('change',()=>{savePreference('selected_project_id',project.value);loadBranches();});
    branch.addEventListener('change',()=>{
      const bench=window.VeriloWorkbench||(window.VeriloWorkbench={});
      bench.worktreeId=branch.value||null;bench.projectId=project.value||null;
      window.dispatchEvent(new CustomEvent('verilo:worktree',{detail:{id:bench.worktreeId}}));
    });
    async function loadBranches(){
      branch.replaceChildren();const empty=node('option','主目录');empty.value='';branch.append(empty);
      if(!project.value)return;
      try{
        const trees=await request('GET','/v1/workspace/worktrees?project_id='+encodeURIComponent(project.value));
        for(const item of trees.items||[]){const o=node('option',item.branch);o.value=item.id;branch.append(o);}
        if(window.VeriloWorkbench?.worktreeId)branch.value=window.VeriloWorkbench.worktreeId;
      }catch(e){notice(e.message);}
    }
    window.addEventListener('verilo:worktree',()=>{if(!$('workspace-terminal').hidden)openTerminal();else if(branch.value!==(window.VeriloWorkbench?.worktreeId||''))branch.value=window.VeriloWorkbench?.worktreeId||'';});
    model.addEventListener('change',()=>savePreference('preferred_model',model.value));
    confirm.addEventListener('change',()=>savePreference('confirmation_mode',confirm.value));
    handle.addEventListener('pointerdown',event=>{handle.setPointerCapture(event.pointerId);const y=event.clientY,h=panel.offsetHeight;const move=e=>panel.style.height=Math.max(160,Math.min(innerHeight*0.7,h+y-e.clientY))+'px';handle.addEventListener('pointermove',move);handle.addEventListener('pointerup',()=>handle.removeEventListener('pointermove',move),{once:true});});
    document.addEventListener('keydown',e=>{if(e.ctrlKey&&e.key==='`'){e.preventDefault();button.click();}if(e.ctrlKey&&e.key.toLowerCase()==='n'){e.preventDefault();location.assign('/console/sessions');}});
    const modeLabel=node('span');modeLabel.id='workspace-editor-mode';bar.append(modeLabel);
    document.addEventListener('keydown',e=>{
      const input=e.target;if(input.id!=='prompt'||e.isComposing)return;
      const map=state.preferences.shortcuts, value=input.value, start=input.selectionStart;
      let position=null, consume=false;
      if(map==='vim'){
        if(e.key==='Escape'){input.dataset.vimNormal='true';modeLabel.textContent='普通模式';consume=true;}
        else if(input.dataset.vimNormal==='true'){
          consume=true;
          if(e.key==='i'||e.key==='a'){input.dataset.vimNormal='false';modeLabel.textContent='';if(e.key==='a')position=Math.min(value.length,start+1);}
          else if(e.key==='h')position=Math.max(0,start-1);
          else if(e.key==='l')position=Math.min(value.length,start+1);
          else if(e.key==='0')position=value.lastIndexOf('\n',start-1)+1;
          else if(e.key==='$'){const end=value.indexOf('\n',start);position=end<0?value.length:end;}
          else if(e.key==='x'){input.setRangeText('',start,Math.min(value.length,start+1),'start');input.dispatchEvent(new Event('input',{bubbles:true}));}
          else if(e.key==='j'||e.key==='k'){
            const lineStart=value.lastIndexOf('\n',start-1)+1,col=start-lineStart;
            if(e.key==='j'){const next=value.indexOf('\n',start);if(next>=0){const end=value.indexOf('\n',next+1);position=Math.min(next+1+col,end<0?value.length:end);}}
            else if(lineStart>0){const prev=value.lastIndexOf('\n',lineStart-2)+1;position=Math.min(prev+col,lineStart-1);}
          }
        }
      }else if(map==='emacs'&&e.ctrlKey&&!e.altKey){
        const lineStart=value.lastIndexOf('\n',start-1)+1,end=value.indexOf('\n',start);
        if(e.key==='a'){position=lineStart;consume=true;}
        if(e.key==='e'){position=end<0?value.length:end;consume=true;}
        if(e.key==='k'){input.setRangeText('',start,end<0?value.length:end,'start');input.dispatchEvent(new Event('input',{bubbles:true}));consume=true;}
      }
      if(position!==null)input.setSelectionRange(position,position);
      if(consume){e.preventDefault();e.stopPropagation();}
    },true);
    reload().then(loadBranches).catch(e=>notice(e.message));
    request('GET','/v1/product/entries/hooks').then(d=>{state.hooks=d.items||[];}).catch(()=>{});
    window.addEventListener('verilo:event',event=>{
      const {type,data}=event.detail;const completed=type==='task'&&data.status==='completed'?'task.completed':type==='message.updated'&&data.role==='assistant'&&data.status==='done'?'reply.completed':null;
      if(!completed||state.notified.has(data.id)||!('Notification'in window)||Notification.permission!=='granted')return;
      if(state.hooks.some(h=>h.enabled&&h.config?.event===completed)){state.notified.add(data.id);new Notification('Verilo',{body:completed==='task.completed'?'任务已完成':'有一条新回复'});}
    });
  }
  window.VeriloWorkspace={request,reload,selections,applyPreferences};
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',mount);else mount();
})();
