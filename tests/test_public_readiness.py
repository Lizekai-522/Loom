import json
from pathlib import Path
from types import SimpleNamespace

import httpx

from scripts import check_public_preview as readiness


def fixture(tmp_path, origin='https://example.trycloudflare.com'):
    private = tmp_path / 'loom-public-preview'
    private.mkdir()
    (private / 'access.json').write_text(json.dumps({'origin': origin, 'port': 8799, 'access_code': 'PRIVATE_TEST_VALUE'}))
    repo = Path(readiness.__file__).resolve().parents[1]
    markers = {'api': repo / 'scripts/serve_public_preview.py', 'worker': repo / 'scripts/run_local_executor.py',
               'tunnel': tmp_path / 'loom-tunnel/cloudflared.exe'}
    (private / 'processes.json').write_text(json.dumps({'processes': [
        {'kind': name, 'id': pid, 'marker': str(markers[name])}
        for pid, name in enumerate(markers, 1)
    ]}))
    return markers


def test_readiness_reports_stopped_worker_and_https_failure_separately(tmp_path, monkeypatch):
    markers = fixture(tmp_path)
    def run(args, **kwargs):
        if args[0] == 'docker': return SimpleNamespace(returncode=0)
        command = args[-1]
        marker = markers['api'] if 'ProcessId=1' in command else markers['tunnel'] if 'ProcessId=3' in command else ''
        return SimpleNamespace(stdout=str(marker).encode())
    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, url, headers):
            if url.startswith('https://'): raise httpx.ConnectError('offline')
            return SimpleNamespace(status_code=200)
    monkeypatch.setattr(readiness.subprocess, 'run', run)
    monkeypatch.setattr(readiness.httpx, 'Client', Client)
    result = dict(readiness.check(tmp_path))
    assert result['体验服务进程'] and not result['执行器进程'] and result['隧道进程']
    assert result['Docker 服务'] and result['本机体验入口'] and not result['临时 HTTPS 入口']
    assert 'PRIVATE_TEST_VALUE' not in str(result)


def test_readiness_rejects_untrusted_origin_without_requests(tmp_path, monkeypatch):
    fixture(tmp_path, origin='https://attacker.example')
    def never(*args, **kwargs): raise AssertionError('unexpected request')
    monkeypatch.setattr(readiness.subprocess, 'run', never)
    monkeypatch.setattr(readiness.httpx, 'Client', never)
    assert readiness.check(tmp_path) == [('私有体验配置可读取', False)]
