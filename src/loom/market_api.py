"""Curated read-only HTTP plugin catalog, registration and explicit invocation."""
from copy import deepcopy
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from monsoon.safety import redact_value
from .auth import is_human
from .store import NotFound, Conflict

CATALOG = [dict(id='github-public',name='GitHub 仓库',description='读取公开仓库信息',manifest=dict(plugin_id='github-public',version='1.0.0',base_url='https://api.github.com',roles=[],enabled=True,endpoints=[dict(name='repository',path='/repos/{owner}/{repo}',params={'type':'object','properties':{'owner':{'type':'string','minLength':1,'maxLength':100},'repo':{'type':'string','minLength':1,'maxLength':100}},'required':['owner','repo'],'additionalProperties':False})])),dict(id='npm-public',name='npm 软件包',description='读取公开软件包最新版本',manifest=dict(plugin_id='npm-public',version='1.0.0',base_url='https://registry.npmjs.org',roles=[],enabled=True,endpoints=[dict(name='package',path='/{package}/latest',params={'type':'object','properties':{'package':{'type':'string','minLength':1,'maxLength':150}},'required':['package'],'additionalProperties':False})]))]

class Invoke(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    arguments: dict = Field(default_factory=dict)

class Enabled(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    enabled: bool


def install_market_api(app,store,*,Reader,Writer,Key):
    plugins=app.state.platform['plugins']
    def human(identity):
        if not is_human(identity):raise HTTPException(403,'a named human user is required')
    def template(identifier):
        item=next((x for x in CATALOG if x['id']==identifier),None)
        if item is None:raise NotFound('catalog entry not found')
        return item
    @app.get('/v1/market/catalog')
    def catalog(identity:Reader):
        installed={p['plugin_id']:p for p in plugins.list(identity.tenant_id)}
        return {'items':[dict(id=p['id'],name=p['name'],description=p['description'],installed=p['id'] in installed,enabled=bool(installed.get(p['id'],{}).get('enabled')),parameters=p['manifest']['endpoints'][0]['params']['required']) for p in CATALOG]}
    @app.post('/v1/market/{identifier}/install',status_code=201)
    def install(identifier:str,identity:Writer,idempotency_key:Key):
        human(identity)
        return plugins.register(identity.tenant_id,idempotency_key,deepcopy(template(identifier)['manifest']),actor='subject:'+identity.subject_id)
    @app.patch('/v1/market/{identifier}')
    def enable(identifier:str,body:Enabled,identity:Writer,idempotency_key:Key):
        human(identity)
        if identifier not in {p['plugin_id'] for p in plugins.list(identity.tenant_id)}:raise NotFound('plugin not installed')
        manifest=deepcopy(template(identifier)['manifest']);manifest['enabled']=body.enabled
        return plugins.register(identity.tenant_id,idempotency_key,manifest,actor='subject:'+identity.subject_id)
    @app.post('/v1/market/{identifier}/invoke')
    def invoke(identifier:str,body:Invoke,identity:Writer,idempotency_key:Key):
        human(identity);item=template(identifier)
        installed=next((p for p in plugins.list(identity.tenant_id) if p['plugin_id']==identifier),None)
        if installed is None:raise NotFound('plugin not installed')
        if installed['base_url']!=item['manifest']['base_url'] or installed['endpoints'][0]['path']!=item['manifest']['endpoints'][0]['path']:
            raise Conflict('plugin catalog definition changed; reinstall first')
        return redact_value(plugins.invoke(identity.tenant_id,idempotency_key,identifier,item['manifest']['endpoints'][0]['name'],body.arguments,actor='subject:'+identity.subject_id,dry_run=False))
