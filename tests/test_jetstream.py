import asyncio
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from loom.jetstream import JetStreamBridge, QueueMessageRejected, event_subject
from loom.store import Conflict, Store, canonical, digest


class Message:
    def __init__(self, subject, event):
        self.subject, self.data = subject, canonical(event).encode()
        self.acked, self.terminated = 0, 0
        self.ack_error = False

    async def ack_sync(self):
        if self.ack_error:
            raise ConnectionError("acknowledgement lost")
        self.acked += 1

    async def term(self):
        self.terminated += 1


class JetStreamTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "queue.db")
        self.run = self.store.create_run("tenant-a", "start", acceptance=["tests.pass"])["run_id"]
        self.bridge = JetStreamBridge(self.store)
        self.event = self.store.events("tenant-a", self.run)[0]
        self.subject = event_subject("loom.events", "tenant-a", self.run)

    def tearDown(self):
        self.tmp.cleanup()

    def effect(self, db, event):
        db.execute("INSERT INTO settings(key,value) VALUES ('queue_counter',1) ON CONFLICT(key) DO UPDATE SET value=value+1")

    async def test_publish_waits_for_durable_ack_and_keeps_failed_outbox(self):
        class Broker:
            async def publish(self, subject, payload, **options):
                raise ConnectionError("broker offline")
        with self.assertRaises(ConnectionError):
            await self.bridge.publish_pending(Broker())
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT sum(published) FROM outbox").fetchone()[0], 0)

    async def test_wrong_stream_ack_does_not_mark_published(self):
        class Broker:
            async def publish(self, *args, **kwargs):
                return SimpleNamespace(stream="OTHER", seq=1)
        with self.assertRaises(Conflict):
            await self.bridge.publish_pending(Broker())
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT sum(published) FROM outbox").fetchone()[0], 0)

    async def test_ack_loss_after_database_commit_does_not_repeat_effect(self):
        message = Message(self.subject, self.event)
        message.ack_error = True
        with self.assertRaises(ConnectionError):
            await self.bridge.consume_message(message, tenant="tenant-a", run_id=self.run, durable="C1", apply=self.effect)
        message.ack_error = False
        delivered = await self.bridge.consume_message(message, tenant="tenant-a", run_id=self.run, durable="C1", apply=self.effect)
        self.assertTrue(delivered.duplicate)
        self.assertEqual(message.acked, 1)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT value FROM settings WHERE key='queue_counter'").fetchone()[0], 1)

    async def test_failed_local_effect_is_not_acked_and_rolls_back_inbox(self):
        message = Message(self.subject, self.event)
        def fail(db, event):
            self.effect(db, event)
            raise RuntimeError("process exits before local commit")
        with self.assertRaises(RuntimeError):
            await self.bridge.consume_message(message, tenant="tenant-a", run_id=self.run, durable="C1", apply=fail)
        self.assertEqual(message.acked, 0)
        with self.store.connect() as db:
            self.assertIsNone(db.execute("SELECT value FROM settings WHERE key='queue_counter'").fetchone())
        result = await self.bridge.consume_message(message, tenant="tenant-a", run_id=self.run, durable="C1", apply=self.effect)
        self.assertTrue(result.applied)

    async def test_forged_broker_payload_with_recomputed_hash_is_rejected(self):
        altered = deepcopy(self.event)
        altered["type"] = "gate.passed"
        altered["hash"] = digest({k: v for k, v in altered.items() if k != "hash"})
        message = Message(self.subject, altered)
        with self.assertRaises(QueueMessageRejected):
            await self.bridge.consume_message(message, tenant="tenant-a", run_id=self.run, durable="C1", apply=self.effect)
        self.assertEqual((message.acked, message.terminated), (0, 1))
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM queue_rejections").fetchone()[0], 1)

    async def test_cross_tenant_subject_is_rejected_even_with_original_event(self):
        message = Message(event_subject("loom.events", "tenant-b", self.run), self.event)
        with self.assertRaises(QueueMessageRejected):
            await self.bridge.consume_message(message, tenant="tenant-a", run_id=self.run, durable="C1", apply=self.effect)
        self.assertEqual(message.terminated, 1)

    async def test_duplicate_json_keys_are_rejected_before_application(self):
        message = Message(self.subject, self.event)
        message.data = b'{"event_id":"one","event_id":"two"}'
        with self.assertRaises(QueueMessageRejected):
            await self.bridge.consume_message(message, tenant="tenant-a", run_id=self.run, durable="C1", apply=self.effect)

    async def test_out_of_order_delivery_catches_up_authoritative_prefix_once(self):
        events = self.store.events("tenant-a", self.run)
        seen = []
        def apply(db, event):
            self.effect(db, event)
            seen.append(event["seq"])
        last = Message(self.subject, events[-1])
        result = await self.bridge.consume_message(last, tenant="tenant-a", run_id=self.run, durable="ordered", apply=apply)
        self.assertEqual(result.caught_up_events, 3)
        self.assertEqual(seen, [1, 2, 3])
        first = Message(self.subject, events[0])
        duplicate = await self.bridge.consume_message(first, tenant="tenant-a", run_id=self.run, durable="ordered", apply=apply)
        self.assertTrue(duplicate.duplicate)
        self.assertEqual(seen, [1, 2, 3])

    async def test_async_effect_cannot_silently_commit_only_inbox(self):
        async def wrong_adapter(db, event):
            raise AssertionError("must not be invoked")
        message = Message(self.subject, self.event)
        with self.assertRaises(TypeError):
            await self.bridge.consume_message(message, tenant="tenant-a", run_id=self.run, durable="ordered", apply=wrong_adapter)
        self.assertEqual(message.acked, 0)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM inbox").fetchone()[0], 0)

    def test_subject_tokens_do_not_inherit_tenant_wildcards(self):
        subject = event_subject("loom.events", "tenant.*.>", "run.with.dots")
        self.assertEqual(len(subject.split(".")), 4)
        self.assertNotIn(">", subject)
        self.assertNotIn("*", subject)
        for prefix in ("loom.*", "loom..events", "loom.>", "loom events"):
            with self.assertRaises(ValueError):
                event_subject(prefix, "a", "r")


if __name__ == "__main__":
    unittest.main()
