"""Official peak cache-miss upper bounds checked 2026-10-29; not invoices."""
from dataclasses import dataclass
from .store import InvalidInput

@dataclass(frozen=True)
class ModelTariff:
    model:str
    input_per_million_microusd:int
    output_per_million_microusd:int
    context_limit:int=1048576
    def cost(self,prompt_tokens,output_tokens):
        if any(type(v) is not int or v<0 for v in (prompt_tokens,output_tokens)):
            raise InvalidInput('invalid usage accounting')
        return (self.input_per_million_microusd*prompt_tokens+self.output_per_million_microusd*output_tokens+999999)//1000000

FLASH=ModelTariff('deepseek-flash',300000,1200000)
PRO=ModelTariff('deepseek-v4-pro',1320000,3960000)

# Byte-level BPE: every prompt token encodes at least one byte of the message
# text, and the serialized request (checked against context_limit_bytes before
# any reservation) is never shorter than that text. The chat template adds a
# small fixed number of special tokens; this margin covers them generously. If a
# provider still reports more prompt tokens, settlement records an overrun and
# suspends the run (existing PROVIDER_USAGE_OVER_RESERVATION path).
REQUEST_TOKEN_OVERHEAD=2048


def prompt_token_bound(spec,tariff):
    """Worst-case prompt tokens for a frozen task contract.

    Contracts without context_limit_bytes keep the full model context bound, so
    previously approved plans reserve exactly as before.
    """
    limit=spec['limits'].get('context_limit_bytes')
    if limit is None:return tariff.context_limit
    if type(limit) is not int or not 256<=limit<=262144:raise InvalidInput('invalid context byte limit')
    return min(tariff.context_limit,limit+REQUEST_TOKEN_OVERHEAD)


def role_tariff(spec,route=None):
    tariff=PRO if spec['model_alias']=='deepseek-v4-pro' else FLASH
    if tariff is PRO and spec['role']!='reviewer':raise InvalidInput('Pro is currently restricted to the reviewed read-only reviewer contract')
    if route is not None and (route.provider,route.model)!=('deepseek',tariff.model):
        raise InvalidInput('actual model differs from the approved generation tariff')
    return tariff
