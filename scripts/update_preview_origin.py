"""保存隧道地址及本机体验说明，不输出访问码。"""
import argparse
import json
from pathlib import Path
import re

from bootstrap_local import write_private
from loom.public_access import PreviewAccess


def update(work,origin):
    if not re.fullmatch(r'https://[a-z0-9-]+\.trycloudflare\.com',origin):raise ValueError('quick tunnel HTTPS origin required')
    private=Path(work).resolve()/'loom-public-preview';path=private/'access.json'
    data=json.loads(path.read_bytes());data['origin']=origin
    # Validation uses an inert token; actual backend token never enters this file.
    PreviewAccess(origin,data['access_code'],'validation-placeholder-000000000000000')
    write_private(path,data)
    info=private/'访问信息.txt'
    info.write_text('Loom 临时公网体验\n\n地址：'+origin+'/console\n体验访问码：'+data['access_code']+'\n\n访问码仅分享给受邀测试者，不公开模型 API 密钥或内部身份。\n本机必须保持开机、联网，Docker 和服务/执行器/隧道进程持续运行。\n重启隧道地址会变化；重启服务需重新登录。停止脚本可关闭公网体验。\n单目标最多 $1 / 3,200,000 词元，空间最多 20 个目标，不自动重试模型。\n独立双模型仍待配置，候选提交不代表最终完成。\n',encoding='utf-8')
    try:info.chmod(0o600)
    except OSError:pass


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--work-root',required=True,type=Path);parser.add_argument('--origin',required=True);args=parser.parse_args()
    update(args.work_root,args.origin)
    print('Public origin saved; access code remains in the private local file.')
