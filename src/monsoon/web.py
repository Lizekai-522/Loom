"""Read-only web access for research: pluggable search plus a guarded page fetch.

Everything returned here is untrusted data. Fetches are https-only, refuse IP
literals and names that resolve to private/loopback/link-local/reserved
addresses (rechecked on every redirect), never send cookies or credentials,
and are bounded in time, size and redirects.
"""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import nullcontext
from html.parser import HTMLParser
from http.cookiejar import CookieJar, DefaultCookiePolicy
import ipaddress
import re
import socket
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from loom.store import InvalidInput

MAX_PAGE_BYTES = 1_500_000
MAX_TEXT = 6000
REDIRECTS = 3
TIMEOUT = 10.0
PROVIDERS = ('none', 'bocha', 'tavily', 'searxng')


class _RejectCookies(DefaultCookiePolicy):
    """Reject both storage and sending, including across concurrent requests."""

    def set_ok(self, cookie, request):
        return False

    def return_ok(self, cookie, request):
        return False


class WebError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class SearchConfig:
    provider: str = 'none'
    key_env: str | None = None
    base_url: str | None = None
    max_results: int = 5
    # Separate private file for the search key: the model secrets file is read by
    # many tools that accept only model key names, so it must not gain extra keys.
    secrets_file: str | None = None

    @classmethod
    def parse(cls, data):
        if data is None:
            return cls()
        if not isinstance(data, dict) or not set(data) <= {'provider', 'key_env', 'base_url', 'max_results', 'secrets_file'}:
            raise InvalidInput('invalid search configuration')
        provider = data.get('provider', 'none')
        if provider not in PROVIDERS:
            raise InvalidInput('search provider must be one of ' + ', '.join(PROVIDERS))
        key_env = data.get('key_env')
        if provider in ('bocha', 'tavily') and (type(key_env) is not str or not re.fullmatch(r'[A-Z_][A-Z0-9_]{0,63}', key_env)):
            raise InvalidInput('search provider needs key_env naming a private secret')
        secrets_file = data.get('secrets_file')
        if provider in ('bocha', 'tavily') and (type(secrets_file) is not str or not secrets_file.strip() or len(secrets_file) > 4096):
            raise InvalidInput('search provider needs secrets_file: a private JSON file holding only the search key')
        base = data.get('base_url')
        if provider == 'searxng':
            parts = urlsplit(base or '')
            if parts.scheme not in ('https', 'http') or not parts.hostname:
                raise InvalidInput('searxng needs base_url')
        count = data.get('max_results', 5)
        if type(count) is not int or not 1 <= count <= 10:
            raise InvalidInput('max_results must be 1 to 10')
        return cls(provider, key_env, base, count, data.get('secrets_file'))


def _public_host(host, resolver=socket.getaddrinfo):
    try:
        ipaddress.ip_address(host.strip('[]'))
        raise WebError('FETCH_IP_LITERAL_REFUSED')
    except ValueError:
        pass
    if '.' not in host or host == 'localhost' or host.endswith(('.local', '.internal', '.localhost', '.lan', '.home.arpa')):
        raise WebError('FETCH_INTERNAL_NAME_REFUSED')
    try:
        infos = resolver(host, 443, type=socket.SOCK_STREAM)
    except OSError:
        raise WebError('FETCH_DNS_FAILED') from None
    if not infos:
        raise WebError('FETCH_DNS_FAILED')
    addresses = []
    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0])
        except (IndexError, TypeError, ValueError):
            raise WebError('FETCH_DNS_FAILED') from None
        if not address.is_global:
            raise WebError('FETCH_PRIVATE_ADDRESS_REFUSED')
        addresses.append(str(address))
    return addresses


def check_url(url, resolver=socket.getaddrinfo):
    parts = urlsplit(url) if isinstance(url, str) else None
    if parts is None or parts.scheme != 'https' or not parts.hostname or parts.username or parts.password or len(url) > 2000:
        raise WebError('FETCH_URL_REFUSED')
    _public_host(parts.hostname.lower(), resolver)
    return url


def _pinned_request(url, resolver):
    """Connect to a validated IP while retaining the original TLS name and Host."""
    parts = urlsplit(url)
    host = parts.hostname.encode('idna').decode('ascii').lower()
    addresses = _public_host(host, resolver)
    address = addresses[0]
    try:
        port = parts.port
    except ValueError:
        raise WebError('FETCH_URL_REFUSED') from None
    ip_host = f'[{address}]' if ':' in address else address
    target = urlunsplit(('https', ip_host + (f':{port}' if port else ''), parts.path, parts.query, ''))
    authority = host + (f':{port}' if port and port != 443 else '')
    return target, authority, host


class _Text(HTMLParser):
    SKIP = {'script', 'style', 'noscript', 'svg', 'template', 'iframe', 'head'}
    BLOCK = {'p', 'div', 'br', 'li', 'tr', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'section', 'article', 'pre'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip, self.title, self._in_title = [], 0, '', False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        if tag == 'title':
            self._in_title = True
        if tag in self.BLOCK:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        if tag == 'title':
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self.skip:
            self.parts.append(data)


def html_to_text(raw):
    parser = _Text()
    try:
        parser.feed(raw)
        parser.close()
    except Exception:
        pass
    body = re.sub(r'[ \t\r\f\v]+', ' ', ''.join(parser.parts))
    body = re.sub(r'\n\s*\n+', '\n', body).strip()
    return parser.title.strip()[:200], body


class Web:
    def __init__(self, config: SearchConfig, *, secrets=None, transport=None, resolver=socket.getaddrinfo):
        self.config, self.secrets, self.resolver = config, secrets or {}, resolver
        self._mock_transport = transport is not None
        self.client = httpx.Client(transport=transport, follow_redirects=False, timeout=TIMEOUT,
                                   trust_env=False,
                                   headers={'User-Agent': 'LoomResearch/0.31 (+read-only)'})
        # Pass the CookieJar itself: assigning an httpx.Cookies wrapper makes
        # httpx copy it into a new jar and loses the restrictive policy.
        self.client.cookies = CookieJar(policy=_RejectCookies())

    def close(self):
        self.client.close()

    @property
    def provider(self):
        return self.config.provider

    def search(self, query):
        query = query.strip()[:300]
        if not query:
            return []
        cfg = self.config
        try:
            if cfg.provider == 'bocha':
                response = self.client.post('https://api.bochaai.com/v1/web-search',
                                            json={'query': query, 'count': cfg.max_results, 'summary': True},
                                            headers={'Authorization': 'Bearer ' + self._key()})
                data = self._json(response)
                pages = (((data.get('data') or {}).get('webPages') or {}).get('value')) or []
                items = [dict(title=p.get('name'), url=p.get('url'), snippet=p.get('summary') or p.get('snippet')) for p in pages]
            elif cfg.provider == 'tavily':
                response = self.client.post('https://api.tavily.com/search',
                                            json={'query': query, 'max_results': cfg.max_results},
                                            headers={'Authorization': 'Bearer ' + self._key()})
                items = [dict(title=r.get('title'), url=r.get('url'), snippet=r.get('content'))
                         for r in self._json(response).get('results') or []]
            elif cfg.provider == 'searxng':
                response = self.client.get(cfg.base_url.rstrip('/') + '/search', params={'q': query, 'format': 'json'})
                items = [dict(title=r.get('title'), url=r.get('url'), snippet=r.get('content'))
                         for r in self._json(response).get('results') or []]
            else:
                raise WebError('SEARCH_NOT_CONFIGURED')
        except httpx.HTTPError:
            raise WebError('SEARCH_TRANSPORT_ERROR') from None
        results = []
        for item in items:
            url, title = item.get('url'), item.get('title')
            if isinstance(url, str) and url.startswith('https://') and isinstance(title, str):
                snippet = item.get('snippet') if isinstance(item.get('snippet'), str) else ''
                results.append(dict(title=title[:200], url=url[:2000], snippet=snippet[:500]))
        return results[:cfg.max_results]

    def _key(self):
        value = self.secrets.get(self.config.key_env) if self.config.key_env else None
        if not isinstance(value, str) or not value:
            raise WebError('SEARCH_KEY_MISSING')
        return value

    @staticmethod
    def _json(response):
        if response.status_code != 200:
            raise WebError('SEARCH_HTTP_ERROR')
        if len(response.content) > 2_000_000:
            raise WebError('SEARCH_RESPONSE_TOO_LARGE')
        try:
            data = response.json()
        except ValueError:
            raise WebError('SEARCH_RESPONSE_INVALID') from None
        if not isinstance(data, dict):
            raise WebError('SEARCH_RESPONSE_INVALID')
        return data

    def fetch(self, url):
        """Readable text of one public https page (untrusted)."""
        current = url
        for _ in range(REDIRECTS + 1):
            check_url(current, self.resolver)
            if self._mock_transport:
                target, headers, extensions = current, {}, {}
                context = nullcontext(self.client)
            else:
                target, authority, hostname = _pinned_request(current, self.resolver)
                headers, extensions = {'Host': authority}, {'sni_hostname': hostname}
                # One fresh pool per target avoids sharing a TLS session between
                # different names that happen to resolve to the same address.
                context = httpx.Client(follow_redirects=False, timeout=TIMEOUT, trust_env=False)
            headers['Accept'] = 'text/html,text/plain,application/json;q=0.8'
            try:
                with context as client, client.stream('GET', target, headers=headers, extensions=extensions) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get('location')
                        if not location:
                            raise WebError('FETCH_REDIRECT_INVALID')
                        current = urljoin(current, location)
                        continue
                    if response.status_code != 200:
                        raise WebError('FETCH_HTTP_ERROR')
                    kind = response.headers.get('content-type', '').split(';', 1)[0].strip().lower()
                    if kind not in ('text/html', 'text/plain', 'application/json', 'application/xhtml+xml'):
                        raise WebError('FETCH_UNSUPPORTED_CONTENT')
                    data = bytearray()
                    for chunk in response.iter_bytes():
                        data.extend(chunk)
                        if len(data) > MAX_PAGE_BYTES:
                            raise WebError('FETCH_TOO_LARGE')
                    encoding = response.encoding or 'utf-8'
            except httpx.HTTPError:
                raise WebError('FETCH_TRANSPORT_ERROR') from None
            try:
                decoded = bytes(data).decode(encoding, errors='replace')
            except LookupError:
                decoded = bytes(data).decode('utf-8', errors='replace')
            title, body = html_to_text(decoded) if 'html' in kind else ('', decoded)
            return dict(url=current, title=title, text=body[:MAX_TEXT], truncated=len(body) > MAX_TEXT)
        raise WebError('FETCH_TOO_MANY_REDIRECTS')


URL_PATTERN = re.compile(r'https://[^\s<>"\'）)，。]+')


def urls_in(text):
    return list(dict.fromkeys(URL_PATTERN.findall(text or '')))[:5]
