"""Authenticate local verifier attestations; provider statements are never signatures.

The caller owns the trusted key registry and must obtain receipts from the
gateway ledger. This module authenticates exact claims, not test truth or
production separation of people/services. No network, database or completion.
"""
import base64
from dataclasses import dataclass
from hashlib import sha256
from typing import Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey,Ed25519PublicKey
from pydantic import BaseModel,ConfigDict,Field,ValidationError

from .policy import VerificationSignature
from .store import InvalidInput,canonical

HASH=r'^[0-9a-f]{64}$'
TEXT=dict(min_length=1,max_length=200,pattern=r'^[A-Za-z0-9_.:@/-]+$')
MODELS=frozenset({'deepseek-flash','deepseek-v4-pro'})

class VerificationError(InvalidInput):
    def __init__(self):super().__init__('VERIFICATION_SIGNATURE_INVALID')

class Strict(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,allow_inf_nan=False,revalidate_instances='always')

class VerificationBinding(Strict):
    tenant_id:str=Field(**TEXT)
    run_id:str=Field(**TEXT)
    task_id:str=Field(**TEXT)
    epoch:int=Field(ge=1)
    plan_digest:str=Field(pattern=HASH)
    content_hash:str=Field(pattern=HASH)
    ruleset_version:str=Field(**TEXT)
    evaluation_digest:str=Field(pattern=HASH)
    evaluation_event_hash:str=Field(pattern=HASH)
    submission_event_hash:str=Field(pattern=HASH)
    input_digest:str=Field(pattern=HASH)

class GatewayReceipt(Strict):
    provider:Literal['deepseek']
    model_id:Literal['deepseek-flash','deepseek-v4-pro']
    call_id:str=Field(pattern=HASH)
    request_hash:str=Field(pattern=HASH)
    response_hash:str=Field(pattern=HASH)
    candidate_hash:str=Field(pattern=HASH)

class VerdictBody(Strict):
    schema_version:Literal['loom.verifier-attestation/v1']='loom.verifier-attestation/v1'
    key_id:str=Field(**TEXT)
    verifier_id:str=Field(**TEXT)
    binding:VerificationBinding
    receipt:GatewayReceipt
    passed:bool
    issued_at:int=Field(ge=0)
    expires_at:int=Field(ge=1)

class SignedVerdict(Strict):
    body:VerdictBody
    signature:str=Field(min_length=88,max_length=88)

@dataclass(frozen=True)
class TrustedVerifier:
    key_id:str
    verifier_id:str
    model_id:str
    public_key:Ed25519PublicKey
    revoked:bool=False
    signing_not_before:int=0
    signing_not_after:int=2**63-1

def public_fingerprint(key):
    if not isinstance(key,Ed25519PublicKey):raise VerificationError()
    return sha256(key.public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)).hexdigest()

def sign_verdict(body,private_key):
    """Trusted signer primitive. Never exposed as a caller-controlled HTTP action."""
    try:
        checked=VerdictBody.model_validate(body.model_dump() if isinstance(body,VerdictBody) else body)
        if not isinstance(private_key,Ed25519PrivateKey):raise ValueError
        if not 1<=checked.expires_at-checked.issued_at<=3600:raise ValueError
        signature=private_key.sign(canonical(checked.model_dump()).encode('utf-8'))
        return SignedVerdict(body=checked,signature=base64.b64encode(signature).decode('ascii')).model_dump()
    except (ValidationError,ValueError,TypeError):raise VerificationError() from None

def verify_pair(verdicts,binding,trusted_keys,*,now):
    """Only successful cryptographic authentication supplies authenticated=True."""
    try:
        expected=VerificationBinding.model_validate(binding.model_dump() if isinstance(binding,VerificationBinding) else binding)
        if type(now) is not int or now<0 or type(verdicts) not in (list,tuple) or len(verdicts)!=2 or type(trusted_keys) is not dict:raise ValueError
        ids=set();models=set();fingerprints=set();calls=set();results=[]
        for raw in verdicts:
            signed=SignedVerdict.model_validate(raw);body=signed.body
            key=trusted_keys.get(body.key_id)
            if (not isinstance(key,TrustedVerifier) or key.revoked is not False
                or key.key_id!=body.key_id or key.verifier_id!=body.verifier_id or key.model_id!=body.receipt.model_id
                or key.model_id not in MODELS or body.binding!=expected
                or not body.issued_at<=now<body.expires_at or not 1<=body.expires_at-body.issued_at<=3600
                or type(key.signing_not_before) is not int or type(key.signing_not_after) is not int
                or not key.signing_not_before<=body.issued_at<=key.signing_not_after):raise ValueError
            signature=base64.b64decode(signed.signature,validate=True)
            if len(signature)!=64:raise ValueError
            key.public_key.verify(signature,canonical(body.model_dump()).encode('utf-8'))
            fingerprint=public_fingerprint(key.public_key)
            if body.verifier_id in ids or key.model_id in models or fingerprint in fingerprints or body.receipt.call_id in calls:raise ValueError
            ids.add(body.verifier_id);models.add(key.model_id);fingerprints.add(fingerprint);calls.add(body.receipt.call_id)
            results.append(VerificationSignature(body.verifier_id,key.model_id,expected.content_hash,expected.ruleset_version,
                f'{expected.task_id}:{expected.epoch}',body.passed,authenticated=True))
        if models!=MODELS:raise ValueError
        return tuple(results)
    except (ValidationError,ValueError,TypeError,InvalidSignature,AttributeError,OverflowError):raise VerificationError() from None

def capabilities(tenant):
    return dict(schema_version='loom.verification-capabilities/v1',tenant_id=tenant,algorithm='Ed25519',
        supported_models=sorted(MODELS),exact_binding_required=True,key_registration_over_http=False,
        configured=False,model_dispatched=False,counts_as_success=False,completion_connected=False)
