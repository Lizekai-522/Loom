"""One explicit, bounded real model request; no retries, no generated code execution."""
import argparse
import asyncio
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import time

from loom.local_config import PrivateConfigError, load_project_secrets
from loom.model_gateway import GatewayError, ModelGateway, load_model_config


async def check(args):
    started = time.monotonic()
    report = {"date": datetime.now(timezone(timedelta(hours=8))).isoformat(), "passed": False,
              "real_model_used": False, "requests_attempted": 0, "automatic_retries": 0,
              "generated_code_executed": False, "provider_cost_usd": None,
              "model_response_validated": False, "billing_uncertain": False}
    try:
        routes, roles = load_model_config(args.config)
        secrets = load_project_secrets(args.secrets_file, allowed_names={route.key_env for route in routes})
        async with ModelGateway(routes, role_aliases=roles, environ=secrets) as gateway:
            readiness = gateway.readiness(args.route)
            if not readiness.ready:
                raise GatewayError(readiness.reason)
            report["requests_attempted"] = 1
            report["billing_uncertain"] = True
            result = await gateway.generate(args.route,
                [{"role": "system", "content": "This is a Loom project connectivity check. Reply exactly LOOM_OK."},
                 {"role": "user", "content": "Verify the configured connection."}], args.max_output_tokens)
            report.update(real_model_used=True, provider=result.provider, model=result.model_id,
                          response_model=result.response_model, usage=asdict(result.usage),
                          request_id=result.request_id, finish_reason=result.finish_reason,
                          passed=not result.truncated and result.content.strip() == "LOOM_OK",
                          model_response_validated=True, billing_uncertain=False)
            if not report["passed"]:
                report["error_code"] = "MODEL_CHECK_RESPONSE_MISMATCH"
    except GatewayError as error:
        report.update(error_code=error.code, http_status=error.status_code)
    except PrivateConfigError:
        report["error_code"] = "PRIVATE_CONFIG_INVALID"
    report["seconds"] = round(time.monotonic() - started, 3)
    report["billing_note"] = "Usage comes from the provider; billed currency was not independently queried."
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("configs/models.example.json"))
    parser.add_argument("--secrets-file", type=Path, required=True)
    parser.add_argument("--route", default="deepseek-flash")
    parser.add_argument("--max-output-tokens", type=int, default=32)
    parser.add_argument("--report", type=Path, default=Path("artifacts/model-check.json"))
    args = parser.parse_args()
    if not 1 <= args.max_output_tokens <= 128:
        parser.error("connectivity check output must be limited to 1-128 tokens")
    if args.report.resolve() in {args.secrets_file.resolve(), args.config.resolve()}:
        parser.error("report cannot overwrite input configuration")
    report = asyncio.run(check(args))
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
