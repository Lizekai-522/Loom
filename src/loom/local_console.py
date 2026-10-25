"""Opt-in loopback development access without exposing bearer credentials."""
from ipaddress import ip_address
from urllib.parse import urlsplit

from .auth import AuthConfigError, Principal


def console_principal(identities, enabled):
    if type(enabled) is not bool:
        raise AuthConfigError()
    if not enabled:
        return None
    operators = [identity for identity in identities if 'operator' in identity.roles]
    if len(operators) != 1:
        raise AuthConfigError()
    identity = operators[0]
    return Principal(identity.tenant_id, identity.subject_id, identity.roles)


def allows_local_console(request):
    """Require local peer, literal loopback Host and same-origin browser use.

    The custom header is a CSRF marker, not a secret. Other local processes can
    use this explicitly enabled development mode. Production access remains a
    separate authenticated deployment concern.
    """
    try:
        if request.headers.get('x-loom-console') != 'local' or not request.client:
            return False
        if not ip_address(request.client.host).is_loopback:
            return False
        if len(request.headers.getlist('host')) != 1 or len(request.headers.getlist('origin')) > 1:
            return False
        if any(name in request.headers for name in ('forwarded', 'x-forwarded-host', 'x-forwarded-for')):
            return False
        host = request.headers['host']
        parsed = urlsplit('//'+host)
        if parsed.hostname not in {'localhost', '127.0.0.1', '::1'} or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
            return False
        port = parsed.port or (443 if request.url.scheme == 'https' else 80)
        origin = request.headers.get('origin')
        if origin:
            source = urlsplit(origin)
            if (source.scheme != request.url.scheme or source.hostname != parsed.hostname
                    or (source.port or (443 if source.scheme == 'https' else 80)) != port
                    or source.username or source.password or source.path or source.query or source.fragment):
                return False
        if request.headers.get('sec-fetch-site') not in {None, 'same-origin', 'none'}:
            return False
        return True
    except (ValueError, TypeError, KeyError):
        return False
