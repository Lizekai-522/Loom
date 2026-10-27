"""创建独立的公网体验空间。访问码只写入源码之外的本机私有文件。"""
import argparse
import json
from pathlib import Path
import secrets

from bootstrap_local import write_private
from loom.auth import load_identities
from loom.execution import freeze_tests
from loom.executor_config import load_executor_config
from loom.public_access import PreviewAccess, PreviewStore
from loom.sandbox import FrozenTest
from loom.workflow import WorkflowService


DESCRIPTION = '实现英文词频程序：从标准输入读取 JSON 对象 {"text":字符串,"limit":整数}。按正则 [A-Za-z]+ 提取单词，全部转小写。输出 {"total":单词总数,"top":[{"word":单词,"count":次数}]}，按次数降序、同次数单词字母升序，top 最多 limit 项。limit 缺省为 3，允许 0 到 20；布尔或其他非法 limit 返回 {"error":"invalid_limit"}。使用 Python 标准库，通过标准输出返回单个 JSON，不输出解释或调试文本。'


def create_example(store,repo,evaluation,operator,key):
    workflow=WorkflowService(store)
    goal=workflow.create_goal('public-preview',key,title='首轮体验：英文词频',description=DESCRIPTION,acceptance=['schema.valid','tests.pass','report.traceable'],cost_limit=1000000,token_limit=3200000,actor='subject:'+operator.subject_id)
    plan=workflow.get_plan('public-preview',goal['run_id'])
    if plan['revision']==0:
        definition=json.loads((repo/'packs/software-dev/workflow-v1.json').read_bytes());limits=dict(cost_limit_microusd=320000,token_limit=1060000,max_attempts=1)
        definition['nodes']=[dict(id='requirements',title='分析词频需求',instruction=DESCRIPTION,role='analyst',role_output=dict(kind='requirements',schema_version='loom.requirements/v1'),limits=limits),
            dict(id='implement',title='实现并隔离测试词频程序',instruction=DESCRIPTION,role='developer',evaluation_digest=evaluation,input_artifacts=[dict(source_node='requirements',kind='requirements',schema_version='loom.requirements/v1',required=True)],limits=limits),
            dict(id='verify',title='等待独立模型验收',instruction='独立模型待配置；不将候选自动记为成功',role='validator',channel='verification',depends=['requirements','implement'],limits=limits)]
        workflow.save_plan('public-preview',goal['run_id'],key+':plan',definition,0,actor='subject:'+operator.subject_id)
    return goal


def prepare(work_root, port=8799):
    repo=Path(__file__).resolve().parents[1]; work=Path(work_root).resolve()
    if work.is_relative_to(repo.parent) or work==Path(work.anchor):raise ValueError('private root required')
    if type(port) is not int or not 1024<=port<=65535:raise ValueError('invalid port')
    private=work/'loom-public-preview';private.mkdir(parents=True,exist_ok=True)
    identity_path=private/'identities.json'
    if not identity_path.exists():
        write_private(identity_path,dict(schema_version='loom.local-identities/v1',identities=[dict(tenant_id='public-preview',subject_id='preview-'+role,roles=[role],token=secrets.token_urlsafe(32)) for role in ('operator','worker')]))
    identities=load_identities(identity_path)
    tests=[FrozenTest('counts',{'text':'Cat dog CAT bird dog','limit':3},{'total':5,'top':[{'word':'cat','count':2},{'word':'dog','count':2},{'word':'bird','count':1}]}),
           FrozenTest('empty',{'text':''},{'total':0,'top':[]}),
           FrozenTest('ascii',{'text':'你好 A1b__A!','limit':1},{'total':3,'top':[{'word':'a','count':2}]}),
           FrozenTest('zero',{'text':'a b a','limit':0},{'total':3,'top':[]}),
           FrozenTest('boolean',{'text':'a','limit':True},{'error':'invalid_limit'}),
           FrozenTest('default',{'text':'d c b a'},{'total':4,'top':[{'word':'a','count':1},{'word':'b','count':1},{'word':'c','count':1}]})]
    evaluation=freeze_tests(tests)[1];host=private/'word-count-host.json'
    expected={'tests':[dict(name=t.name,input=t.input,expected=t.expected) for t in tests]}
    if host.exists() and json.loads(host.read_bytes())!=expected:raise ValueError('frozen preview tests changed')
    if not host.exists():write_private(host,expected)
    executor=private/'executor.json'
    if not executor.exists():
        write_private(executor,dict(schema_version='loom.local-executor/v1',executor_id='public-preview',identities_file=str(identity_path),secrets_file=str(work/'loom-private/model-secrets.json'),model_config=str(repo/'configs/models.example.json'),sandbox_image=(work/'loom-sandbox-image.id').read_text().strip(),host_tests={evaluation:str(host)}))
    load_executor_config(executor)
    config_path=private/'access.json'
    if not config_path.exists():write_private(config_path,dict(origin='https://unconfigured.invalid',access_code=secrets.token_urlsafe(32),port=port))
    config=json.loads(config_path.read_bytes());operator=next(i for i in identities if 'operator' in i.roles)
    PreviewAccess(config['origin'],config['access_code'],operator.token)
    store=PreviewStore(private/'preview.db')
    # Repeatable startup restores the same draft, never creates new paid work.
    goal=create_example(store,repo,evaluation,operator,'initial-preview-example-v2')
    return dict(passed=True,port=config['port'],run_id=goal['run_id'],private_directory=str(private),model_calls=0)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--work-root',required=True,type=Path);parser.add_argument('--port',type=int,default=8799);args=parser.parse_args()
    print(json.dumps(prepare(args.work_root,args.port),ensure_ascii=False))
