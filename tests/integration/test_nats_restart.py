"""Crash a test-owned native broker and reconnect to its persisted JetStream data."""
import asyncio
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
from uuid import uuid4

import nats

from loom.jetstream import JetStreamBridge
from loom.store import Store


@unittest.skipUnless(os.environ.get("LOOM_NATS_SERVER_BIN"), "native NATS binary not configured for restart test")
class BrokerRestartTests(unittest.IsolatedAsyncioTestCase):
    async def test_puback_and_inbox_protect_effects_after_broker_process_crash(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            server = None
            client = None
            async def quiet_error(_):
                pass
            async def start():
                nonlocal server, client
                server = subprocess.Popen([os.environ["LOOM_NATS_SERVER_BIN"], "-js", "-a", "127.0.0.1", "-p", str(port), "-sd", str(base / "jetstream")],
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                          creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                for _ in range(100):
                    if server.poll() is not None:
                        self.fail("test broker exited during startup")
                    try:
                        client = await nats.connect(f"nats://127.0.0.1:{port}", allow_reconnect=False,
                                                    connect_timeout=0.2, error_cb=quiet_error)
                        return client.jetstream()
                    except (OSError, nats.errors.Error, asyncio.TimeoutError):
                        await asyncio.sleep(0.05)
                self.fail("test broker did not start")
            try:
                js = await start()
                store = Store(base / "origin.db")
                run = store.create_run("restart-tenant", "start", acceptance=["tests.pass"])["run_id"]
                stream = "RESTART_" + uuid4().hex[:16]
                bridge = JetStreamBridge(store, stream=stream, prefix="restart." + uuid4().hex[:16])
                await bridge.provision(js)
                subscription = await bridge.subscribe(js, tenant="restart-tenant", run_id=run, durable="durable", ack_wait=0.15)
                await bridge.publish_pending(js)
                def apply(db, event):
                    db.execute("INSERT INTO settings VALUES ('restart_effects',1) ON CONFLICT(key) DO UPDATE SET value=value+1")
                message = (await subscription.fetch(1, timeout=2))[0]
                await bridge.consume_message(message, tenant="restart-tenant", run_id=run, durable="durable", apply=apply)
                await client.close()
                client = None
                # Intentional process crash, not deletion of the server's data.
                server.kill()
                server.wait(timeout=10)
                js = await start()
                self.assertEqual((await js.stream_info(stream)).state.messages, 3)
                reopened = JetStreamBridge(Store(base / "origin.db"), stream=stream, prefix=bridge.prefix)
                await reopened.provision(js)
                subscription = await reopened.subscribe(js, tenant="restart-tenant", run_id=run, durable="durable", ack_wait=0.15)
                # Even a previously ack_sync'd notification may be redelivered
                # after a single-node process crash. Broker ack position is not
                # the business effect's authority; the local inbox is.
                for _ in range(4):
                    info = await js.consumer_info(stream, "durable")
                    if info.num_pending == 0 and info.num_ack_pending == 0:
                        break
                    message = (await subscription.fetch(1, timeout=2))[0]
                    await reopened.consume_message(message, tenant="restart-tenant", run_id=run, durable="durable", apply=apply)
                info = await js.consumer_info(stream, "durable")
                self.assertEqual((info.num_pending, info.num_ack_pending), (0, 0))
                with store.connect() as db:
                    self.assertEqual(db.execute("SELECT value FROM settings WHERE key='restart_effects'").fetchone()[0], 3)
            finally:
                if client is not None:
                    await client.close()
                if server is not None and server.poll() is None:
                    server.terminate()
                    server.wait(timeout=10)
