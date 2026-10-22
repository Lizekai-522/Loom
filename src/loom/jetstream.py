"""JetStream outbox bridge for the local reference store.

The event database remains authoritative. The broker transports notifications;
consumers compare each message with its persisted origin before applying local
SQL effects and inbox state in one transaction. Broker delivery is at least once.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import inspect
import math
import re
import sqlite3
from typing import Any, Callable

from .store import Conflict, Store, canonical, digest


class QueueMessageRejected(Conflict):
    code = "QUEUE_MESSAGE_REJECTED"


def _name(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        raise ValueError("stream/consumer name must be 1-64 letters, digits, hyphens or underscores")
    return value


def _prefix(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*", value):
        raise ValueError("subject prefix cannot contain wildcard, whitespace or empty tokens")
    return value


def event_subject(prefix: str, tenant: str, run_id: str) -> str:
    _prefix(prefix)
    if not isinstance(tenant, str) or not tenant or not isinstance(run_id, str) or not run_id:
        raise ValueError("tenant and run are required")
    # Opaque fixed tokens also prevent tenant names from becoming NATS wildcards.
    return prefix + "." + sha256(tenant.encode()).hexdigest() + "." + sha256(run_id.encode()).hexdigest()


def _strict_json(data: bytes) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def nonfinite(_):
        raise ValueError("nonfinite JSON value")

    value = json.loads(data.decode("utf-8"), object_pairs_hook=pairs, parse_constant=nonfinite)
    if not isinstance(value, dict):
        raise ValueError("event must be an object")
    return value


@dataclass(frozen=True)
class DeliveryResult:
    event_id: str
    applied: bool
    duplicate: bool
    caught_up_events: int = 0


class JetStreamBridge:
    def __init__(self, store: Store, *, stream: str = "LOOM_EVENTS", prefix: str = "loom.events"):
        self.store, self.stream, self.prefix = store, _name(stream), _prefix(prefix)
        with self.store.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS queue_rejections (
                consumer TEXT NOT NULL, message_digest TEXT NOT NULL, subject TEXT NOT NULL,
                reason TEXT NOT NULL, occurrences INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY(consumer,message_digest))""")
            db.execute("""CREATE TABLE IF NOT EXISTS queue_consumer_progress (
                consumer TEXT PRIMARY KEY, last_seq INTEGER NOT NULL, head_hash TEXT NOT NULL)""")

    async def provision(self, js) -> None:
        """Provision a dedicated durable stream; never repurpose an existing stream."""
        from nats.js.api import DiscardPolicy, RetentionPolicy, StorageType, StreamConfig
        from nats.js.errors import NotFoundError

        subjects = [self.prefix + ".*.*"]
        try:
            info = await js.stream_info(self.stream)
        except NotFoundError:
            await js.add_stream(config=StreamConfig(name=self.stream, subjects=subjects,
                                storage=StorageType.FILE, retention=RetentionPolicy.LIMITS,
                                num_replicas=1, duplicate_window=120, discard=DiscardPolicy.NEW,
                                max_msgs=-1, max_bytes=-1, max_msgs_per_subject=-1, max_age=0))
            info = await js.stream_info(self.stream)
        if set(info.config.subjects or []) != set(subjects) or info.config.storage != StorageType.FILE:
            raise Conflict("existing stream has incompatible subject coverage or nondurable storage")
        if info.config.retention != RetentionPolicy.LIMITS:
            raise Conflict("existing stream retention is incompatible with durable multi-consumer delivery")
        if (info.config.discard != DiscardPolicy.NEW or info.config.max_age not in (None, 0)
                or any(value not in (None, -1) for value in (info.config.max_msgs, info.config.max_bytes, info.config.max_msgs_per_subject))):
            raise Conflict("stream must reject new data when full and cannot silently expire acknowledged events")

    def _pending(self, limit: int) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 10000:
            raise ValueError("limit must be an integer from 1 to 10000")
        with self.store.connect() as db:
            return [dict(row) for row in db.execute("""SELECT e.id,e.tenant,e.run_id,e.envelope,e.hash
                FROM outbox o JOIN events e ON e.id=o.event_id
                WHERE o.published=0 ORDER BY e.rowid LIMIT ?""", (limit,))]

    def _mark_published(self, event_id: str) -> None:
        with self.store.transaction() as db:
            changed = db.execute("UPDATE outbox SET published=1 WHERE event_id=?", (event_id,)).rowcount
            if changed != 1:
                raise Conflict("outbox origin disappeared after broker acknowledgement")

    async def publish_pending(self, js, limit: int = 100) -> int:
        sent = 0
        for row in self._pending(limit):
            body = json.loads(row["envelope"])
            if (body.get("tenant_id"), body.get("run_id"), body.get("event_id")) != (row["tenant"], row["run_id"], row["id"]) or digest(body) != row["hash"]:
                raise QueueMessageRejected("persisted event identity or digest mismatch")
            event = {**body, "hash": row["hash"]}
            acknowledgement = await js.publish(event_subject(self.prefix, row["tenant"], row["run_id"]),
                            canonical(event).encode(), stream=self.stream,
                            headers={"Nats-Msg-Id": row["id"]})
            if acknowledgement.stream != self.stream or type(acknowledgement.seq) is not int or acknowledgement.seq < 1:
                raise Conflict("broker did not acknowledge the expected durable stream")
            # A crash here leaves a pending outbox row. The next publish uses the
            # same message ID; inbox deduplication remains necessary after the
            # broker's bounded deduplication window expires.
            self._mark_published(row["id"])
            sent += 1
        return sent

    async def subscribe(self, js, *, tenant: str, run_id: str, durable: str, ack_wait: float = 30):
        from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy
        from nats.js.errors import APIError, NotFoundError

        _name(durable)
        if isinstance(ack_wait, bool) or not isinstance(ack_wait, (int, float)) or not 0.05 <= ack_wait <= 3600:
            raise ValueError("ack_wait must be 0.05-3600 seconds")
        subject = event_subject(self.prefix, tenant, run_id)
        with self.store.connect() as db:
            self.store._run(db, tenant, run_id)
        config = ConsumerConfig(durable_name=durable, filter_subject=subject,
                                ack_policy=AckPolicy.EXPLICIT, deliver_policy=DeliverPolicy.ALL,
                                ack_wait=ack_wait, max_ack_pending=1, max_deliver=-1,
                                mem_storage=False, headers_only=False)
        try:
            info = await js.consumer_info(self.stream, durable)
        except NotFoundError:
            # nats-py 2.16 add_consumer is create-or-update: two NotFound
            # lookups can otherwise overwrite another tenant's filter or ack
            # contract after its subscription has already returned. Use the
            # server's atomic create-only action, then bind without auto-create.
            version = js._nc.connected_server_version
            if (version.major, version.minor, version.patch) < (2, 15, 1):
                raise Conflict("consumer create-only integration requires nats-server >= 2.15.1")
            try:
                await js._api_request(
                    f"{js._prefix}.CONSUMER.CREATE.{self.stream}.{durable}.{subject}",
                    canonical({"stream_name": self.stream, "config": config.as_dict(), "action": "create"}).encode(),
                    timeout=js._timeout)
            except APIError as exc:
                # Reconcile only a server-side conflicting creator. Transport,
                # permission and unrelated broker failures must fail closed.
                if exc.err_code != 10148:
                    raise
            info = await js.consumer_info(self.stream, durable)
        if (info.config.filter_subject != subject or info.config.ack_policy != AckPolicy.EXPLICIT
                or info.config.filter_subjects or info.config.deliver_subject or info.config.deliver_group
                or info.config.max_ack_pending != 1 or info.config.deliver_policy != DeliverPolicy.ALL
                or info.config.max_deliver not in (None, -1) or info.config.inactive_threshold not in (None, 0)
                or info.config.headers_only or info.config.mem_storage or info.config.pause_until
                or info.config.backoff or info.config.ack_wait is None
                or not math.isclose(info.config.ack_wait, ack_wait, abs_tol=0.000001)):
            raise Conflict("durable consumer already belongs to a different subject or ack contract")
        return await js.pull_subscribe_bind(consumer=durable, stream=self.stream)

    def _validate_message(self, data: bytes, subject: str, tenant: str, run_id: str) -> dict:
        try:
            event = _strict_json(data)
            if subject != event_subject(self.prefix, tenant, run_id):
                raise ValueError("wrong subject")
            if event.get("tenant_id") != tenant or event.get("run_id") != run_id or event.get("chain_id") != run_id:
                raise ValueError("wrong event tenant, run or chain")
            if type(event.get("seq")) is not int or event["seq"] < 1 or not isinstance(event.get("event_id"), str):
                raise ValueError("invalid sequence or event ID")
            if event.get("schema_version") != 1:
                raise ValueError("unsupported event schema version")
            body = {key: value for key, value in event.items() if key != "hash"}
            if event.get("hash") != digest(body):
                raise ValueError("event digest mismatch")
            with self.store.connect() as db:
                origin = db.execute("SELECT tenant,run_id,envelope,hash FROM events WHERE id=?", (event["event_id"],)).fetchone()
            if origin is None or origin["tenant"] != tenant or origin["run_id"] != run_id:
                raise ValueError("event absent from authorized origin log")
            if origin["hash"] != event["hash"] or json.loads(origin["envelope"]) != body:
                raise ValueError("broker payload differs from authoritative event")
            return event
        except (ValueError, KeyError, TypeError, UnicodeError, RecursionError) as exc:
            raise QueueMessageRejected(str(exc)) from exc

    def _consume_prefix(self, identity: str, event: dict, apply: Callable) -> int:
        """Order effects by authoritative seq even if multiple relays reorder messages.

        A notification for seq N catches up missing events through N from the
        origin log. Effects, inbox markers and cursor commit atomically. It is
        never necessary to trust a broker arrival order or wait behind a later
        message while max_ack_pending is one.
        """
        if inspect.iscoroutinefunction(apply):
            raise TypeError("consumer effect must be synchronous SQL in the provided transaction")
        with self.store.transaction() as db:
            if db.execute("SELECT 1 FROM inbox WHERE consumer=? AND event_id=?", (identity, event["event_id"])).fetchone():
                return 0
            cursor = db.execute("SELECT last_seq,head_hash FROM queue_consumer_progress WHERE consumer=?", (identity,)).fetchone()
            seq, previous = (cursor["last_seq"], cursor["head_hash"]) if cursor else (0, "0" * 64)
            if seq >= event["seq"]:
                raise Conflict("consumer progress has no matching inbox marker")
            rows = db.execute("SELECT id,seq,envelope,hash FROM events WHERE tenant=? AND run_id=? AND seq>? AND seq<=? ORDER BY seq",
                              (event["tenant_id"], event["run_id"], seq, event["seq"])).fetchall()
            count = 0
            for row in rows:
                body = json.loads(row["envelope"])
                if (row["seq"] != seq + 1 or body.get("seq") != row["seq"] or body.get("event_id") != row["id"]
                        or body.get("prev_hash") != previous or digest(body) != row["hash"]
                        or body.get("schema_version") != 1 or body.get("tenant_id") != event["tenant_id"]
                        or body.get("run_id") != event["run_id"] or body.get("chain_id") != event["chain_id"]):
                    raise Conflict("authoritative prefix is incomplete or corrupted")
                current = {**body, "hash": row["hash"]}
                result = apply(db, current)
                if inspect.isawaitable(result):
                    if inspect.iscoroutine(result):
                        result.close()
                    raise TypeError("consumer effect cannot return an awaitable")
                db.execute("INSERT INTO inbox(consumer,event_id) VALUES (?,?)", (identity, row["id"]))
                seq, previous = row["seq"], row["hash"]
                count += 1
            if seq != event["seq"] or previous != event["hash"]:
                raise Conflict("authoritative prefix does not reach notified event")
            db.execute("INSERT INTO queue_consumer_progress VALUES (?,?,?) ON CONFLICT(consumer) DO UPDATE SET last_seq=excluded.last_seq,head_hash=excluded.head_hash",
                       (identity, seq, previous))
            return count

    async def consume_message(self, message, *, tenant: str, run_id: str, durable: str,
                              apply: Callable[[sqlite3.Connection, dict], None]) -> DeliveryResult:
        _name(durable)
        subject = event_subject(self.prefix, tenant, run_id)
        identity = canonical({"stream": self.stream, "subject": subject, "durable": durable})
        try:
            event = self._validate_message(message.data, message.subject, tenant, run_id)
        except QueueMessageRejected as exc:
            with self.store.transaction() as db:
                db.execute("""INSERT INTO queue_rejections(consumer,message_digest,subject,reason) VALUES (?,?,?,?)
                    ON CONFLICT(consumer,message_digest) DO UPDATE SET occurrences=occurrences+1""",
                           (identity, sha256(message.data).hexdigest(), message.subject, str(exc)))
            # Do not endlessly retry poison data. A rejection digest and reason
            # have committed before asking the broker to terminate redelivery.
            await message.term()
            raise
        applied_count = self._consume_prefix(identity, event, apply)
        # Broker acknowledgement happens strictly after local SQL + inbox commit.
        # Ack loss is safe: redelivery sees the committed inbox marker.
        await message.ack_sync()
        return DeliveryResult(event["event_id"], applied_count > 0, applied_count == 0, applied_count)
