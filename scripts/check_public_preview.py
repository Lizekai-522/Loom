"""只读检查临时受邀体验入口；不打印凭据，不发起模型调用。"""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

import httpx


def check(work_root: Path) -> list[tuple[str, bool]]:
    private = work_root.resolve() / 'loom-public-preview'
    repo = Path(__file__).resolve().parents[1]
    try:
        access = json.loads((private / 'access.json').read_bytes())
        record = json.loads((private / 'processes.json').read_bytes())
        origin, port = access['origin'], access['port']
        if not isinstance(origin, str) or not re.fullmatch(r'https://[a-z0-9-]+\.trycloudflare\.com', origin):
            raise ValueError('invalid origin')
        if type(port) is not int or not 1024 <= port <= 65535:
            raise ValueError('invalid port')
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return [('私有体验配置可读取', False)]

    checks = [('私有体验配置可读取', True)]
    expected = {'api': repo / 'scripts/serve_public_preview.py',
                'worker': repo / 'scripts/run_local_executor.py',
                'tunnel': work_root.resolve() / 'loom-tunnel/cloudflared.exe'}
    for kind, label in [('api', '体验服务进程'), ('worker', '执行器进程'), ('tunnel', '隧道进程')]:
        try:
            items = [item for item in record['processes'] if item.get('kind') == kind]
            item, = items
            pid = item['id']
            if type(pid) is not int or pid <= 0 or Path(item['marker']).resolve() != expected[kind].resolve():
                raise ValueError('invalid process record')
            # The command contains only a validated integer. Captured process details remain private.
            command = f"$p=Get-CimInstance Win32_Process -Filter 'ProcessId={pid}'; if($p){{$p.CommandLine}}"
            proc = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command],
                                  capture_output=True, timeout=8, check=True)
            alive = str(expected[kind]).casefold() in proc.stdout.decode('utf-8', errors='replace').casefold()
        except (OSError, ValueError, TypeError, KeyError, AttributeError, subprocess.SubprocessError):
            alive = False
        checks.append((label, alive))

    try:
        result = subprocess.run(['docker', 'info', '--format', '{{.ServerVersion}}'],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=12)
        checks.append(('Docker 服务', result.returncode == 0))
    except (OSError, subprocess.SubprocessError):
        checks.append(('Docker 服务', False))

    with httpx.Client(trust_env=False, timeout=10, follow_redirects=False) as client:
        for label, url, headers in [
            ('本机体验入口', f'http://127.0.0.1:{port}/preview/login', {'Host': origin.removeprefix('https://')}),
            ('临时 HTTPS 入口', origin + '/preview/login', {}),
        ]:
            try:
                ok = client.get(url, headers=headers).status_code == 200
            except httpx.HTTPError:
                ok = False
            checks.append((label, ok))
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-root', type=Path, default=Path(__file__).resolve().parents[3] / 'work')
    checks = check(parser.parse_args().work_root)
    for label, ok in checks:
        print(f'{label}：{"通过" if ok else "未通过"}')
    print('进程存在及入口可访问不代表模型、容器执行或独立验收已经通过。')
    print('临时地址可能变化；请以本机访问信息文件为准，并在正式体验前用独立网络复测。')
    return 0 if all(ok for _, ok in checks) else 1


if __name__ == '__main__':
    sys.exit(main())
