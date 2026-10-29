"""Exact serialized request byte limits; never a token estimate or truncation."""
from .model_gateway import GatewayError


def check_context_budget(spec, request_bytes):
    limit=spec['limits'].get('context_limit_bytes')
    if limit is None:
        return
    if type(limit) is not int or not 256<=limit<=262144 or type(request_bytes) is not bytes:
        raise GatewayError('INVALID_CONTEXT_BUDGET')
    if len(request_bytes)>limit:
        raise GatewayError('TASK_CONTEXT_LIMIT_EXCEEDED')
