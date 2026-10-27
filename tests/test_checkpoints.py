"""Offline audit-boundary tests: real signatures, rewritten chains and rollback."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from loom.checkpoints import (
    CheckpointError, CheckpointRollback, TrustedKey, advance_trusted_checkpoint,
    export_checkpoint, generate_keypair, load_checkpoint, load_public_key,
    verify_checkpoint,
)
from loom.store import Store, canonical, digest


NOW = datetime(2026, 10, 23, 2, 0, tzinfo=timezone.utc)
PASSWORD = b"test-only-local-signing-password"


@pytest.fixture
def audit(tmp_path):
    store = Store(tmp_path / "database" / "loom.db")
    tenant = "audit-tenant"
    run = store.create_run(tenant, "create", acceptance=["tests.pass"])["run_id"]
    private, public = tmp_path / "signer" / "private.pem", tmp_path / "verifier" / "public.pem"
    generate_keypair(private, public, password=PASSWORD, database_path=store.path)
    key = TrustedKey("key-1", load_public_key(public))
    env = SimpleNamespace(store=store, tenant=tenant, run=run, private=private, public=public,
                          keys={key.key_id: key}, directory=tmp_path / "checkpoints",
                          trusted=tmp_path / "trusted" / "head.json")
    env.checkpoint = export_checkpoint(
        store, tenant, run, private_key_path=private, checkpoint_path=env.directory / "first.json",
        key_id="key-1", password=PASSWORD, now=NOW,
    )
    return env


def verify(audit, checkpoint=None, events=None, **kwargs):
    return verify_checkpoint(
        audit.checkpoint if checkpoint is None else checkpoint,
        audit.store.events(audit.tenant, audit.run) if events is None else events,
        trusted_keys=kwargs.pop("trusted_keys", audit.keys), expected_tenant=audit.tenant,
        expected_run=audit.run, expected_chain=audit.run, now=kwargs.pop("now", NOW), **kwargs,
    )


def advance(audit, checkpoint=None, events=None, **kwargs):
    return advance_trusted_checkpoint(
        audit.checkpoint if checkpoint is None else checkpoint,
        audit.store.events(audit.tenant, audit.run) if events is None else events,
        trusted_checkpoint_path=audit.trusted, database_path=audit.store.path,
        trusted_keys=audit.keys, expected_tenant=audit.tenant, expected_run=audit.run,
        expected_chain=audit.run, now=kwargs.pop("now", NOW), **kwargs,
    )


def grow(audit):
    audit.store.add_task(audit.tenant, audit.run, "add-one", task_id="task-1", title="One task")
    return export_checkpoint(
        audit.store, audit.tenant, audit.run, private_key_path=audit.private,
        checkpoint_path=audit.directory / "second.json", key_id="key-1", password=PASSWORD,
        now=NOW + timedelta(seconds=1), previous_checkpoint=audit.checkpoint, trusted_keys=audit.keys,
    )


def rewrite_events(events):
    """Attacker recomputes every local hash; no access to signer or trusted file."""
    rewritten = deepcopy(events)
    rewritten[0]["payload"]["attacker_replaced_history"] = True
    previous = "0" * 64
    for event in rewritten:
        event["prev_hash"] = previous
        event["hash"] = digest({name: value for name, value in event.items() if name != "hash"})
        previous = event["hash"]
    return rewritten


def write_attacker_history(audit):
    events = rewrite_events(audit.store.events(audit.tenant, audit.run))
    # This is a controlled attack on this test's temporary database only.
    with audit.store.transaction() as db:
        db.execute("DROP TRIGGER events_no_update")
        for event in events:
            db.execute("UPDATE events SET envelope=?,hash=? WHERE id=?",
                       (canonical({name: value for name, value in event.items() if name != "hash"}),
                        event["hash"], event["event_id"]))
    return events


def test_signed_snapshot_uses_external_public_key_and_exact_count(audit):
    verified = verify(audit)
    assert verified.seq == verified.event_count == verified.observed_count == 3
    assert verified.unanchored_tail_count == 0
    assert verified.head_hash == audit.store.events(audit.tenant, audit.run)[-1]["hash"]
    assert load_checkpoint(audit.directory / "first.json") == audit.checkpoint
    assert audit.checkpoint["algorithm"] == "Ed25519"
    assert "PRIVATE KEY" not in canonical(audit.checkpoint)
    assert "ENCRYPTED PRIVATE KEY" in audit.private.read_text()


@pytest.mark.parametrize("field,value", [
    ("head_hash", "f" * 64), ("created_at", "2026-10-23T02:00:01.000000Z"),
    ("expires_at", "2026-10-24T03:00:00.000000Z"),
    ("signature", "A" * 88), ("algorithm", "none"), ("schema", "loom.checkpoint/v2"),
    ("seq", True), ("event_count", 4),
])
def test_modified_signed_fields_fail_closed(audit, field, value):
    changed = dict(audit.checkpoint)
    changed[field] = value
    with pytest.raises(CheckpointError):
        verify(audit, changed)


@pytest.mark.parametrize("field", ["tenant_id", "run_id", "chain_id"])
def test_identity_fields_are_authenticated_even_if_caller_uses_changed_identity(audit, field):
    changed = dict(audit.checkpoint)
    changed[field] = "copied-identity"
    expected = {"expected_tenant": audit.tenant, "expected_run": audit.run, "expected_chain": audit.run}
    expected[{"tenant_id": "expected_tenant", "run_id": "expected_run", "chain_id": "expected_chain"}[field]] = changed[field]
    with pytest.raises(CheckpointError, match="signature verification failed"):
        verify_checkpoint(changed, audit.store.events(audit.tenant, audit.run),
                          trusted_keys=audit.keys, now=NOW, **expected)


def test_key_id_is_signed_not_just_a_lookup_hint(audit):
    changed = dict(audit.checkpoint, key_id="key-alias")
    same_public = TrustedKey("key-alias", audit.keys["key-1"].public_key)
    with pytest.raises(CheckpointError, match="signature verification failed"):
        verify(audit, changed, trusted_keys={"key-alias": same_public})


def test_wrong_public_key_unknown_key_and_revoked_key_are_rejected(audit):
    wrong = TrustedKey("key-1", Ed25519PrivateKey.generate().public_key())
    with pytest.raises(CheckpointError, match="signature verification failed"):
        verify(audit, trusted_keys={"key-1": wrong})
    with pytest.raises(CheckpointError, match="allowlist"):
        verify(audit, trusted_keys={})
    revoked = replace(audit.keys["key-1"], revoked=True)
    with pytest.raises(CheckpointError, match="allowlist"):
        verify(audit, trusted_keys={"key-1": revoked})


def test_key_signing_window_is_enforced(audit):
    later = replace(audit.keys["key-1"], signing_not_before=NOW + timedelta(seconds=1))
    earlier = replace(audit.keys["key-1"], signing_not_after=NOW - timedelta(seconds=1))
    for key in (later, earlier):
        with pytest.raises(CheckpointError, match="signing window"):
            verify(audit, trusted_keys={"key-1": key})


def test_expiration_and_verifier_max_age_are_independent(audit):
    with pytest.raises(CheckpointError, match="expired"):
        verify(audit, now=NOW + timedelta(days=1))
    with pytest.raises(CheckpointError, match="freshness"):
        verify(audit, now=NOW + timedelta(hours=2), max_age=timedelta(hours=1))
    with pytest.raises(CheckpointError, match="future"):
        verify(audit, now=NOW - timedelta(minutes=2))


def test_expired_previous_anchor_still_enforces_known_high_water(audit):
    second = grow(audit)
    # Refresh signatures explicitly; the previous trusted head's expiry must not
    # permit erasing its already-known seq/hash.
    later = export_checkpoint(
        audit.store, audit.tenant, audit.run, private_key_path=audit.private,
        checkpoint_path=audit.directory / "later.json", key_id="key-1", password=PASSWORD,
        now=NOW + timedelta(days=2), previous_checkpoint=second, trusted_keys=audit.keys,
    )
    result = verify(audit, later, previous_checkpoint=second, now=NOW + timedelta(days=2))
    assert result.seq == 4


def test_truncation_is_detected_even_when_remaining_local_chain_is_self_consistent(audit):
    truncated = audit.store.events(audit.tenant, audit.run)[:-1]
    assert Store.verify_events(truncated)
    with pytest.raises(CheckpointError, match="count"):
        verify(audit, events=truncated)


def test_full_database_hash_rewrite_cannot_replace_independent_signed_head(audit):
    rewritten = rewrite_events(audit.store.events(audit.tenant, audit.run))
    assert Store.verify_events(rewritten)
    with pytest.raises(CheckpointError, match="independently signed head"):
        verify(audit, events=rewritten)


def test_payload_tampering_without_rehash_is_rejected(audit):
    events = audit.store.events(audit.tenant, audit.run)
    events[1]["actor"] = "human:forged"
    with pytest.raises(CheckpointError, match="envelope hash"):
        verify(audit, events=events)


def test_checkpoint_cannot_be_copied_to_another_run_or_tenant(audit):
    other = audit.store.create_run("other", "create-other", acceptance=["tests.pass"])["run_id"]
    with pytest.raises(CheckpointError, match="identity"):
        verify_checkpoint(audit.checkpoint, audit.store.events("other", other), trusted_keys=audit.keys,
                          expected_tenant="other", expected_run=other, expected_chain=other, now=NOW)


def test_unanchored_tail_is_rejected_by_default_and_explicitly_reported(audit):
    grow(audit)
    with pytest.raises(CheckpointError, match="count"):
        verify(audit)
    prefix = verify(audit, allow_unanchored_tail=True)
    assert prefix.seq == 3
    assert prefix.observed_count == 4
    assert prefix.unanchored_tail_count == 1


def test_tampering_in_unanchored_tail_is_not_ignored(audit):
    grow(audit)
    events = audit.store.events(audit.tenant, audit.run)
    events[-1]["actor"] = "human:forged"
    with pytest.raises(CheckpointError):
        verify(audit, events=events, allow_unanchored_tail=True)


def test_external_trusted_seq_only_advances_and_rejects_old_signed_snapshot(audit):
    first_events = audit.store.events(audit.tenant, audit.run)
    assert advance(audit, initialize=True).seq == 3
    second = grow(audit)
    assert advance(audit, second, now=NOW + timedelta(seconds=1)).seq == 4
    trusted_bytes = audit.trusted.read_bytes()
    with pytest.raises(CheckpointRollback):
        advance(audit, audit.checkpoint, first_events, now=NOW + timedelta(seconds=2))
    assert audit.trusted.read_bytes() == trusted_bytes


def test_previously_trusted_prefix_cannot_be_rewritten_even_if_signer_signs_it_again(audit):
    write_attacker_history(audit)
    audit.store.add_task(audit.tenant, audit.run, "new-task", task_id="new-task", title="One new task")
    rewritten = export_checkpoint(
        audit.store, audit.tenant, audit.run, private_key_path=audit.private,
        checkpoint_path=audit.directory / "rewritten.json", key_id="key-1", password=PASSWORD,
        now=NOW + timedelta(seconds=1),
    )
    # A newly approved signature alone is insufficient to prove continuity.
    assert verify(audit, rewritten, now=NOW + timedelta(seconds=1)).seq == 4
    with pytest.raises(CheckpointRollback, match="previously trusted"):
        verify(audit, rewritten, previous_checkpoint=audit.checkpoint, now=NOW + timedelta(seconds=1))


def test_signer_refuses_rewritten_history_when_previous_anchor_is_supplied(audit):
    write_attacker_history(audit)
    with pytest.raises(CheckpointRollback):
        export_checkpoint(
            audit.store, audit.tenant, audit.run, private_key_path=audit.private,
            checkpoint_path=audit.directory / "must-not-exist.json", key_id="key-1", password=PASSWORD,
            now=NOW + timedelta(seconds=1), previous_checkpoint=audit.checkpoint, trusted_keys=audit.keys,
        )
    assert not (audit.directory / "must-not-exist.json").exists()


def test_key_rotation_requires_both_approved_historical_and_current_keys(audit):
    new_private = audit.private.with_name("private-2.pem")
    new_public = audit.public.with_name("public-2.pem")
    generate_keypair(new_private, new_public, password=PASSWORD, database_path=audit.store.path)
    old = replace(audit.keys["key-1"], signing_not_after=NOW + timedelta(seconds=1))
    new = TrustedKey("key-2", load_public_key(new_public), signing_not_before=NOW + timedelta(seconds=1))
    keys = {old.key_id: old, new.key_id: new}
    audit.store.add_task(audit.tenant, audit.run, "rotation-task", task_id="rotation", title="Rotation event")
    rotated = export_checkpoint(
        audit.store, audit.tenant, audit.run, private_key_path=new_private,
        checkpoint_path=audit.directory / "rotated.json", key_id="key-2", password=PASSWORD,
        now=NOW + timedelta(seconds=2), previous_checkpoint=audit.checkpoint, trusted_keys=keys,
    )
    assert verify(audit, rotated, previous_checkpoint=audit.checkpoint, trusted_keys=keys,
                  now=NOW + timedelta(seconds=2)).key_id == "key-2"
    with pytest.raises(CheckpointError, match="allowlist"):
        verify(audit, rotated, previous_checkpoint=audit.checkpoint, trusted_keys={new.key_id: new},
               now=NOW + timedelta(seconds=2))


def test_no_model_trust_flag_or_embedded_public_key_can_replace_signature(audit):
    for field, value in (("authenticated", True), ("public_key", "attacker-key")):
        changed = dict(audit.checkpoint)
        changed[field] = value
        with pytest.raises(CheckpointError, match="fields"):
            verify(audit, changed)


def test_key_and_checkpoint_paths_must_be_separate_from_database(audit):
    with pytest.raises(CheckpointError, match="outside"):
        generate_keypair(Path(audit.store.path).with_name("private.pem"), audit.public.with_name("p.pem"),
                         password=PASSWORD, database_path=audit.store.path)
    for output in (Path(audit.store.path).with_name("checkpoint.json"), audit.private.with_name("checkpoint.json")):
        with pytest.raises(CheckpointError):
            export_checkpoint(audit.store, audit.tenant, audit.run, private_key_path=audit.private,
                              checkpoint_path=output, key_id="key-1", password=PASSWORD, now=NOW)


def test_keys_and_snapshot_files_are_never_overwritten(audit):
    with pytest.raises(CheckpointError, match="already exist"):
        generate_keypair(audit.private, audit.public, password=PASSWORD, database_path=audit.store.path)
    with pytest.raises(CheckpointError, match="overwrite"):
        export_checkpoint(audit.store, audit.tenant, audit.run, private_key_path=audit.private,
                          checkpoint_path=audit.directory / "first.json", key_id="key-1", password=PASSWORD, now=NOW)


def test_wrong_private_key_password_never_writes_checkpoint(audit):
    output = audit.directory / "wrong-password.json"
    with pytest.raises(CheckpointError, match="decrypted"):
        export_checkpoint(audit.store, audit.tenant, audit.run, private_key_path=audit.private,
                          checkpoint_path=output, key_id="key-1", password=b"wrong", now=NOW)
    assert not output.exists()


def test_trusted_state_is_locked_and_invalid_candidate_does_not_replace_it(audit):
    advance(audit, initialize=True)
    original = audit.trusted.read_bytes()
    changed = dict(audit.checkpoint, head_hash="f" * 64)
    with pytest.raises(CheckpointError):
        advance(audit, changed)
    assert audit.trusted.read_bytes() == original
    lock = audit.trusted.with_name(audit.trusted.name + ".lock")
    lock.touch()
    with pytest.raises(CheckpointError, match="locked"):
        advance(audit)
    assert audit.trusted.read_bytes() == original
    assert lock.exists()


def test_missing_trusted_state_is_not_silently_bootstrapped_from_current_chain(audit):
    with pytest.raises(CheckpointError, match="explicit initialization"):
        advance(audit)
    assert not audit.trusted.exists()


def test_initialization_requires_a_valid_independent_signature(audit):
    forged = dict(audit.checkpoint, signature="A" * 88)
    with pytest.raises(CheckpointError):
        advance(audit, forged, initialize=True)
    assert not audit.trusted.exists()


def test_initialization_cannot_overwrite_existing_trusted_state(audit):
    advance(audit, initialize=True)
    original = audit.trusted.read_bytes()
    with pytest.raises(CheckpointError, match="already exists"):
        advance(audit, initialize=True)
    assert audit.trusted.read_bytes() == original


def test_duplicate_json_and_nonfinite_values_are_rejected(audit):
    for filename, content in (("duplicate.json", '{"schema":"a","schema":"b"}'),
                              ("nan.json", '{"seq":NaN}')):
        path = audit.directory / filename
        path.write_text(content, encoding="utf-8")
        with pytest.raises(CheckpointError):
            load_checkpoint(path)


def test_invalid_event_snapshot_is_not_signed(audit):
    events = audit.store.events(audit.tenant, audit.run)
    # Store.events normally guarantees this shape; the signer independently
    # rejects a malformed snapshot rather than trusting the adapter's assertion.
    events[0]["seq"] = True
    fake_store = SimpleNamespace(path=audit.store.path, events=lambda tenant, run: events)
    output = audit.directory / "invalid-events.json"
    with pytest.raises(CheckpointError):
        export_checkpoint(fake_store, audit.tenant, audit.run, private_key_path=audit.private,
                          checkpoint_path=output, key_id="key-1", password=PASSWORD, now=NOW)
    assert not output.exists()
