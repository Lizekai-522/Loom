"""Trusted LOCAL fixtures. These are not real independent model signatures."""
from loom.policy import ArtifactSubmission, VerificationSignature
from loom.store import digest


def trusted_verification(claim, output, *, ruleset="kernel-v1", passed=True):
    content = digest(output)
    revision = f"{claim.task_id}:{claim.epoch}"
    signatures = tuple(VerificationSignature(f"fixture-verifier-{i}", f"fixture-model-{i}", content,
                        ruleset, revision, passed, authenticated=True) for i in range(2))
    return ArtifactSubmission(content, ruleset, revision, "fixture://evidence/" + content, True, passed,
                              dual_sign="signed", signatures=signatures)
