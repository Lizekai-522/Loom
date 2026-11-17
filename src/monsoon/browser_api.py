"""Local browser preview and element pick.

Pages are read with the research client's public-https guards. The inspector
returns a bounded HTML outline and the element the person clicked. It does not
run page scripts, store cookies, or grant the model a browser.
"""
import threading
import time
from uuid import uuid4

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from loom.auth import is_human
from loom.local_console import allows_local_console
from monsoon.safety import redact
from monsoon.web import SearchConfig, Web, WebError, extract_elements

from urllib.parse import urlsplit


class Body(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)


class PreviewIn(Body):
    url: str = Field(min_length=8, max_length=2000)


class PickIn(Body):
    preview_id: str = Field(min_length=8, max_length=80)
    selector: str = Field(min_length=1, max_length=300)


class BrowserInspector:
    def __init__(self, fetch, *, clock=time.time, ttl=600):
        self.fetch, self.clock, self.ttl = fetch, clock, ttl
        self.lock = threading.Lock()
        self.previews = {}

    def _owner(self, identity):
        return identity.tenant_id, identity.subject_id

    def _prune(self, now):
        stale = [key for key, item in self.previews.items() if item['expires'] <= now]
        for key in stale:
            self.previews.pop(key, None)

    def preview(self, identity, url):
        try:
            document = self.fetch(url)
        except WebError as error:
            raise HTTPException(409, error.code) from None
        html = document.get('html') or ''
        elements = extract_elements(html) if html else []
        now = self.clock()
        preview_id = 'br-' + uuid4().hex[:20]
        record = dict(
            id=preview_id, owner=self._owner(identity), url=redact(document.get('url') or url)[:2000],
            title=redact(document.get('title') or '')[:200], text=redact(document.get('text') or '')[:6000],
            elements=elements, renderer=document.get('renderer') or 'html-extract', expires=now + self.ttl)
        with self.lock:
            self._prune(now)
            if len(self.previews) > 40:
                raise HTTPException(409, 'BROWSER_PREVIEW_LIMIT')
            self.previews[preview_id] = record
        return {k: v for k, v in record.items() if k not in ('owner', 'expires')}

    def pick(self, identity, preview_id, selector):
        now = self.clock()
        with self.lock:
            self._prune(now)
            record = self.previews.get(preview_id)
            if record is None or record['owner'] != self._owner(identity) or record['expires'] <= now:
                raise HTTPException(404, 'BROWSER_PREVIEW_NOT_FOUND')
            element = next((item for item in record['elements'] if item['selector'] == selector), None)
        if element is None:
            raise HTTPException(404, 'BROWSER_ELEMENT_NOT_FOUND')
        attachment = redact(element.get('text') or element['tag'])[:500]
        return dict(preview_id=preview_id, url=record['url'], selector=element['selector'], tag=element['tag'],
                    text=attachment, attachment='页面元素 ' + element['selector'] + '：' + attachment)


def install_browser_api(app, *, Reader, Writer, local_console=False, fetch=None):
    """Mount only for the loopback console. Public previews keep the route absent."""
    if not local_console:
        return None

    def default_fetch(url):
        web = Web(SearchConfig())
        try:
            return web.inspect(url)
        finally:
            web.close()

    inspector = BrowserInspector(fetch or default_fetch)
    app.state.browser_inspector = inspector

    def guard(request, identity):
        if (not is_human(identity) or not allows_local_console(request)
                or urlsplit(str(request.base_url)).hostname not in {'127.0.0.1', 'localhost', '::1'}):
            raise HTTPException(403, 'BROWSER_CONTROL_LOCAL_HUMAN_ONLY')

    @app.post('/v1/assistant/browser/preview')
    def preview(body: PreviewIn, request: Request, identity: Writer):
        guard(request, identity)
        return inspector.preview(identity, body.url)

    @app.post('/v1/assistant/browser/pick')
    def pick(body: PickIn, request: Request, identity: Writer):
        guard(request, identity)
        return inspector.pick(identity, body.preview_id, body.selector)

    return inspector
