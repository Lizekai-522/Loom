"""Durable local sliding-window admission, independent of process lifetime."""
from dataclasses import asdict, dataclass
import json
import re
import sqlite3

from .auth import LocalIdentity
from .store import Conflict, InvalidInput, canonical, digest


SCHEMA='''
CREATE TABLE IF NOT EXISTS quota_policies (
 tenant TEXT NOT NULL,revision INTEGER NOT NULL,command_key TEXT NOT NULL,
 record TEXT NOT NULL,hash TEXT NOT NULL,PRIMARY KEY(tenant,revision),UNIQUE(tenant,command_key));
CREATE TABLE IF NOT EXISTS quota_admissions (
 tenant TEXT NOT NULL,call_id TEXT NOT NULL,record TEXT NOT NULL,event_id TEXT NOT NULL,
 event_hash TEXT NOT NULL,PRIMARY KEY(tenant,call_id),FOREIGN KEY(event_id) REFERENCES events(id));
CREATE TRIGGER IF NOT EXISTS quota_policies_no_update BEFORE UPDATE ON quota_policies
 BEGIN SELECT RAISE(ABORT,'quota policies are append-only'); END;
CREATE TRIGGER IF NOT EXISTS quota_policies_no_delete BEFORE DELETE ON quota_policies
 BEGIN SELECT RAISE(ABORT,'quota policies are append-only'); END;
CREATE TRIGGER IF NOT EXISTS quota_admissions_no_update BEFORE UPDATE ON quota_admissions
 BEGIN SELECT RAISE(ABORT,'quota admissions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS quota_admissions_no_delete BEFORE DELETE ON quota_admissions
 BEGIN SELECT RAISE(ABORT,'quota admissions are append-only'); END;
'''


class QuotaExceeded(Conflict):
    def __init__(self,code):
        self.code=code
        super().__init__(code)


@dataclass(frozen=True)
class Limit:
    max_calls:int
    max_in_flight:int
    def __post_init__(self):
        if (type(self.max_calls) is not int or not 0<=self.max_calls<=10000
            or type(self.max_in_flight) is not int or not 0<=self.max_in_flight<=256):
            raise InvalidInput('invalid quota limits')


@dataclass(frozen=True)
class QuotaPolicy:
    window_seconds:int
    tenant:Limit
    model:Limit
    tool:Limit
    schema_version:str='loom.quotas/v1'
    def __post_init__(self):
        if (self.schema_version!='loom.quotas/v1' or type(self.window_seconds) is not int
            or not 1<=self.window_seconds<=3600 or not all(type(value) is Limit for value in (self.tenant,self.model,self.tool))):
            raise InvalidInput('invalid quota policy')
    @classmethod
    def parse(cls,value):
        try:
            if type(value) is not dict or set(value)!={'schema_version','window_seconds','tenant','model','tool'}:raise ValueError
            return cls(value['window_seconds'],*(Limit(**value[key]) for key in ('tenant','model','tool')),value['schema_version'])
        except (TypeError,ValueError,KeyError):raise InvalidInput('invalid quota policy') from None


class QuotaService:
    def __init__(self,store):self.store=store

    def _enabled(self,db):
        marker=db.execute("SELECT value FROM settings WHERE key='quotas_enabled'").fetchone()
        if marker is None:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name IN ('quota_policies','quota_admissions')").fetchone():
                raise QuotaExceeded('QUOTA_POLICY_INVALID')
            return False
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if marker[0]!=1 or not {'quota_policies','quota_admissions'}<=tables:
            raise QuotaExceeded('QUOTA_POLICY_INVALID')
        return True

    def _policy(self,db,tenant):
        if not self._enabled(db):return None
        previous='0'*64;current=None
        try:
            for revision,row in enumerate(db.execute('SELECT * FROM quota_policies WHERE tenant=? ORDER BY revision',(tenant,)),1):
                record=json.loads(row['record']);policy=QuotaPolicy.parse(record['policy'])
                if (row['revision']!=revision or row['hash']!=digest(record) or record['previous_hash']!=previous
                    or record['tenant_id']!=tenant or record['revision']!=revision or record['command_key']!=row['command_key']
                    or canonical(record['policy'])!=canonical(asdict(policy))):raise ValueError
                previous=row['hash'];current=(policy,record,row['hash'])
        except (ValueError,KeyError,TypeError,InvalidInput):raise QuotaExceeded('QUOTA_POLICY_INVALID') from None
        return current

    def configure(self,identity,policy,*,key,expected_revision,reason):
        if (not isinstance(identity,LocalIdentity) or 'operator' not in identity.roles or type(policy) is not QuotaPolicy
            or type(expected_revision) is not int or expected_revision<0 or not isinstance(key,str) or not 1<=len(key)<=200
            or not isinstance(reason,str) or not reason.strip() or len(reason)>2000):raise InvalidInput('named operator and exact quota policy required')
        with self.store.transaction() as db:
            statement=''
            for line in SCHEMA.splitlines(True):
                statement+=line
                if sqlite3.complete_statement(statement):
                    db.execute(statement);statement=''
            db.execute("INSERT OR IGNORE INTO settings VALUES('quotas_enabled',1)")
            current=self._policy(db,identity.tenant_id)
            request=dict(actor='subject:'+identity.subject_id,policy=asdict(policy),expected_revision=expected_revision,reason=reason)
            old=db.execute('SELECT record,hash FROM quota_policies WHERE tenant=? AND command_key=?',(identity.tenant_id,key)).fetchone()
            if old:
                record=json.loads(old['record'])
                if any(record[k]!=v for k,v in request.items()):raise Conflict('quota command key reused')
                return dict(revision=record['revision'],policy_hash=old['hash'],changed=False)
            revision=current[1]['revision'] if current else 0
            if revision!=expected_revision:raise Conflict('quota policy revision changed')
            record=dict(**request,tenant_id=identity.tenant_id,revision=revision+1,command_key=key,
                previous_hash=current[2] if current else '0'*64)
            identity_hash=digest(record)
            db.execute('INSERT INTO quota_policies VALUES(?,?,?,?,?)',(identity.tenant_id,revision+1,key,canonical(record),identity_hash))
            return dict(revision=revision+1,policy_hash=identity_hash,changed=True)

    def _records(self,db,tenant):
        records=[]
        for row in db.execute('SELECT * FROM quota_admissions WHERE tenant=?',(tenant,)):
            try:
                record=json.loads(row['record']);event=db.execute('SELECT envelope,hash FROM events WHERE id=?',(row['event_id'],)).fetchone()
                envelope=json.loads(event['envelope'])
                if (row['call_id']!=record['call_id'] or record['tenant_id']!=tenant or event['hash']!=row['event_hash']
                    or digest(envelope)!=event['hash'] or envelope['type']!='quota.admitted' or envelope['actor']!='local-executor'
                    or envelope['tenant_id']!=tenant or envelope['run_id']!=record['run_id'] or envelope['payload']!=record):raise ValueError
                records.append(record)
            except (ValueError,TypeError,KeyError):raise QuotaExceeded('QUOTA_LEDGER_INVALID') from None
        return records

    def _usage(self,db,tenant,policy,now,*,exclude=None):
        records=self._records(db,tenant)
        if any(r['admitted_at']>now for r in records):raise QuotaExceeded('QUOTA_CLOCK_UNCERTAIN')
        by_resource={};total=dict(calls=0,in_flight=0)
        known={r['call_id'] for r in records}
        for record in records:
            if record['call_id']==exclude:continue
            key=(record['kind'],record['resource'])
            item=by_resource.setdefault(key,dict(calls=0,in_flight=0))
            recent=record['admitted_at']>=now-policy.window_seconds
            effect=db.execute('SELECT state FROM effects WHERE tenant=? AND id=?',(tenant,record['call_id'])).fetchone()
            active=effect is None or effect[0] not in {'done','reconciled','not_sent'}
            item['calls']+=int(recent);total['calls']+=int(recent)
            item['in_flight']+=int(active);total['in_flight']+=int(active)
        legacy=sum(row['id'] not in known and row['id']!=exclude for row in db.execute(
            "SELECT id FROM effects WHERE tenant=? AND state NOT IN ('done','reconciled','not_sent')",(tenant,)))
        total['in_flight']+=legacy
        return total,by_resource,legacy

    def admit_model_in(self,db,claim,call_id,request):
        model=request.get('model');provider=request.get('provider','deepseek')
        if any(not isinstance(v,str) or re.fullmatch(r'[A-Za-z0-9_.-]{1,64}',v) is None for v in (provider,model)):
            if self._enabled(db):raise QuotaExceeded('QUOTA_RESOURCE_INVALID')
            return
        return self._admit_in(db,claim,call_id,'model',provider+'/'+model)

    def admit_tool_in(self,db,claim,operation_id,request):
        if not self._enabled(db):return None
        if self._policy(db,claim.tenant_id) is None:raise QuotaExceeded('QUOTA_POLICY_NOT_CONFIGURED')
        if request.get('tool_id')!='tests.run':raise QuotaExceeded('QUOTA_RESOURCE_INVALID')
        grant=self.store._begin_effect(db,claim,operation_id,request)
        if not grant['dispatch']:raise QuotaExceeded('QUOTA_TOOL_REPLAY_REQUIRES_REVIEW')
        self._admit_in(db,claim,grant['call_id'],'tool','tests.run')
        return grant

    def _admit_in(self,db,claim,call_id,kind,resource):
        current=self._policy(db,claim.tenant_id)
        if current is None:
            if self._enabled(db):raise QuotaExceeded('QUOTA_POLICY_NOT_CONFIGURED')
            return
        policy,_,policy_hash=current;now=self.store.now(db)
        total,resources,legacy=self._usage(db,claim.tenant_id,policy,now,exclude=call_id)
        specific=resources.get((kind,resource),dict(calls=0,in_flight=0))
        for scope,limits,usage in [('TENANT',policy.tenant,total),(kind.upper(),getattr(policy,kind),{**specific,'in_flight':specific['in_flight']+legacy})]:
            if usage['calls']>=limits.max_calls:raise QuotaExceeded('QUOTA_'+scope+'_RATE_LIMIT')
            if usage['in_flight']>=limits.max_in_flight:raise QuotaExceeded('QUOTA_'+scope+'_IN_FLIGHT_LIMIT')
        record=dict(tenant_id=claim.tenant_id,run_id=claim.run_id,task_id=claim.task_id,epoch=claim.epoch,
            call_id=call_id,kind=kind,resource=resource,admitted_at=now,policy_hash=policy_hash,counts_as_success=False)
        event=self.store._event(db,claim.tenant_id,claim.run_id,'quota.admitted',record,'local-executor')
        db.execute('INSERT INTO quota_admissions VALUES(?,?,?,?,?)',(claim.tenant_id,call_id,canonical(record),event['event_id'],event['hash']))

    def read(self,tenant):
        with self.store.connect() as db:
            db.execute('BEGIN');current=self._policy(db,tenant)
            base=dict(schema_version='loom.quota-status/v1',tenant_id=tenant,configured=current is not None,
                model_dispatched=False,counts_as_success=False,tool_enforcement_connected=current is not None)
            if current is None:return base
            policy,record,policy_hash=current;total,resources,legacy=self._usage(db,tenant,policy,self.store.now(db))
            return dict(**base,revision=record['revision'],policy_hash=policy_hash,limits=asdict(policy),tenant_usage=total,
                resources=[dict(kind=kind,resource=resource,**usage) for (kind,resource),usage in sorted(resources.items())],
                legacy_unknown_in_flight=legacy,model_enforcement_connected=True)
