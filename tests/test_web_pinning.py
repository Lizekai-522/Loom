import pytest

from monsoon.web import SearchConfig, Web, WebError, _pinned_request


def resolver_for(addresses):
    return lambda *args, **kwargs: [(None, None, None, None, (a, 443)) for a in addresses]


def test_pins_address_and_preserves_tls_and_host():
    target, host, sni = _pinned_request('https://example.com:8443/path?a=1', resolver_for(['8.8.8.8']))
    assert target == 'https://8.8.8.8:8443/path?a=1'
    assert host == 'example.com:8443'
    assert sni == 'example.com'


@pytest.mark.parametrize('addresses', [[], ['127.0.0.1'], ['8.8.8.8', '10.0.0.1'], ['::1']])
def test_pin_rejects_empty_and_mixed_private_addresses(addresses):
    with pytest.raises(WebError):
        _pinned_request('https://example.com/', resolver_for(addresses))


def test_rebinding_to_private_address_between_resolutions_fails_closed():
    responses = iter([['8.8.8.8'], ['127.0.0.1']])
    def resolver(*args, **kwargs):
        return resolver_for(next(responses))()
    web = Web(SearchConfig(), resolver=resolver)
    try:
        with pytest.raises(WebError) as error:
            web.fetch('https://example.com/')
        assert error.value.code == 'FETCH_PRIVATE_ADDRESS_REFUSED'
    finally:
        web.close()
