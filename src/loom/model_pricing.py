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

def role_tariff(spec,route=None):
    tariff=PRO if spec['model_alias']=='deepseek-v4-pro' else FLASH
    if tariff is PRO and spec['role']!='reviewer':raise InvalidInput('Pro is currently restricted to the reviewed read-only reviewer contract')
    if route is not None and (route.provider,route.model)!=('deepseek',tariff.model):
        raise InvalidInput('actual model differs from the approved generation tariff')
    return tariff
