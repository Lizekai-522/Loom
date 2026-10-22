"""Explicit offline Ed25519 checkpoints for the local event adapter.

Verification requires an independently configured public-key allowlist and a
previous trusted checkpoint when enforcing a high-water mark. It never signs,
loads a key from an event, or bootstraps trust as a side effect of verification.
Local files are not WORM storage, and a local signing key is not a remote HSM.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
from typing import Any, Iterator, Mapping, Sequence, TYPE_CHECKING
from uuid import uuid4

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .store import canonical, digest

if TYPE_CHECKING:
    from .store import Store


SCHEMA = "loom.checkpoint/v1"
ALGORITHM = "Ed25519"
DOMAIN = b"loom:checkpoint:v1\x00"
ZERO_HASH = "0" * 64
MAX_CHECKPOINT_BYTES = 64 * 1024
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_FIELDS = frozenset({
    "schema", "algorithm", "key_id", "tenant_id", "run_id", "chain_id", "seq",
    "event_count", "head_hash", "created_at", "expires_at", "signature",
})
_EVENT_FIELDS = frozenset({
    "schema_version", "event_id", "tenant_id", "run_id", "chain_id", "seq", "ts",
    "type", "actor", "payload", "submission_key", "prev_hash", "hash",
})


class CheckpointError(ValueError):
    """Malformed, unauthenticated, stale, mismatching or unsafe checkpoint."""

    code = "CHECKPOINT_INVALID"


class CheckpointRollback(CheckpointError):
    code = "CHECKPOINT_ROLLBACK"


@dataclass(frozen=True)
class TrustedKey:
    """Operator-configured key, never supplied by the signed checkpoint itself.

    Time windows apply to the signing timestamp. Retired keys may remain in the
    allowlist for historical verification; revoked keys are always rejected.
    """

    key_id: str
    public_key: Ed25519PublicKey
    signing_not_before: datetime | None = None
    signing_not_after: datetime | None = None
    revoked: bool = False

    def __post_init__(self) -> None:
        _text(self.key_id, "key_id")
        if not isinstance(self.public_key, Ed25519PublicKey):
            raise CheckpointError("allowlist requires an Ed25519 public key")
        if type(self.revoked) is not bool:
            raise CheckpointError("revoked must be a configured boolean")
        if self.signing_not_before is not None:
            _utc(self.signing_not_before)
        if self.signing_not_after is not None:
            _utc(self.signing_not_after)
        if (self.signing_not_before is not None and self.signing_not_after is not None
                and self.signing_not_after < self.signing_not_before):
            raise CheckpointError("invalid key signing window")


@dataclass(frozen=True)
class VerifiedCheckpoint:
    tenant_id: str
    run_id: str
    chain_id: str
    seq: int
    event_count: int
    head_hash: str
    created_at: datetime
    expires_at: datetime
    key_id: str
    observed_count: int
    unanchored_tail_count: int


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 1000:
        raise CheckpointError(f"{name} must be a nonempty string <= 1000 characters")
    return value


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise CheckpointError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise CheckpointError("checkpoint timestamps must be canonical UTC strings")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise CheckpointError("invalid checkpoint timestamp") from exc
    if _timestamp(parsed) != value:
        raise CheckpointError("checkpoint timestamps must use six fractional UTC digits")
    return parsed


def _duration(value: timedelta, name: str, *, zero: bool = False) -> None:
    if not isinstance(value, timedelta) or value.total_seconds() < 0 or (not zero and not value):
        raise CheckpointError(f"invalid {name}")


def _same_identity(checkpoint: Mapping[str, Any], tenant: str, run: str, chain: str) -> None:
    for name, expected in (("tenant_id", tenant), ("run_id", run), ("chain_id", chain)):
        _text(expected, "expected_" + name)
        if checkpoint[name] != expected:
            raise CheckpointError(f"checkpoint {name} does not match expected identity")


def _signed_bytes(checkpoint: Mapping[str, Any]) -> bytes:
    body = {name: value for name, value in checkpoint.items() if name != "signature"}
    try:
        return DOMAIN + canonical(body).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CheckpointError("checkpoint is not canonical JSON data") from exc


def _authenticate(checkpoint: Mapping[str, Any], trusted_keys: Mapping[str, TrustedKey],
                  tenant: str, run: str, chain: str) -> tuple[datetime, datetime]:
    if not isinstance(checkpoint, Mapping) or set(checkpoint) != _FIELDS:
        raise CheckpointError("unexpected checkpoint fields or schema")
    if checkpoint["schema"] != SCHEMA or checkpoint["algorithm"] != ALGORITHM:
        raise CheckpointError("unsupported checkpoint schema or signature algorithm")
    for name in ("key_id", "tenant_id", "run_id", "chain_id"):
        _text(checkpoint[name], name)
    for name in ("seq", "event_count"):
        if type(checkpoint[name]) is not int or checkpoint[name] < 1:
            raise CheckpointError(f"{name} must be a positive integer")
    if checkpoint["event_count"] != checkpoint["seq"]:
        raise CheckpointError("v1 checkpoints require a complete chain starting at seq 1")
    if not isinstance(checkpoint["head_hash"], str) or not _HASH.fullmatch(checkpoint["head_hash"]):
        raise CheckpointError("invalid checkpoint head hash")
    created, expires = _parse_timestamp(checkpoint["created_at"]), _parse_timestamp(checkpoint["expires_at"])
    if expires <= created:
        raise CheckpointError("checkpoint expiry must follow signing time")
    _same_identity(checkpoint, tenant, run, chain)
    key = trusted_keys.get(checkpoint["key_id"])
    if (not isinstance(key, TrustedKey) or key.key_id != checkpoint["key_id"] or key.revoked):
        raise CheckpointError("checkpoint key is not in the active configured allowlist")
    if key.signing_not_before is not None and created < _utc(key.signing_not_before):
        raise CheckpointError("checkpoint predates the key signing window")
    if key.signing_not_after is not None and created > _utc(key.signing_not_after):
        raise CheckpointError("checkpoint follows the key signing window")
    if not isinstance(checkpoint["signature"], str):
        raise CheckpointError("checkpoint signature must be a base64 string")
    try:
        signature = base64.b64decode(checkpoint["signature"], validate=True)
    except (ValueError, TypeError) as exc:
        raise CheckpointError("checkpoint signature is not strict base64") from exc
    if len(signature) != 64:
        raise CheckpointError("invalid Ed25519 signature length")
    try:
        key.public_key.verify(signature, _signed_bytes(checkpoint))
    except InvalidSignature as exc:
        raise CheckpointError("checkpoint signature verification failed") from exc
    return created, expires


def _validate_events(events: Sequence[Mapping[str, Any]], tenant: str, run: str, chain: str) -> None:
    if not isinstance(events, (list, tuple)) or not events:
        raise CheckpointError("a complete nonempty event snapshot is required")
    previous, ids = ZERO_HASH, set()
    for seq, event in enumerate(events, 1):
        if not isinstance(event, Mapping) or set(event) != _EVENT_FIELDS:
            raise CheckpointError("unexpected event envelope fields")
        if type(event["schema_version"]) is not int or event["schema_version"] != 1:
            raise CheckpointError("unsupported event schema version")
        _same_identity(event, tenant, run, chain)
        if type(event["seq"]) is not int or event["seq"] != seq:
            raise CheckpointError("event sequence is incomplete or reordered")
        for name in ("event_id", "type", "actor"):
            _text(event[name], name)
        if event["event_id"] in ids:
            raise CheckpointError("duplicate event identity")
        ids.add(event["event_id"])
        try:
            finite_timestamp = type(event["ts"]) in (int, float) and math.isfinite(event["ts"])
        except OverflowError:
            finite_timestamp = False
        if (not finite_timestamp or not isinstance(event["payload"], dict)
                or event["submission_key"] is not None and not isinstance(event["submission_key"], str)):
            raise CheckpointError("invalid event timestamp, payload or submission key")
        if (not isinstance(event["hash"], str) or not _HASH.fullmatch(event["hash"])
                or event["prev_hash"] != previous):
            raise CheckpointError("event hash chain is inconsistent")
        try:
            actual = digest({name: value for name, value in event.items() if name != "hash"})
        except (TypeError, ValueError) as exc:
            raise CheckpointError("event is not canonical JSON data") from exc
        if actual != event["hash"]:
            raise CheckpointError("event envelope hash mismatch")
        previous = event["hash"]


def verify_checkpoint(checkpoint: Mapping[str, Any], events: Sequence[Mapping[str, Any]], *,
                      trusted_keys: Mapping[str, TrustedKey], expected_tenant: str,
                      expected_run: str, expected_chain: str,
                      previous_checkpoint: Mapping[str, Any] | None = None,
                      now: datetime | None = None, max_age: timedelta = timedelta(days=1),
                      clock_skew: timedelta = timedelta(seconds=60),
                      allow_unanchored_tail: bool = False) -> VerifiedCheckpoint:
    """Authenticate an external checkpoint, then compare its exact signed prefix.

    By default the observed chain must end at the signed seq/count/head. Prefix
    mode is explicit and reports the unauthenticated tail instead of calling it
    signed. A previous externally retained checkpoint enforces monotonic seq and
    prefix consistency, including across signing-key rotations.
    """
    _duration(max_age, "max_age")
    _duration(clock_skew, "clock_skew", zero=True)
    if type(allow_unanchored_tail) is not bool:
        raise CheckpointError("allow_unanchored_tail must be an explicit boolean")
    current = _utc(now or datetime.now(timezone.utc))
    created, expires = _authenticate(checkpoint, trusted_keys, expected_tenant, expected_run, expected_chain)
    if created > current + clock_skew:
        raise CheckpointError("checkpoint signing time is in the future")
    if expires <= current or current - created > max_age:
        raise CheckpointError("checkpoint expired or exceeds verifier freshness policy")
    _validate_events(events, expected_tenant, expected_run, expected_chain)
    seq = checkpoint["seq"]
    if len(events) < seq or not allow_unanchored_tail and len(events) != seq:
        raise CheckpointError("observed event count does not match signed checkpoint")
    if events[seq - 1]["hash"] != checkpoint["head_hash"]:
        raise CheckpointError("observed head differs from independently signed head")
    if previous_checkpoint is not None:
        previous_created, _ = _authenticate(previous_checkpoint, trusted_keys, expected_tenant, expected_run, expected_chain)
        # Previous records are historical anchors: their expiry does not erase
        # the known high-water mark. Their key must still be trusted for history.
        old_seq = previous_checkpoint["seq"]
        if seq < old_seq or created < previous_created:
            raise CheckpointRollback("checkpoint is older than the retained trusted high-water mark")
        if events[old_seq - 1]["hash"] != previous_checkpoint["head_hash"]:
            raise CheckpointRollback("candidate rewrites a previously trusted event prefix")
    return VerifiedCheckpoint(
        expected_tenant, expected_run, expected_chain, seq, checkpoint["event_count"],
        checkpoint["head_hash"], created, expires, checkpoint["key_id"], len(events), len(events) - seq,
    )


def _external_path(path: str | Path, database_path: str | Path) -> Path:
    resolved, database = Path(path).resolve(), Path(database_path).resolve()
    if resolved == database or resolved.is_relative_to(database.parent):
        raise CheckpointError("key, checkpoint and trusted state must be outside the database directory")
    return resolved


def _write_new(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as stream:
            try:
                os.chmod(path, 0o600)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            except BaseException:
                # A partial file is deliberately not reused or overwritten.
                raise
    except FileExistsError as exc:
        raise CheckpointError("refusing to overwrite an existing key or checkpoint") from exc


def generate_keypair(private_key_path: str | Path, public_key_path: str | Path, *,
                     password: bytes, database_path: str | Path) -> None:
    """Explicit operator action; encrypted PKCS8 private key, public SPKI PEM."""
    if not isinstance(password, bytes) or not password:
        raise CheckpointError("a nonempty private-key encryption password is required")
    private_path = _external_path(private_key_path, database_path)
    public_path = _external_path(public_key_path, database_path)
    if private_path == public_path or private_path.exists() or public_path.exists():
        raise CheckpointError("key paths must be distinct and must not already exist")
    key = Ed25519PrivateKey.generate()
    _write_new(private_path, key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(password),
    ))
    _write_new(public_path, key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
    ))


def load_public_key(path: str | Path) -> Ed25519PublicKey:
    try:
        key = serialization.load_pem_public_key(Path(path).read_bytes())
    except (ValueError, TypeError) as exc:
        raise CheckpointError("invalid public-key PEM") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise CheckpointError("allowlisted public key must be Ed25519")
    return key


def load_checkpoint(path: str | Path) -> dict[str, Any]:
    """Read data only. Loading never authenticates it or establishes trust."""
    def no_duplicates(items):
        result = {}
        for name, value in items:
            if name in result:
                raise CheckpointError("duplicate checkpoint JSON field")
            result[name] = value
        return result

    path = Path(path)
    if path.stat().st_size > MAX_CHECKPOINT_BYTES:
        raise CheckpointError("checkpoint file is too large")
    try:
        result = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=no_duplicates,
                            parse_constant=lambda value: (_ for _ in ()).throw(CheckpointError("nonfinite JSON value")))
    except (ValueError, UnicodeError) as exc:
        raise CheckpointError("invalid checkpoint JSON") from exc
    if not isinstance(result, dict):
        raise CheckpointError("checkpoint must be a JSON object")
    return result


def export_checkpoint(store: Store, tenant: str, run_id: str, *,
                      private_key_path: str | Path, checkpoint_path: str | Path,
                      key_id: str, password: bytes,
                      now: datetime | None = None, valid_for: timedelta = timedelta(days=1),
                      previous_checkpoint: Mapping[str, Any] | None = None,
                      trusted_keys: Mapping[str, TrustedKey] | None = None) -> dict[str, Any]:
    """Explicit signing action; immutable snapshot file separate from DB and key.

    On first export the operator is attesting the current history. Export cannot
    prove what happened before the first independently retained checkpoint.
    Later exports should supply the previous trusted record and key allowlist.
    """
    _text(key_id, "key_id")
    _duration(valid_for, "valid_for")
    if not isinstance(password, bytes) or not password:
        raise CheckpointError("private-key password is required")
    private_path = _external_path(private_key_path, store.path)
    output = _external_path(checkpoint_path, store.path)
    if output == private_path or output.parent == private_path.parent:
        raise CheckpointError("private key and checkpoint must use separate directories")
    if output.exists():
        raise CheckpointError("refusing to overwrite a checkpoint")
    try:
        key = serialization.load_pem_private_key(private_path.read_bytes(), password=password)
    except (ValueError, TypeError) as exc:
        raise CheckpointError("private key could not be decrypted") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise CheckpointError("signing key must be Ed25519")
    events = store.events(tenant, run_id)
    _validate_events(events, tenant, run_id, run_id)
    current = _utc(now or datetime.now(timezone.utc))
    checkpoint = {
        "schema": SCHEMA, "algorithm": ALGORITHM, "key_id": key_id,
        "tenant_id": tenant, "run_id": run_id, "chain_id": run_id,
        "seq": len(events), "event_count": len(events), "head_hash": events[-1]["hash"],
        "created_at": _timestamp(current), "expires_at": _timestamp(current + valid_for),
    }
    checkpoint["signature"] = base64.b64encode(key.sign(_signed_bytes(checkpoint))).decode("ascii")
    if previous_checkpoint is not None:
        if trusted_keys is None:
            raise CheckpointError("previous checkpoint requires the independent key allowlist")
        verify_checkpoint(checkpoint, events, trusted_keys=trusted_keys, expected_tenant=tenant,
                          expected_run=run_id, expected_chain=run_id, previous_checkpoint=previous_checkpoint,
                          now=current, max_age=valid_for)
    _write_new(output, (canonical(checkpoint) + "\n").encode("utf-8"))
    return checkpoint


@contextmanager
def _state_lock(path: Path) -> Iterator[None]:
    """Exclusive fail-closed local writer lock; a crash may require manual recovery."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name(path.name + ".lock")
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise CheckpointError("trusted checkpoint state is locked; inspect before retrying") from exc
    try:
        os.close(descriptor)
        yield
    finally:
        lock.unlink()


def advance_trusted_checkpoint(checkpoint: Mapping[str, Any], events: Sequence[Mapping[str, Any]], *,
                               trusted_checkpoint_path: str | Path, database_path: str | Path,
                               trusted_keys: Mapping[str, TrustedKey], expected_tenant: str,
                               expected_run: str, expected_chain: str,
                               now: datetime | None = None, max_age: timedelta = timedelta(days=1),
                               clock_skew: timedelta = timedelta(seconds=60),
                               initialize: bool = False) -> VerifiedCheckpoint:
    """Verify and atomically advance one independently protected run high-water file.

    Missing trusted state fails closed unless the operator explicitly requests
    initialize=True. Initialization still requires real signature verification;
    it never takes its expected head or public key from the untrusted database.
    Existing state cannot be initialized over. The file must not be writable by DB/Workers.
    It is mutable local state, not an external monotonic service or WORM anchor.
    """
    path = _external_path(trusted_checkpoint_path, database_path)
    if type(initialize) is not bool:
        raise CheckpointError("initialize must be an explicit boolean")
    with _state_lock(path):
        if not path.exists() and not initialize:
            raise CheckpointError("trusted high-water state is missing; explicit initialization required")
        if path.exists() and initialize:
            raise CheckpointError("trusted high-water state already exists; advance instead of initializing")
        previous = load_checkpoint(path) if path.exists() else None
        verified = verify_checkpoint(
            checkpoint, events, trusted_keys=trusted_keys, expected_tenant=expected_tenant,
            expected_run=expected_run, expected_chain=expected_chain, previous_checkpoint=previous,
            now=now, max_age=max_age, clock_skew=clock_skew,
        )
        temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
        try:
            _write_new(temporary, (canonical(dict(checkpoint)) + "\n").encode("utf-8"))
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()
        return verified
