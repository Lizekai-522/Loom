"""Requires an explicitly configured real JetStream server, never auto-fakes it."""
import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

import nats

from loom.jetstream import JetStreamBridge
from loom.store import Store


@unittest.skipUnless(os.environ.get("LOOM_NATS_TEST_URL"), "live NATS URL not configured")
class LiveNatsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "integration.db")
        self.run = self.store.create_run("live-tenant", "start", acceptance=["tests.pass"])["run_id"]
        self.stream = "LOOM_TEST_" + uuid4().hex[:16]
        self.prefix = "loomtest." + uuid4().hex[:16]
        self.bridge = JetStreamBridge(self.store, stream=self.stream, prefix=self.prefix)
        self.nc = await nats.connect(os.environ["LOOM_NATS_TEST_URL"], allow_reconnect=False, connect_timeout=3)
        self.js = self.nc.jetstream()
        await self.bridge.provision(self.js)

    async def asyncTearDown(self):
        if hasattr(self, "js"):
            await self.js.delete_stream(self.stream)
        if hasattr(self, "nc"):
            await self.nc.close()
        self.tmp.cleanup()

    def effect(self, db, event):
        db.execute("INSERT INTO settings(key,value) VALUES ('live_effect_count',1) ON CONFLICT(key) DO UPDATE SET value=value+1")

    async def test_real_durable_publish_and_consume(self):
        subscription = await self.bridge.subscribe(self.js, tenant="live-tenant", run_id=self.run, durable="worker")
        self.assertEqual(await self.bridge.publish_pending(self.js), 3)
        for _ in range(3):
            message = (await subscription.fetch(1, timeout=2))[0]
            result = await self.bridge.consume_message(message, tenant="live-tenant", run_id=self.run, durable="worker", apply=self.effect)
            self.assertTrue(result.applied)
        info = await self.js.consumer_info(self.stream, "worker")
        self.assertEqual((info.num_ack_pending, info.num_pending), (0, 0))
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT value FROM settings WHERE key='live_effect_count'").fetchone()[0], 3)

    async def test_domain_pack_publication_delivers_with_immutable_content_after_reopen(self):
        from loom.domain_packs import DomainPackService, builtin_pack
        from loom.workflow import WorkflowService
        run = WorkflowService(self.store).create_goal('live-tenant','assets',title='Assets',
            description='No execution',acceptance=['schema.valid'])['run_id']
        data = builtin_pack().model_dump(mode='json'); data.update(name='nats-software',version='1.0.0')
        published = DomainPackService(self.store).publish('live-tenant','publish-pack',run,data,actor='subject:operator')
        subscription = await self.bridge.subscribe(self.js,tenant='live-tenant',run_id=run,durable='pack-reader')
        await self.bridge.publish_pending(self.js)
        received = []
        for _ in self.store.events('live-tenant',run):
            message = (await subscription.fetch(1,timeout=2))[0]
            def apply(db,event):
                if event['type']=='domain_pack.published':received.append(event['payload'])
            await self.bridge.consume_message(message,tenant='live-tenant',run_id=run,durable='pack-reader',apply=apply)
        self.assertEqual(len(received),1)
        self.assertEqual(received[0]['reference'],published['reference'])
        self.assertEqual(DomainPackService(Store(self.store.path)).get('live-tenant','nats-software','1.0.0'),published)

    async def test_exit_boundary_after_broker_ack_republishes_same_message_id(self):
        mark = self.bridge._mark_published
        def fail(_):
            raise RuntimeError("simulated exit after durable broker ack, before local mark")
        self.bridge._mark_published = fail
        with self.assertRaises(RuntimeError):
            await self.bridge.publish_pending(self.js, limit=1)
        self.bridge._mark_published = mark
        restarted = JetStreamBridge(Store(self.store.path), stream=self.stream, prefix=self.prefix)
        self.assertEqual(await restarted.publish_pending(self.js, limit=1), 1)
        info = await self.js.stream_info(self.stream)
        self.assertEqual(info.state.messages, 1)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT sum(published) FROM outbox").fetchone()[0], 1)

    async def test_ack_loss_redelivery_does_not_duplicate_database_effect(self):
        subscription = await self.bridge.subscribe(self.js, tenant="live-tenant", run_id=self.run, durable="recover", ack_wait=0.15)
        await self.bridge.publish_pending(self.js, limit=1)
        message = (await subscription.fetch(1, timeout=2))[0]
        class LostAck:
            data, subject = message.data, message.subject
            async def ack_sync(self):
                raise ConnectionError("simulated network loss before ack reaches broker")
        with self.assertRaises(ConnectionError):
            await self.bridge.consume_message(LostAck(), tenant="live-tenant", run_id=self.run, durable="recover", apply=self.effect)
        await asyncio.sleep(0.2)
        delivered_again = (await subscription.fetch(1, timeout=2))[0]
        result = await self.bridge.consume_message(delivered_again, tenant="live-tenant", run_id=self.run, durable="recover", apply=self.effect)
        self.assertTrue(result.duplicate)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT value FROM settings WHERE key='live_effect_count'").fetchone()[0], 1)

    async def test_durable_cannot_be_reused_for_another_tenant(self):
        from loom.store import Conflict
        await self.bridge.subscribe(self.js, tenant="live-tenant", run_id=self.run, durable="bound")
        other = self.store.create_run("other-tenant", "start", acceptance=["tests.pass"])["run_id"]
        with self.assertRaises(Conflict):
            await self.bridge.subscribe(self.js, tenant="other-tenant", run_id=other, durable="bound")

    async def _race_durable_creation(self, first, second, durable):
        from nats.js.errors import NotFoundError
        arrived, barrier = 0, asyncio.Event()
        creation_count, first_bound = 0, asyncio.Event()
        real_js = self.js

        class SimultaneousLookup:
            def __getattr__(self, name):
                return getattr(real_js, name)

            async def consumer_info(self, stream, consumer):
                nonlocal arrived
                if consumer == durable and arrived < 2:
                    arrived += 1
                    if arrived == 2:
                        barrier.set()
                    await asyncio.wait_for(barrier.wait(), 3)
                    raise NotFoundError(code=404)
                return await real_js.consumer_info(stream, consumer)

            async def create_in_order(self, method, *args, **kwargs):
                nonlocal creation_count
                creation_count += 1
                if creation_count == 2:
                    # Both lookups were NotFound, but the first caller has
                    # already returned a valid subscription before the second
                    # create request reaches the real broker.
                    await asyncio.wait_for(first_bound.wait(), 3)
                return await method(*args, **kwargs)

            async def add_consumer(self, *args, **kwargs):
                return await self.create_in_order(real_js.add_consumer, *args, **kwargs)

            async def _api_request(self, *args, **kwargs):
                return await self.create_in_order(real_js._api_request, *args, **kwargs)

            async def pull_subscribe(self, *args, **kwargs):
                subscription = await real_js.pull_subscribe(*args, **kwargs)
                first_bound.set()
                return subscription

            async def pull_subscribe_bind(self, *args, **kwargs):
                subscription = await real_js.pull_subscribe_bind(*args, **kwargs)
                first_bound.set()
                return subscription

        js = SimultaneousLookup()
        results = await asyncio.gather(
            self.bridge.subscribe(js, durable=durable, **first),
            self.bridge.subscribe(js, durable=durable, **second), return_exceptions=True)
        from loom.store import Conflict
        self.assertEqual(sum(isinstance(result, Conflict) for result in results), 1, results)
        winner = next(i for i, result in enumerate(results) if not isinstance(result, Exception))
        from loom.jetstream import event_subject
        expected = (first, second)[winner]
        info = await self.js.consumer_info(self.stream, durable)
        self.assertEqual(info.config.filter_subject, event_subject(self.prefix, expected["tenant"], expected["run_id"]))
        self.assertAlmostEqual(info.config.ack_wait, expected.get("ack_wait", 30))

    async def test_concurrent_tenants_cannot_rebind_the_same_durable(self):
        other = self.store.create_run("other-tenant", "start", acceptance=["tests.pass"])["run_id"]
        await self._race_durable_creation(
            {"tenant": "live-tenant", "run_id": self.run},
            {"tenant": "other-tenant", "run_id": other}, "racing_scope")

    async def test_concurrent_creation_cannot_overwrite_ack_contract(self):
        await self._race_durable_creation(
            {"tenant": "live-tenant", "run_id": self.run, "ack_wait": 30},
            {"tenant": "live-tenant", "run_id": self.run, "ack_wait": 0.15}, "racing_ack")

    async def test_preexisting_new_only_or_finite_retry_consumer_is_rejected(self):
        from nats.js.api import ConsumerConfig, DeliverPolicy, AckPolicy
        from loom.jetstream import event_subject
        from loom.store import Conflict
        for name, options in (("new_only", {"deliver_policy": DeliverPolicy.NEW}), ("single_try", {"max_deliver": 1}),
                              ("expires", {"inactive_threshold": 60})):
            config = ConsumerConfig(durable_name=name, filter_subject=event_subject(self.prefix, "live-tenant", self.run),
                                    ack_policy=AckPolicy.EXPLICIT, max_ack_pending=1, **options)
            await self.js.add_consumer(self.stream, config=config)
            with self.assertRaises(Conflict):
                await self.bridge.subscribe(self.js, tenant="live-tenant", run_id=self.run, durable=name)

    async def test_preexisting_stream_with_silent_eviction_is_rejected(self):
        from nats.js.api import StreamConfig, StorageType, DiscardPolicy
        from loom.store import Conflict
        await self.js.update_stream(config=StreamConfig(name=self.stream, subjects=[self.prefix + ".*.*"], storage=StorageType.FILE,
                                                        discard=DiscardPolicy.OLD, max_msgs=1))
        with self.assertRaises(Conflict):
            await self.bridge.provision(self.js)

    async def test_frozen_development_suite_event_survives_durable_delivery_and_restart(self):
        import json
        from loom.artifacts import ArtifactService
        from loom.development_suites import DevelopmentSuiteService
        from loom.workflow import WorkflowService
        workflow = WorkflowService(self.store)
        goal = workflow.create_goal("live-tenant", "suite-goal", title="Review suite",
                                    description="Development only", acceptance=["tests.pass"])
        run = goal["run_id"]
        definition = json.loads((Path(__file__).resolve().parents[2] / "packs/software-dev/workflow-v1.json").read_text(encoding="utf-8"))
        definition["nodes"] = [{"id": "checks", "title": "Public cases", "instruction": "Data only",
            "role": "tester", "depends": [],
            "limits": {"cost_limit_microusd": 1000, "token_limit": 1000, "max_attempts": 1}}]
        saved = workflow.save_plan("live-tenant", run, "suite-plan", definition, 0)
        workflow.approve_plan("live-tenant", run, "suite-approve", saved["revision"], saved["digest"])
        claim = self.store.claim_next("live-tenant", run, "tester", lease_seconds=3600)
        body = {"schema_version": "loom.public-checks/v1", "visibility": "development",
                "cases": [{"name": "exact", "input": 9007199254740993, "expected": True}]}
        candidate = ArtifactService(self.store).save_candidate(
            claim, "suite-candidate", "public-checks", body, actor="subject:tester")
        service = DevelopmentSuiteService(self.store)
        preview = service.preview("live-tenant", run, candidate["artifact_id"])
        frozen = service.freeze("live-tenant", run, candidate["artifact_id"], "suite-freeze",
                                actor="subject:operator", expected_review_digest=preview["review_digest"])
        with self.store.connect() as db:
            event_count = db.execute("SELECT count(*) FROM events WHERE tenant=? AND run_id=?",
                                     ("live-tenant", run)).fetchone()[0]
        subscription = await self.bridge.subscribe(self.js, tenant="live-tenant", run_id=run, durable="suite", ack_wait=0.15)
        await self.bridge.publish_pending(self.js)
        received = []
        def apply(db, event):
            if event["type"] == "development.suite_frozen":
                received.append(event)
        last = None
        for index in range(event_count):
            last = (await subscription.fetch(1, timeout=2))[0]
            if index == event_count - 1:
                class LostAck:
                    data, subject = last.data, last.subject
                    async def ack_sync(self):
                        raise ConnectionError("simulated lost acknowledgement")
                with self.assertRaises(ConnectionError):
                    await self.bridge.consume_message(LostAck(), tenant="live-tenant", run_id=run,
                                                       durable="suite", apply=apply)
            else:
                result = await self.bridge.consume_message(last, tenant="live-tenant", run_id=run,
                                                           durable="suite", apply=apply)
                self.assertTrue(result.applied)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["payload"]["suite_digest"], frozen["suite_digest"])
        self.assertFalse(received[0]["payload"]["execution_connected"])
        self.assertNotIn("body", received[0]["payload"])
        await asyncio.sleep(0.2)
        redelivered = (await subscription.fetch(1, timeout=2))[0]
        duplicate = await self.bridge.consume_message(redelivered, tenant="live-tenant", run_id=run,
                                                      durable="suite", apply=apply)
        self.assertTrue(duplicate.duplicate)
        self.assertEqual(len(received), 1)
        restarted = DevelopmentSuiteService(Store(self.store.path))
        view = restarted.get_suite("live-tenant", run, frozen["suite_id"], include_body=True)
        self.assertEqual(view["body"], body)
        self.assertEqual(view["freeze_event_hash"], frozen["freeze_event_hash"])
