"""Official peak cache-miss upper bounds checked 2026-10-29; not invoices."""
from dataclasses import dataclass
from .store import InvalidInput

@dataclass(frozen=True)
class ModelTariff:
    model:str
    input_per_million_microusd:int
    output_per_million_microusd:int
    context_limit:int=1048576
    provider:str='deepseek'
    def cost(self,prompt_tokens,output_tokens):
        if any(type(v) is not int or v<0 for v in (prompt_tokens,output_tokens)):
            raise InvalidInput('invalid usage accounting')
        return (self.input_per_million_microusd*prompt_tokens+self.output_per_million_microusd*output_tokens+999999)//1000000

FLASH=ModelTariff('deepseek-flash',300000,1200000)
PRO=ModelTariff('deepseek-v4-pro',1320000,3960000)
# Alibaba Cloud Model Studio, Beijing list price checked 2026-11-05: CNY 12 input /
# CNY 36 output per million tokens, one tier up to 1M, thinking and non-thinking
# alike. Converted at a deliberately low 6.5 CNY per USD so the micro-USD bound
# overstates, never understates, what the provider bills.
QWEN=ModelTariff('qwen3.8-max-0902',1846154,5538462,context_limit=1000000,provider='dashscope')
TARIFFS={tariff.model:tariff for tariff in (FLASH,PRO,QWEN)}
MODEL_PROVIDERS={tariff.model:tariff.provider for tariff in TARIFFS.values()}
# Read-only reviewer models: never generators, only the reviewer role.
REVIEW_ONLY_MODELS=frozenset({PRO.model,QWEN.model})
# Independent verification pairs a DeepSeek generator family member with a model
# from another vendor. Attestations made with the earlier same-vendor pair stay
# readable as history; new jobs always use the current pair.
VERIFIER_MODELS=(FLASH.model,QWEN.model)
LEGACY_VERIFIER_MODELS=(FLASH.model,PRO.model)
ACCEPTED_VERIFIER_PAIRS=frozenset({frozenset(VERIFIER_MODELS),frozenset(LEGACY_VERIFIER_MODELS)})

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
    tariff=TARIFFS.get(spec['model_alias'],FLASH)
    if tariff.model in REVIEW_ONLY_MODELS and spec['role']!='reviewer':
        raise InvalidInput('reviewer-only models are restricted to the reviewed read-only reviewer contract')
    if route is not None and (route.provider,route.model)!=(tariff.provider,tariff.model):
        raise InvalidInput('actual model differs from the approved generation tariff')
    return tariff
