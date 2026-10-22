import argparse
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
    args = parser.parse_args()
    if args.command == "demo":
        report = run_demo(Store(args.db), args.pack)
        output = Path(args.report)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({k: v for k, v in report.items() if k not in {"events", "replay"}}, ensure_ascii=False, indent=2))
    else:
        keys = json.loads(os.environ.get("LOOM_TENANT_KEYS", "{}"))
        if not isinstance(keys, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in keys.items()):
            parser.error("LOOM_TENANT_KEYS must be a JSON object mapping tenant IDs to local API tokens")
        from .api import create_app
        import uvicorn
        try:
            app = create_app(Store(args.db), keys)
        except ValueError as exc:
            parser.error(str(exc))
        # Bind exclusively to loopback. Internet/production serving is out of scope.
        uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
