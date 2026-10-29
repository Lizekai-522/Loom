import json
import importlib.util
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import pytest

from loom.approved_execution import ReadOnlyStore
from loom.recovery_check import RecoveryCheckError, fingerprint, inspect_copy, make_copy
from loom.store import Store
from tests.test_dispatch import dispatch_case, enqueue
from tests.test_approved_node import node_case

ROOT=Path(__file__).resolve().parents[1]


def state(path):
    with ReadOnlyStore(path).connect() as db: return fingerprint(db)


def test_legacy_copy_keeps_unknown_call_and_budget_frozen(tmp_path):
    store=Store(tmp_path/'source.db')
    run=store.create_run('a','goal',acceptance=['tests.pass'],cost_limit=1000,token_limit=1000)['run_id']
    store.add_task('a',run,'add',task_id='work',title='测试')
    claim=store.claim_next('a',run,'worker',lease_seconds=300)
    store.reserve(claim,'hold',max_cost=100,max_tokens=100)
    store.begin_effect(claim,'external-operation',{'private':'NOT_IN_REPORT'})
    before=state(store.path)
    copy=make_copy(store.path,tmp_path/'private')
    result=inspect_copy(copy)
    assert result['passed'] and result['manual_review_required']
    assert result['held_reservations']==result['unresolved_effects']==1
    assert result['requests_observed']==0 and not result['projection_rebuild_performed']
    assert result['store_reopen_verified'] and not result['cold_process_open_verified']
    assert state(store.path)==state(copy)==before
    assert 'NOT_IN_REPORT' not in json.dumps(result)


def test_queued_history_is_verified_without_dispatch_or_private_config(dispatch_case,tmp_path):
    b=dispatch_case();enqueue(b);before=state(b['store'].path)
    copy=make_copy(b['store'].path,tmp_path/'private')
    with patch('loom.local_executor.load_project_secrets',side_effect=AssertionError('secret read')),\
         patch('loom.local_executor.LocalExecutor',side_effect=AssertionError('worker started')):
        result=inspect_copy(copy)
    assert result['queued_requests']==result['requests_observed']==1 and not result['model_dispatched']
    assert state(b['store'].path)==state(copy)==before


def test_recovery_cli_cold_process_preserves_source_and_prints_only_summary(dispatch_case,tmp_path):
    b=dispatch_case();enqueue(b);before=state(b['store'].path);report=tmp_path/'summary.json'
    result=subprocess.run([sys.executable,'-X','utf8',str(ROOT/'scripts/check_recovery.py'),'--db',b['store'].path,
        '--work-root',str(tmp_path/'private'),'--report',str(report)],capture_output=True,timeout=20)
    assert result.returncode==0,result.stderr
    data=json.loads(result.stdout)
    assert data['passed'] and data['cold_process_open_verified'] and data['queued_requests']==1
    assert json.loads(report.read_bytes())==data and state(b['store'].path)==before
    assert b['store'].path not in result.stdout.decode() and '已核对计划' not in result.stdout.decode()


def test_changed_restored_copy_is_rejected_and_preserved(tmp_path):
    store=Store(tmp_path/'source.db');store.create_run('a','goal',acceptance=['tests.pass'])
    copy=make_copy(store.path,tmp_path/'private')
    with Store(copy).transaction() as db: db.execute('UPDATE runs SET spent=1')
    with pytest.raises(RecoveryCheckError,match='RESTORED_COPY_DIFFERS'):inspect_copy(copy)
    assert copy.is_file()


def test_broken_source_audit_is_not_accepted(tmp_path):
    store=Store(tmp_path/'source.db');store.create_run('a','goal',acceptance=['tests.pass'])
    with store.transaction() as db:
        db.execute('DROP TRIGGER events_no_update')
        db.execute("UPDATE events SET hash=? WHERE seq=1",('f'*64,))
    with pytest.raises(RecoveryCheckError,match='AUDIT_CHAIN_INVALID'):make_copy(store.path,tmp_path/'private')


def test_copy_directory_cannot_be_inside_public_output(tmp_path):
    store=Store(tmp_path/'source.db');public=tmp_path/'outputs'
    with pytest.raises(RecoveryCheckError,match='PRIVATE_RECOVERY_DIRECTORY_REQUIRED'):
        make_copy(store.path,public/'copies',excluded_roots=(public,))
    assert not public.exists()


def test_existing_report_is_not_overwritten(tmp_path):
    store=Store(tmp_path/'source.db');report=tmp_path/'existing.json';report.write_text('PRESERVE_THIS_REPORT')
    result=subprocess.run([sys.executable,'-X','utf8',str(ROOT/'scripts/check_recovery.py'),'--db',store.path,
        '--work-root',str(tmp_path/'private'),'--report',str(report)],capture_output=True,timeout=20)
    assert result.returncode==1 and json.loads(result.stdout)['passed'] is False
    assert report.read_text()=='PRESERVE_THIS_REPORT' and not (tmp_path/'private').exists()


def test_online_backup_uses_one_snapshot_during_concurrent_source_write(tmp_path):
    store=Store(tmp_path/'source.db');store.create_run('a','first',acceptance=['tests.pass'])
    original=fingerprint;written=[]
    def write_after_snapshot(db):
        value=original(db)
        if not written:
            written.append(True)
            store.create_run('a','concurrent',acceptance=['tests.pass'])
        return value
    with patch('loom.recovery_check.fingerprint',side_effect=write_after_snapshot):copy=make_copy(store.path,tmp_path/'private')
    assert inspect_copy(copy)['runs']==1
    with store.connect() as db:assert db.execute('SELECT count(*) FROM runs').fetchone()[0]==2


def test_running_legacy_task_without_external_call_still_requires_review(tmp_path):
    store=Store(tmp_path/'source.db')
    run=store.create_run('a','goal',acceptance=['tests.pass'])['run_id']
    store.add_task('a',run,'add',task_id='work',title='仍在途的任务')
    store.claim_next('a',run,'worker',lease_seconds=300)
    result=inspect_copy(make_copy(store.path,tmp_path/'private'))
    assert result['running_tasks']==1 and result['manual_review_required']
    assert result['unresolved_effects']==result['held_reservations']==0


def test_cold_inspection_refuses_original_database_path(tmp_path):
    store=Store(tmp_path/'source.db');before=state(store.path)
    with pytest.raises(RecoveryCheckError,match='GENERATED_RECOVERY_COPY_REQUIRED'):inspect_copy(store.path)
    assert state(store.path)==before


def test_child_timeout_cleans_owned_process_and_keeps_source(tmp_path,monkeypatch,capsys):
    store=Store(tmp_path/'source.db');store.create_run('a','goal',acceptance=['tests.pass']);before=state(store.path)
    spec=importlib.util.spec_from_file_location('recovery_cli_timeout',ROOT/'scripts/check_recovery.py')
    script=importlib.util.module_from_spec(spec);spec.loader.exec_module(script)
    cleaned=[]
    class Process:
        pid=123456
        waits=0
        def wait(self,timeout):
            self.waits+=1
            if self.waits==1:raise subprocess.TimeoutExpired('fixture-child',timeout)
            return 0
        def kill(self):cleaned.append(True)
    monkeypatch.setattr(script.subprocess,'Popen',lambda *args,**kwargs:Process())
    def cleanup(args,**kwargs):
        assert args[:2]==['taskkill','/PID'] and args[2]=='123456'
        cleaned.append(True)
    monkeypatch.setattr(script.subprocess,'run',cleanup)
    monkeypatch.setattr(sys,'argv',['check_recovery.py','--db',store.path,'--work-root',str(tmp_path/'private')])
    assert script.main()==1
    result=json.loads(capsys.readouterr().out)
    assert result['code']=='RECOVERY_CHILD_TIMEOUT' and cleaned and state(store.path)==before
    assert list((tmp_path/'private/loom-recovery-checks').glob('*/inspection-private.log'))
