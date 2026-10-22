"""Reproducible, offline checkpoint fixture; never uses the API database.

Each invocation creates a fresh fixture run and unique key/trust/output session.
An existing database is accepted only if previously marked by this script.
Password source: LOOM_AUDIT_KEY_PASSWORD or process-local secure randomness.
No password, private key or private-key path is printed or written to events.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import secrets
import sqlite3
import sys
from uuid import uuid4

# Direct source-checkout execution is intentional; third-party dependencies
# still come from the project's explicitly prepared Python environment.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from loom.checkpoints import (
    CheckpointError, CheckpointRollback, TrustedKey, advance_trusted_checkpoint,
    export_checkpoint, generate_keypair, load_checkpoint, load_public_key,
    verify_checkpoint,
)
from loom.store import Store, canonical, digest


FIXTURE_MARKER = "loom:offline-checkpoint-demo:v1"
FIXTURE_TENANT = "checkpoint-fixture"


def _fixture_store(path: Path) -> Store:
    """Do not open Store (which initializes schema) on an unmarked existing DB."""
    if path.exists():
        with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as db:
            table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='checkpoint_demo_origin'").fetchone()
            if table is None:
                raise ValueError("refusing an existing database not created by this fixture")
            marker = db.execute("SELECT marker FROM checkpoint_demo_origin WHERE id=1").fetchone()
            if marker is None or marker[0] != FIXTURE_MARKER:
                raise ValueError("refusing a database with an unexpected fixture marker")
            if db.execute("SELECT 1 FROM runs WHERE tenant<>? LIMIT 1", (FIXTURE_TENANT,)).fetchone():
                raise ValueError("fixture database contains non-fixture runs")
    store = Store(path)
    with store.transaction() as db:
        db.execute("CREATE TABLE IF NOT EXISTS checkpoint_demo_origin (id INTEGER PRIMARY KEY CHECK(id=1), marker TEXT NOT NULL)")
        db.execute("INSERT OR IGNORE INTO checkpoint_demo_origin VALUES (1,?)", (FIXTURE_MARKER,))
    return store


def _rehashed_attack_copy(store: Store, tenant: str, run_id: str, path: Path) -> list[dict]:
    """Copy the fixture DB, then simulate a DB writer recomputing every hash."""
    if path.exists():
        raise ValueError("refusing to overwrite an attack fixture")
    with store.connect() as origin:
        with sqlite3.connect(path) as target:
            origin.backup(target)
    rewritten = deepcopy(store.events(tenant, run_id))
    rewritten[0]["payload"]["tampered_by_checkpoint_demo"] = True
    previous = "0" * 64
    for event in rewritten:
        event["prev_hash"] = previous
        body = {name: value for name, value in event.items() if name != "hash"}
        event["hash"] = digest(body)
        previous = event["hash"]
    with sqlite3.connect(path) as target:
        target.execute("DROP TRIGGER events_no_update")
        for event in rewritten:
            target.execute("UPDATE events SET envelope=?,hash=? WHERE id=?",
                           (canonical({name: value for name, value in event.items() if name != "hash"}),
                            event["hash"], event["event_id"]))
    # Read back the persisted attack, rather than proving only an in-memory edit.
    return Store(path).events(tenant, run_id)


def run_demo(*, database: Path, key_directory: Path, trust_directory: Path,
             output_directory: Path) -> dict:
    database = database.resolve()
    key_directory, trust_directory, output_directory = (
        path.resolve() for path in (key_directory, trust_directory, output_directory)
    )
    for private_directory in (key_directory, trust_directory):
        if private_directory == output_directory or private_directory.is_relative_to(output_directory):
            raise ValueError("private key/trust directories must not be inside public output")
        if private_directory == database.parent or private_directory.is_relative_to(database.parent):
            raise ValueError("private key/trust directories must be outside the database directory")
    if output_directory == database.parent or output_directory.is_relative_to(database.parent):
        raise ValueError("public checkpoints must be outside the database directory")
    if key_directory == trust_directory:
        raise ValueError("key and trusted-state directories must be distinct")

    session = uuid4().hex
    public_session = output_directory / session
    public_session.mkdir(parents=True, exist_ok=False)
    private_key = key_directory / session / "private.pem"
    public_key = public_session / "public.pem"
    trusted_head = trust_directory / session / "head.json"
    initial_path, next_path = public_session / "checkpoint-seq-3.json", public_session / "checkpoint-seq-4.json"
    result_path = public_session / "result.json"

    configured_password = os.environ.get("LOOM_AUDIT_KEY_PASSWORD")
    if configured_password == "":
        raise ValueError("an explicitly configured password must not be empty")
    password = configured_password.encode("utf-8") if configured_password is not None else secrets.token_bytes(32)

    store = _fixture_store(database)
    run = store.create_run(FIXTURE_TENANT, "checkpoint-demo-create:" + session,
                           acceptance=["schema.valid"])["run_id"]
    generate_keypair(private_key, public_key, password=password, database_path=database)
    key_id = "checkpoint-demo-" + session
    allowed_key = TrustedKey(key_id, load_public_key(public_key))
    keys = {key_id: allowed_key}  # Operator-configured fixture allowlist, never taken from checkpoint fields.
    identity = dict(expected_tenant=FIXTURE_TENANT, expected_run=run, expected_chain=run)

    first = export_checkpoint(
        store, FIXTURE_TENANT, run, private_key_path=private_key, checkpoint_path=initial_path,
        key_id=key_id, password=password,
    )
    first_events = store.events(FIXTURE_TENANT, run)
    verified_first = verify_checkpoint(first, first_events, trusted_keys=keys, **identity)
    initial_trusted = advance_trusted_checkpoint(
        first, first_events, trusted_checkpoint_path=trusted_head, database_path=database,
        trusted_keys=keys, initialize=True, **identity,
    )

    wrong_key_rejected = False
    wrong = TrustedKey(key_id, Ed25519PrivateKey.generate().public_key())
    try:
        verify_checkpoint(first, first_events, trusted_keys={key_id: wrong}, **identity)
    except CheckpointError:
        wrong_key_rejected = True

    store.add_task(FIXTURE_TENANT, run, "checkpoint-demo-add:" + session,
                   task_id="checkpoint-fixture-task-" + session,
                   title="Offline audit fixture; no model or tool execution")
    second = export_checkpoint(
        store, FIXTURE_TENANT, run, private_key_path=private_key, checkpoint_path=next_path,
        key_id=key_id, password=password, previous_checkpoint=load_checkpoint(trusted_head),
        trusted_keys=keys,
    )
    second_events = store.events(FIXTURE_TENANT, run)
    advanced = advance_trusted_checkpoint(
        second, second_events, trusted_checkpoint_path=trusted_head, database_path=database,
        trusted_keys=keys, **identity,
    )

    attack_database = database.with_name(database.stem + ".attack-" + session + database.suffix)
    attacked_events = _rehashed_attack_copy(store, FIXTURE_TENANT, run, attack_database)
    local_attack_self_consistent = Store.verify_events(attacked_events)
    signed_head_rejected = False
    signed_head_error = None
    try:
        verify_checkpoint(second, attacked_events, trusted_keys=keys, **identity)
    except CheckpointError as exc:
        signed_head_rejected = "independently signed head" in str(exc)
        signed_head_error = exc.code

    trusted_before = trusted_head.read_bytes()
    rollback_rejected = False
    rollback_error = None
    try:
        advance_trusted_checkpoint(
            first, first_events, trusted_checkpoint_path=trusted_head, database_path=database,
            trusted_keys=keys, **identity,
        )
    except CheckpointRollback as exc:
        rollback_rejected, rollback_error = True, exc.code
    trusted_unchanged = trusted_head.read_bytes() == trusted_before
    source_unchanged = store.events(FIXTURE_TENANT, run) == second_events

    checks = {
        "real_ed25519_signature_verified": verified_first.seq == 3,
        "operator_configured_public_key_verified": initial_trusted.key_id == key_id,
        "wrong_public_key_rejected": wrong_key_rejected,
        "trusted_high_water_advanced_from_3_to_4": initial_trusted.seq == 3 and advanced.seq == 4,
        "persisted_attacker_chain_self_consistent_after_full_rehash": local_attack_self_consistent,
        "attacker_chain_rejected_by_independent_signed_head": signed_head_rejected,
        "old_signed_snapshot_rollback_rejected": rollback_rejected,
        "trusted_head_unchanged_after_rejected_rollback": trusted_unchanged,
        "original_fixture_database_unchanged_by_attack_copy": source_unchanged,
        "no_model_or_agent_tool_execution": True,
    }
    if not all(checks.values()):
        raise RuntimeError("one or more checkpoint demo checks failed")
    raw_public = allowed_key.public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    result = {
        "schema": "loom.checkpoint-demo/v1", "status": "passed",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "fixture_only": True, "session_id": session, "tenant_id": FIXTURE_TENANT, "run_id": run,
        "key_id": key_id, "algorithm": "Ed25519",
        "sha256_raw_public_key": sha256(raw_public).hexdigest(),
        "initial_seq": verified_first.seq, "advanced_seq": advanced.seq,
        "checks": checks,
        "expected_rejection_codes": {"rewritten_head": signed_head_error, "rollback": rollback_error},
        "public_artifacts": {"public_key": str(public_key), "initial_checkpoint": str(initial_path),
                             "advanced_checkpoint": str(next_path), "result": str(result_path)},
        "private_material_exported": False,
        "limitations": [
            "Offline local fixture; the API database and real model/tool execution are not used.",
            "The demo explicitly configures its generated public key; production trust requires independent approval.",
            "Directory separation does not replace OS access control or independent key custody.",
            "Local signed files/high-water state are not external WORM, HSM or a rollback-proof remote service.",
            "First signing attests the current snapshot, not the integrity of history before initial trust establishment.",
        ],
    }
    with result_path.open("x", encoding="utf-8") as output:
        json.dump(result, output, ensure_ascii=False, indent=2, allow_nan=False)
        output.write("\n")
    return result


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True, help="dedicated fixture DB, never an API database")
    parser.add_argument("--key-dir", type=Path, required=True, help="private, non-public key directory")
    parser.add_argument("--trust-dir", type=Path, required=True, help="independently protected high-water directory")
    parser.add_argument("--output-dir", type=Path, required=True, help="public snapshot/result output directory")
    args = parser.parse_args(argv)
    try:
        result = run_demo(database=args.db, key_directory=args.key_dir,
                          trust_directory=args.trust_dir, output_directory=args.output_dir)
    except Exception as exc:
        # Do not emit passwords, key material or private filesystem content in
        # a traceback/error. Call run_demo directly for trusted developer debugging.
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__}), file=sys.stderr)
        return 1
    print(json.dumps({"status": result["status"], "checks_passed": len(result["checks"]),
                      "result": result["public_artifacts"]["result"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
