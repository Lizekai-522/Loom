from concurrent.futures import ThreadPoolExecutor
import socket
from threading import Barrier, Lock

import httpx

from monsoon.web import SearchConfig, Web


def public_dns(*args, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443))]


def test_redirect_and_later_fetch_never_replay_cookies():
    seen = []

    def handle(request):
        seen.append(request.headers.get('cookie'))
        if request.url.path == '/redirect':
            return httpx.Response(302, headers={'location': '/end', 'set-cookie': 'track=one; Path=/'})
        return httpx.Response(200, headers={'content-type': 'text/plain', 'set-cookie': 'track=two; Path=/'}, text='ok')

    web = Web(SearchConfig(), transport=httpx.MockTransport(handle), resolver=public_dns)
    try:
        web.fetch('https://example.com/redirect')
        web.fetch('https://example.com/next')
        assert seen == [None, None, None]
        assert len(web.client.cookies) == 0
    finally:
        web.close()


def test_concurrent_fetches_cannot_exchange_cookies():
    barrier, lock, seen = Barrier(4), Lock(), []

    def handle(request):
        with lock:
            seen.append(request.headers.get('cookie'))
        if request.url.path.startswith('/first'):
            barrier.wait(timeout=5)
        return httpx.Response(200, headers={'content-type': 'text/plain', 'set-cookie': 'track=x; Path=/'}, text='ok')

    web = Web(SearchConfig(), transport=httpx.MockTransport(handle), resolver=public_dns)
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda i: web.fetch(f'https://example.com/first{i}'), range(4)))
            list(pool.map(lambda i: web.fetch(f'https://example.com/second{i}'), range(4)))
        assert seen == [None] * 8
        assert len(web.client.cookies) == 0
    finally:
        web.close()
