"""Local process observations, separate from execution admission and audit state."""
import asyncio
from contextlib import asynccontextmanager, suppress
import math
import os
from pathlib import Path
import tempfile
import time

from .auth import LocalIdentity
from .executor_config import strict_json
from .store import canonical, digest


WINDOW_SECONDS = 15
INTERVAL_SECONDS = 2

_PROBE_STATES = frozenset({'ok', 'error', 'unknown'})


def _bounded_text(value, limit=120):
    return isinstance(value, str) and 0 < len(value) <= limit


def _validate_extra(key, value):
    """Validate one optional heartbeat extension; anything malformed is dropped."""
    if key in ('config_mtime', 'started_at'):
        if type(value) in (int, float) and math.isfinite(value) and 0 <= value < 8_000_000_000:
            return value
        raise ValueError
    if value is None:
        return None
    if type(value) is not dict or not value or len(value) > 8:
        raise ValueError
    if value.get('state') not in _PROBE_STATES or not type(value.get('checked_at')) in (int, float):
        raise ValueError
    clean = dict(state=value['state'], checked_at=value['checked_at'])
    for field in ('code', 'detail', 'alias', 'image_id', 'docker_version'):
        if value.get(field) is not None:
            if not _bounded_text(value[field]):
                raise ValueError
            clean[field] = value[field]
    real = value.get('real_call')
    if real is not None:
        if type(real) is not dict or real.get('state') not in _PROBE_STATES:
            raise ValueError
        clean_real = dict(state=real['state'])
        for field in ('code', 'detail'):
            if real.get(field) is not None:
                if not _bounded_text(real[field]):
                    raise ValueError
                clean_real[field] = real[field]
        for field in ('cost_microusd', 'tokens'):
            if real.get(field) is not None and type(real[field]) is int and 0 <= real[field] <= 10**9:
                clean_real[field] = real[field]
        clean['real_call'] = clean_real
    return clean


class ExecutorStatus:
    #: Optional process observations published beside the heartbeat. Values are
    #: bounded dicts written only by the executor process itself; the API reads
    #: them verbatim after the same strict validation, never trusting contents.
    EXTRA_KEYS = frozenset({'model_probe', 'sandbox_probe', 'config_mtime', 'started_at'})

    def __init__(self, store, config=None, identities=()):
        self.config = config
        self.identities = tuple(identities)
        self.database = Path(store.path).resolve()
        self.directory = self.database.with_name(self.database.name + '.executor-heartbeats')
        self._extra = {}

    def set_runtime_info(self, **fields):
        """Record bounded probes from the executor process (model/sandbox/config)."""
        for key, value in fields.items():
            if key not in self.EXTRA_KEYS:
                raise ValueError('unknown runtime info key')
            self._extra[key] = value

    def _scope(self, tenant):
        workers = [i for i in self.identities if isinstance(i, LocalIdentity)
                   and i.tenant_id == tenant and 'worker' in i.roles]
        if self.config is None or len(workers) != 1:
            return None
        return digest(dict(database=str(self.database), tenant_id=tenant,
                           worker_id=workers[0].subject_id, executor_id=self.config.executor_id))

    def publish(self):
        """Write only bounded process metadata; no model configuration is read."""
        if self.config is None:
            return
        observed_at = time.time()
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        for tenant in {i.tenant_id for i in self.identities if isinstance(i, LocalIdentity) and 'worker' in i.roles}:
            scope = self._scope(tenant)
            if scope is None:
                continue
            data = dict(schema_version='loom.executor-heartbeat/v1', scope=scope, observed_at=observed_at)
            for key in sorted(self.EXTRA_KEYS):
                value = self._extra.get(key)
                if value is not None:
                    data[key] = value
            data = canonical(data)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.directory,
                                                 prefix='.pulse-', delete=False) as handle:
                    temporary = Path(handle.name)
                    handle.write(data)
                os.replace(temporary, self.directory / (scope + '.json'))
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)

    def read(self, tenant):
        result = dict(schema_version='loom.executor-status/v1', tenant_id=tenant, state='not_observed',
                      observed_age_seconds=None, recent_window_seconds=WINDOW_SECONDS,
                      provider_health_checked=False, sandbox_health_checked=False,
                      model_dispatched=False, counts_as_success=False)
        if self.config is None:
            return {**result, 'state': 'not_configured'}
        scope = self._scope(tenant)
        if scope is None:
            return {**result, 'state': 'unknown'}
        try:
            with (self.directory / (scope + '.json')).open('rb') as handle:
                raw = handle.read(4097)
            if len(raw) > 4096:
                raise ValueError
            data = strict_json(raw)
            required = {'schema_version', 'scope', 'observed_at'}
            if (type(data) is not dict or not required <= set(data) <= required | self.EXTRA_KEYS
                    or data['schema_version'] != 'loom.executor-heartbeat/v1' or data['scope'] != scope
                    or type(data['observed_at']) not in (int, float) or not math.isfinite(data['observed_at'])):
                raise ValueError
            age = time.time() - data['observed_at']
            if not math.isfinite(age) or age < 0:
                raise ValueError
            result = {**result, 'state': 'recent' if age <= WINDOW_SECONDS else 'stale',
                      'observed_age_seconds': min(int(age), 2_147_483_647)}
            for key in sorted(self.EXTRA_KEYS):
                if key in data:
                    result[key] = _validate_extra(key, data[key])
            return result
        except FileNotFoundError:
            return result
        except (OSError, ValueError, TypeError, RecursionError, OverflowError):
            return {**result, 'state': 'unknown'}

    async def _publish_safely(self):
        # An observation failure never authorizes, cancels or retries a task.
        try:
            await asyncio.to_thread(self.publish)
        except (OSError, ValueError):
            pass

    @asynccontextmanager
    async def monitor(self, interval=INTERVAL_SECONDS):
        await self._publish_safely()
        async def pulse():
            while True:
                await asyncio.sleep(interval)
                await self._publish_safely()
        task = asyncio.create_task(pulse())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            # A different process may have updated the same observation. Never
            # delete it or write "offline" on exit; freshness expires naturally.
