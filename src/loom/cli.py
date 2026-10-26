import argparse
import asyncio
import json
import os
from pathlib import Path

from .demo import run_demo
from .store import Store


def main():
    parser = argparse.ArgumentParser(prog="loom", description="Loom local deterministic kernel")
    commands = parser.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo", help="run seven-role fixture workflow with failure and repair")
    demo.add_argument("--db", default="artifacts/demo.db")
    demo.add_argument("--report", default="artifacts/demo-report.json")
    demo.add_argument("--pack", default="packs/software-dev/manifest.json")
    serve = commands.add_parser("serve", help="serve a local authenticated development API")
    serve.add_argument("--db", default="artifacts/dev.db")
    serve.add_argument("--port", type=int, default=8796)
    serve.add_argument("--identities-file", type=Path, help="private named local identities; never put tokens in command arguments")
    serve.add_argument("--local-console", action="store_true", help="本机开发模式：无需输入凭据，使用唯一已配置操作员；仅限回环访问")
    serve.add_argument("--executor-config",type=Path,help="本机执行器私有配置；页面只登记请求，由独立本机进程执行")
    relay = commands.add_parser("queue-drain", help="publish pending events to a dedicated JetStream stream")
    relay.add_argument("--db", required=True)
    relay.add_argument("--server", default="nats://127.0.0.1:4223")
    relay.add_argument("--stream", default="LOOM_EVENTS")
    relay.add_argument("--prefix", default="loom.events")
    relay.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    if args.command == "demo":
        report = run_demo(Store(args.db), args.pack)
        output = Path(args.report)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({k: v for k, v in report.items() if k not in {"events", "replay"}}, ensure_ascii=False, indent=2))
    elif args.command == "queue-drain":
        import nats
        from .jetstream import JetStreamBridge
        async def drain():
            connection = await nats.connect(args.server, allow_reconnect=False, connect_timeout=3)
            try:
                bridge = JetStreamBridge(Store(args.db), stream=args.stream, prefix=args.prefix)
                await bridge.provision(connection.jetstream())
                sent = await bridge.publish_pending(connection.jetstream(), limit=args.limit)
                print(json.dumps({"published": sent, "stream": args.stream, "delivery": "at-least-once"}))
            finally:
                await connection.close()
        asyncio.run(drain())
    else:
        keys = json.loads(os.environ.get("LOOM_TENANT_KEYS", "{}"))
        if not isinstance(keys, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in keys.items()):
            parser.error("LOOM_TENANT_KEYS must be a JSON object mapping tenant IDs to local API tokens")
        from .api import create_app
        from .auth import AuthConfigError, load_identities
        import uvicorn
        try:
            identities = load_identities(args.identities_file) if args.identities_file else ()
            from .executor_config import load_executor_config
            executor_config=load_executor_config(args.executor_config) if args.executor_config else None
            app = create_app(Store(args.db), keys, identities=identities, local_console=args.local_console,executor_config=executor_config)
        except (ValueError, AuthConfigError) as exc:
            parser.error(str(exc))
        # Bind exclusively to loopback. Internet/production serving is out of scope.
        uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
