"""Scalar model-source labels after ArtifactService validates provenance.

These labels record a response in the trusted local gateway. They never prove
content correctness, independent verification, or completion of a dependency.
"""
import re

from .artifact_schemas import ARTIFACT_ROLES, ARTIFACT_SCHEMAS
from .store import Conflict, NotFound


def model_source_summary(candidate):
    if (type(candidate) is not dict or candidate.get("origin") != "model-gateway-response"
            or candidate.get("model_source_recorded") is not True):
        return None
    provenance = candidate.get("model_provenance")
    kind, schema = candidate.get("kind"), candidate.get("schema_version")
    if (type(provenance) is not dict or type(kind) is not str or kind not in ARTIFACT_SCHEMAS
            or schema != ARTIFACT_SCHEMAS[kind] or provenance.get("role") != ARTIFACT_ROLES[kind]
            or provenance.get("provider") != "deepseek" or provenance.get("model") not in {'deepseek-flash','deepseek-v4-pro'}
            or (provenance.get('model')=='deepseek-v4-pro' and (kind!='review-findings' or provenance.get('role')!='reviewer'))):
        return None
    route, usage = provenance.get("route"), provenance.get("usage")
    if (type(route) is not str or re.fullmatch(r"[a-z][a-z0-9_-]{0,47}", route) is None
            or type(usage) is not dict):
        return None
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = usage.get(key)
        if type(value) is not int or not 0 <= value <= 1048576:
            return None
    if usage["prompt_tokens"] + usage["completion_tokens"] != usage["total_tokens"]:
        return None
    cost = provenance.get("usage_cost_upper_microusd")
    epoch = candidate.get("source_epoch")
    artifact = candidate.get("artifact_id")
    if (type(cost) is not int or not 0 <= cost <= 1000000000000 or type(epoch) is not int or epoch < 1
            or type(artifact) is not str or re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", artifact) is None):
        return None
    values = {"call_id": provenance.get("call_id"), "artifact_content_hash": candidate.get("content_hash"),
              "plan_digest": candidate.get("plan_digest"), "source_event_hash": candidate.get("source_event_hash")}
    if any(type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None for value in values.values()):
        return None
    result = {**values, "artifact_id": artifact, "role": provenance["role"], "kind": kind,
            "schema_version": schema, "source_epoch": epoch, "provider": "deepseek", "model": provenance['model'],
            "route": route, "prompt_tokens": usage["prompt_tokens"], "completion_tokens": usage["completion_tokens"],
            "total_tokens": usage["total_tokens"], "usage_cost_upper_microusd": cost,
            "model_response_recorded": True, "producer_verified": False, "counts_as_success": False}
    if kind == "public-checks":
        result["public_checks_executed"] = False
    return result


def submitted_role_source(artifacts, tenant, run_id, task, spec, output):
    """Bind a displayed task output to a verified, current candidate record."""
    fallback = {"model_source_recorded": False, "model_source_status": "unavailable"}
    artifact_id = output.get("artifact_id")
    if type(artifact_id) is not str or len(artifact_id) > 200:
        return fallback
    try:
        candidate = artifacts.get_artifact(tenant, run_id, artifact_id, include_body=False)
    except (Conflict, NotFound):
        return fallback
    source = model_source_summary(candidate)
    if source is None:
        return {"model_source_recorded": False, "model_source_status": "worker_submission"}
    contract = spec.get("role_output") if spec else None
    archival_source=task['status']=='done' and candidate.get('stale_reasons')==['run_terminal']
    if (type(contract) is not dict or (candidate["stale"] and not archival_source)
            or candidate["source_task_id"] != task["id"] or candidate["source_epoch"] != task["epoch"]
            or candidate["plan_digest"] != spec["plan_digest"] or candidate["kind"] != contract["kind"]
            or candidate["schema_version"] != contract["schema_version"]
            or any(output.get(key) != value for key, value in {
                "artifact_kind": candidate["kind"], "artifact_schema_version": candidate["schema_version"],
                "artifact_content_hash": candidate["content_hash"], "plan_digest": candidate["plan_digest"],
                "model_call_id": source["call_id"]}.items())):
        return {"model_source_recorded": False, "model_source_status": "version_mismatch"}
    return {"model_source_recorded": True, "model_source_status": "recorded", "model_provenance": source}
