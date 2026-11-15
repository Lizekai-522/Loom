"""Read-only runtime observation and safe public evidence for the private verifier."""
from hashlib import sha256
import json
from types import SimpleNamespace

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .executor_status import ExecutorStatus
from .store import Conflict,InvalidInput,NotFound,digest
from .verification_jobs import VerificationJobs
from .verification_signatures import TrustedVerifier,capabilities,public_fingerprint,verify_pair

class VerificationRuntime:
    def __init__(self,store,config):
        self.store=store;self.config=config
        self.binding=self._fingerprint()
        self.jobs=VerificationJobs(store,pool_limit=config.pool_limit_microusd)
        self.identities=config.identities()
        self.status=ExecutorStatus(store,SimpleNamespace(executor_id='verification-'+self.binding[:24]),self.identities)

    def _fingerprint(self):
        return digest(dict(config=sha256(self.config.path.read_bytes()).hexdigest(),models=sha256(self.config.model_config.read_bytes()).hexdigest()))

    def check(self,tenant):
        if tenant!=self.config.tenant_id:raise NotFound('verification runtime not found')
        if self._fingerprint()!=self.binding:raise Conflict('VERIFICATION_CONFIGURATION_CHANGED')

    def models_ready(self):
        """Both verifier routes are configured, verified and have a key (no network, no values kept)."""
        from .local_config import load_project_secrets
        from .model_gateway import ModelGateway, load_model_config
        from .model_pricing import MODEL_PROVIDERS, VERIFIER_MODELS
        try:
            routes, roles = load_model_config(self.config.model_config)
            secrets = load_project_secrets(self.config.model_secrets_file, allowed_names={route.key_env for route in routes})
            gateway = ModelGateway(routes, role_aliases=roles, environ=dict(secrets))
        except Exception:
            return False
        try:
            return all(model in gateway.routes and gateway.routes[model].provider == MODEL_PROVIDERS[model]
                       and gateway.readiness(model).ready for model in VERIFIER_MODELS)
        finally:
            del secrets

    def registry(self):
        trusted={};public=[]
        for model,path in self.config.public_keys.items():
            raw=path.read_bytes()
            if len(raw)>8192:raise InvalidInput('invalid verifier public key')
            key=serialization.load_pem_public_key(raw)
            if not isinstance(key,Ed25519PublicKey):raise InvalidInput('invalid verifier public key')
            trusted[model]=TrustedVerifier(model,'local-verifier:'+model,model,key)
            public.append(dict(model_id=model,key_id=model,algorithm='Ed25519',fingerprint=public_fingerprint(key),public_key_pem=raw.decode('ascii')))
        return trusted,public

    def capabilities(self,tenant):
        self.check(tenant);self.registry()
        with self.store.connect() as db:db.execute('BEGIN');pool=self.jobs._pool(db,tenant)
        return {**capabilities(tenant),'configured':True,'completion_connected':True,
            'executor':self.status.read(tenant),'project_pool':pool,
            'max_request_cost_microusd':self.jobs.MAX_RESERVATION,'generation_budget_unchanged':True,
            'private_key_health_checked':False,'production_identity_isolation':False}

    def history(self,tenant,run_id,task_id):
        self.check(tenant);trusted,public=self.registry()
        with self.store.connect() as db:
            db.execute('BEGIN');self.store._run(db,tenant,run_id)
            task=db.execute('SELECT * FROM tasks WHERE tenant=? AND run_id=? AND id=?',(tenant,run_id,task_id)).fetchone()
            if task is None:raise NotFound('task not found')
            states=[self.jobs._state(db,row) for row in db.execute('SELECT * FROM verification_requests WHERE tenant=? AND run_id=? AND task_id=? ORDER BY rowid',(tenant,run_id,task_id))]
            task_status=task['status'];epoch=task['epoch'];output=task['output']
        result=[]
        for state in states:
            proofs=self.jobs.attestations(tenant,state['job_id'])
            authenticated=False
            if proofs:
                # Historical evidence is checked at its signed issuance window;
                # it cannot authorize a new task epoch or a new completion.
                signed=verify_pair(proofs,state['binding'],trusted,now=max(p['body']['issued_at'] for p in proofs))
                authenticated=all(s.authenticated for s in signed)
            current=epoch==state['binding']['epoch'] and output is not None and digest(json.loads(output))==state['binding']['content_hash']
            result.append({**state,'attestations':proofs,'signatures_authenticated':authenticated,
                'task_verified':state['state']=='verified' and authenticated and current and task_status=='done','current_source':current})
        return dict(schema_version='loom.verification-history/v1',run_id=run_id,task_id=task_id,epoch=epoch,
            task_status=task_status,requests=result,public_keys=public,model_dispatched=False,counts_as_success=False)
