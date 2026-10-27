"""Named, role-separated identities for the loopback development API.

This module is a local authentication boundary, not OIDC, a password service,
or proof of independent people. Legacy developer keys never gain control rights.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from hmac import compare_digest
import json
import os
from pathlib import Path
import re
from types import MappingProxyType


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}")
_TOKEN = re.compile(r"[!-~]{32,4096}")
_ROLES = frozenset({"operator", "worker", "viewer"})
_PERMISSIONS = MappingProxyType({
    "operator": frozenset({"read", "goal.write", "plan.approve", "control", "suite.approve"}),
    "worker": frozenset({"read", "task.claim", "task.submit"}),
    "viewer": frozenset({"read"}),
    "developer": frozenset({"read", "goal.write", "plan.approve", "task.claim", "task.submit"}),
})


class AuthConfigError(ValueError):
    code = "LOCAL_AUTH_CONFIG_INVALID"

    def __init__(self):
        super().__init__(self.code)


def _require(condition: bool) -> None:
    if not condition:
        raise AuthConfigError()


def _identifier(value: object) -> bool:
    return isinstance(value, str) and _ID.fullmatch(value) is not None


@dataclass(frozen=True, slots=True)
class Principal:
    tenant_id: str
    subject_id: str
    roles: frozenset[str]

    def __post_init__(self):
        _require(isinstance(self.tenant_id, str) and bool(self.tenant_id)
                 and isinstance(self.subject_id, str) and bool(self.subject_id)
                 and type(self.roles) is frozenset and bool(self.roles)
                 and self.roles <= frozenset(_PERMISSIONS))


@dataclass(frozen=True, slots=True)
class LocalIdentity:
    tenant_id: str
    subject_id: str
    roles: frozenset[str]
    token: str = field(repr=False)

    def __post_init__(self):
        _require(_identifier(self.tenant_id) and _identifier(self.subject_id))
        _require(type(self.roles) is frozenset and bool(self.roles) and self.roles <= _ROLES)
        _require(not {"operator", "worker"} <= self.roles)
        _require(isinstance(self.token, str) and _TOKEN.fullmatch(self.token) is not None)


class AuthRegistry:
    """In-memory credentials; public results contain principals and capabilities.

    Named identities can be added alongside old local developer keys. The latter
    preserve existing fixture routes, but cannot pause, resume or reconcile runs.
    """

    __slots__ = ("_records", "_principals")

    def __init__(self, tenant_keys: Mapping[str, str], identities: Sequence[LocalIdentity] = ()):
        _require(isinstance(tenant_keys, Mapping) and isinstance(identities, (list, tuple)))
        _require(len(tenant_keys) + len(identities) <= 128)
        records, tokens, subjects = [], set(), set()
        for tenant, token in tenant_keys.items():
            # Preserve the older >=16-character developer key contract. Named
            # identities use stricter identifiers and >=32 visible ASCII bytes.
            _require(isinstance(tenant, str) and bool(tenant) and len(tenant) <= 128
                     and isinstance(token, str) and re.fullmatch(r"[!-~]{16,4096}", token) is not None)
            subject = "legacy:" + tenant
            principal = Principal(tenant, subject, frozenset({"developer"}))
            _require(token not in tokens)
            tokens.add(token)
            subjects.add((tenant, subject))
            records.append((token.encode("ascii"), principal))
        for identity in identities:
            _require(type(identity) is LocalIdentity)
            # Revalidate even a frozen instance altered through trusted Python
            # reflection; never import permissions from a cached principal.
            LocalIdentity(identity.tenant_id, identity.subject_id, identity.roles, identity.token)
            subject = (identity.tenant_id, identity.subject_id)
            _require(identity.token not in tokens and subject not in subjects)
            tokens.add(identity.token)
            subjects.add(subject)
            records.append((identity.token.encode("ascii"), Principal(*subject, identity.roles)))
        _require(bool(records))
        self._records = tuple(records)
        self._principals = frozenset(principal for _, principal in records)

    def __repr__(self):
        return "<AuthRegistry credentials=redacted>"

    def authenticate(self, token: object) -> Principal | None:
        if not isinstance(token, str) or len(token) > 4096:
            return None
        try:
            supplied = token.encode("ascii")
        except UnicodeError:
            return None
        # Compare every registered key, so an early match does not reveal its
        # position. This is bounded by the local configuration's identity cap.
        match = None
        for registered, principal in self._records:
            if compare_digest(supplied, registered):
                match = principal
        return match

    def capabilities(self, principal: Principal) -> list[str]:
        if type(principal) is not Principal or principal not in self._principals:
            return []
        return sorted(set().union(*(_PERMISSIONS[role] for role in principal.roles)))

    def authorize(self, principal: Principal, action: str) -> bool:
        return isinstance(action, str) and action in self.capabilities(principal)


def load_identities(path: str | Path) -> tuple[LocalIdentity, ...]:
    """Load only a bounded private local identity file outside public outputs.

    Windows file ACLs remain the local operator's responsibility. POSIX files
    must not grant any group/other permissions. No process environment changes.
    """
    try:
        supplied = Path(path)
        resolved = supplied.resolve(strict=True)
        public_outputs = Path(__file__).resolve().parents[2].parent
        _require(not supplied.is_symlink() and resolved.is_file()
                 and not Path(os.path.abspath(supplied)).is_relative_to(public_outputs)
                 and not resolved.is_relative_to(public_outputs))
        _require(os.name == "nt" or not resolved.stat().st_mode & 0o077)
        with resolved.open("rb") as stream:
            raw = stream.read(65537)
        _require(len(raw) <= 65536)

        def pairs(items):
            result = {}
            for key, value in items:
                _require(key not in result)
                result[key] = value
            return result

        data = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=pairs)
        _require(isinstance(data, dict) and set(data) == {"schema_version", "identities"}
                 and data["schema_version"] == "loom.local-identities/v1"
                 and isinstance(data["identities"], list) and 1 <= len(data["identities"]) <= 128)
        identities = []
        for row in data["identities"]:
            _require(isinstance(row, dict) and set(row) == {"tenant_id", "subject_id", "roles", "token"}
                     and isinstance(row["roles"], list) and 1 <= len(row["roles"]) <= 3
                     and all(isinstance(role, str) for role in row["roles"])
                     and len(row["roles"]) == len(set(row["roles"])))
            identities.append(LocalIdentity(row["tenant_id"], row["subject_id"], frozenset(row["roles"]), row["token"]))
        result = tuple(identities)
        # Reject duplicates here as well as when legacy keys are merged later.
        AuthRegistry({}, result)
        return result
    except (OSError, ValueError, UnicodeError, TypeError, RecursionError):
        raise AuthConfigError() from None
