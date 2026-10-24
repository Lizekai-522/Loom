"""Local identity/RBAC boundaries with generated fixture keys only; no network."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
import json
import os
from pathlib import Path
import tempfile
import traceback

import pytest

from loom.auth import AuthConfigError, AuthRegistry, LocalIdentity, Principal, load_identities


OPERATOR_KEY = "fixture-operator-credential-not-a-real-key"
WORKER_KEY = "fixture-worker-credential-not-a-real-key"
VIEWER_KEY = "fixture-viewer-credential-not-a-real-key"
LEGACY_KEY = "fixture-legacy-credential-only"


def identity(role="operator", *, tenant="a", subject=None, token=None):
    return LocalIdentity(tenant, subject or role, frozenset({role}), token or {
        "operator": OPERATOR_KEY, "worker": WORKER_KEY, "viewer": VIEWER_KEY
    }[role])


def document(*identities):
    rows = identities or (identity(),)
    return {"schema_version": "loom.local-identities/v1", "identities": [
        {"tenant_id": row.tenant_id, "subject_id": row.subject_id,
         "roles": sorted(row.roles), "token": row.token} for row in rows
    ]}


def private_file(directory, data):
    path = Path(directory) / "identities.json"
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    path.chmod(0o600)
    return path


@pytest.mark.parametrize("role,allowed", [
    ("operator", {"read", "goal.write", "plan.approve", "control", "suite.approve"}),
    ("worker", {"read", "task.claim", "task.submit"}),
    ("viewer", {"read"}),
])
def test_named_roles_enforce_exact_capabilities(role, allowed):
    registered = identity(role)
    registry = AuthRegistry({}, (registered,))
    principal = registry.authenticate(registered.token)
    assert principal == Principal("a", role, frozenset({role}))
    assert registry.capabilities(principal) == sorted(allowed)
    for action in {"read", "goal.write", "plan.approve", "control", "suite.approve", "task.claim", "task.submit", "arbitrary"}:
        assert registry.authorize(principal, action) is (action in allowed)


def test_legacy_routes_remain_compatible_without_control_rights():
    registry = AuthRegistry({"a": LEGACY_KEY}, (identity(), identity("worker")))
    legacy = registry.authenticate(LEGACY_KEY)
    assert legacy.roles == frozenset({"developer"})
    assert registry.capabilities(legacy) == ["goal.write", "plan.approve", "read", "task.claim", "task.submit"]
    assert not registry.authorize(legacy, "control")
    assert not registry.authorize(legacy, "suite.approve")
    assert not registry.authorize(registry.authenticate(WORKER_KEY), "goal.write")
    assert not registry.authorize(registry.authenticate(OPERATOR_KEY), "task.submit")


def test_principals_cannot_raise_their_roles_or_change_tenant():
    registry = AuthRegistry({}, (identity("worker"),))
    for forged in (Principal("a", "worker", frozenset({"operator"})),
                   Principal("b", "worker", frozenset({"worker"})),
                   Principal("a", "other", frozenset({"worker"})), None, {}):
        assert registry.capabilities(forged) == []
        assert not registry.authorize(forged, "read")


@pytest.mark.parametrize("token", [None, b"fixture", 1, "", "bogus", "秘密", "x" * 4097, OPERATOR_KEY + " "])
def test_invalid_bearers_do_not_authenticate(token):
    assert AuthRegistry({}, (identity(),)).authenticate(token) is None


def test_token_values_are_absent_from_public_repr_and_principals_are_immutable():
    value = identity()
    registry = AuthRegistry({}, (value,))
    principal = registry.authenticate(value.token)
    assert value.token not in repr(value) + repr(registry) + repr(principal)
    assert "token" not in repr(value)
    assert not hasattr(principal, "token")
    with pytest.raises(FrozenInstanceError):
        principal.roles = frozenset({"worker"})
    with pytest.raises(FrozenInstanceError):
        value.token = WORKER_KEY


@pytest.mark.parametrize("roles", [frozenset({"operator", "worker"}), frozenset({"developer"}),
                                  frozenset({"arbitrary"}), frozenset(), {"operator"}])
def test_named_identities_cannot_register_mixed_or_legacy_roles(roles):
    with pytest.raises(AuthConfigError, match="^LOCAL_AUTH_CONFIG_INVALID$"):
        LocalIdentity("a", "operator", roles, OPERATOR_KEY)


@pytest.mark.parametrize("token", ["short", "x" * 31, "x" * 4097, "fixture has spaces" * 3,
                                  "credential-with-newline\n" * 3, "密" * 32, None])
def test_named_token_constraints_fail_with_static_diagnostics(token):
    with pytest.raises(AuthConfigError, match="^LOCAL_AUTH_CONFIG_INVALID$") as caught:
        LocalIdentity("a", "operator", frozenset({"operator"}), token)
    assert str(caught.value) == "LOCAL_AUTH_CONFIG_INVALID"


@pytest.mark.parametrize("tenant,subject", [("", "operator"), ("a", ""), ("a/b", "operator"),
                                            ("a", "operator\nrole"), ("a", "x" * 129)])
def test_named_identifier_constraints(tenant, subject):
    with pytest.raises(AuthConfigError):
        LocalIdentity(tenant, subject, frozenset({"operator"}), OPERATOR_KEY)


def test_tokens_and_tenant_subject_pairs_cannot_be_duplicated():
    cases = [
        ({}, (identity(), identity("viewer", token=OPERATOR_KEY))),
        ({"a": OPERATOR_KEY}, (identity(),)),
        ({"a": LEGACY_KEY, "b": LEGACY_KEY}, ()),
        ({}, (identity(), identity("viewer", subject="operator"))),
        ({"a": LEGACY_KEY}, (identity(subject="legacy:a"),)),
    ]
    for legacy, named in cases:
        with pytest.raises(AuthConfigError, match="^LOCAL_AUTH_CONFIG_INVALID$"):
            AuthRegistry(legacy, named)


def test_same_subject_in_different_tenants_is_distinct():
    registry = AuthRegistry({}, (identity(), identity(tenant="b", token=WORKER_KEY)))
    assert registry.authenticate(OPERATOR_KEY).tenant_id == "a"
    assert registry.authenticate(WORKER_KEY).tenant_id == "b"


def test_registry_revalidates_reflectively_mutated_identity():
    changed = identity()
    object.__setattr__(changed, "roles", frozenset({"operator", "worker"}))
    with pytest.raises(AuthConfigError):
        AuthRegistry({}, (changed,))


def test_registry_is_bounded_and_cannot_be_empty():
    with pytest.raises(AuthConfigError):
        AuthRegistry({}, ())
    with pytest.raises(AuthConfigError):
        AuthRegistry({str(n): f"fixture-{n:04d}-legacy-credential-only" for n in range(129)})


def test_readonly_authentication_is_consistent_under_concurrent_requests():
    registry = AuthRegistry({}, (identity(), identity("worker")))
    with ThreadPoolExecutor(max_workers=4) as pool:
        principals = list(pool.map(registry.authenticate, [OPERATOR_KEY, WORKER_KEY] * 30))
    assert [value.subject_id for value in principals] == ["operator", "worker"] * 30


def test_load_preserves_environment_and_returns_frozen_identity_tuple():
    with tempfile.TemporaryDirectory() as tmp:
        path = private_file(tmp, document(identity(), identity("worker")))
        before = dict(os.environ)
        loaded = load_identities(path)
        assert isinstance(loaded, tuple) and len(loaded) == 2
        assert loaded[0] == identity()
        assert dict(os.environ) == before


def test_relative_private_path_can_leave_public_project_with_parent_segments(tmp_path, monkeypatch):
    import loom.auth as auth
    public = tmp_path / "outputs"
    project = public / "loom"
    source = project / "src" / "loom" / "auth.py"
    source.parent.mkdir(parents=True)
    private = tmp_path / "work"
    private.mkdir()
    path = private_file(private, document())
    monkeypatch.setattr(auth, "__file__", str(source))
    monkeypatch.chdir(project)
    relative = Path("..") / ".." / "work" / path.name
    assert load_identities(relative) == (identity(),)


@pytest.mark.parametrize("data", [
    {}, [], {"schema_version": "wrong", "identities": []},
    {"schema_version": "loom.local-identities/v1", "identities": []},
    {**document(), "extra": "forbidden"},
    {"schema_version": "loom.local-identities/v1", "identities": [{"tenant_id": "a"}]},
    {"schema_version": "loom.local-identities/v1", "identities": [{**document()["identities"][0], "roles": "operator"}]},
    {"schema_version": "loom.local-identities/v1", "identities": [{**document()["identities"][0], "roles": ["operator", "operator"]}]},
    {"schema_version": "loom.local-identities/v1", "identities": [{**document()["identities"][0], "roles": ["operator", "worker"]}]},
    {"schema_version": "loom.local-identities/v1", "identities": [{**document()["identities"][0], "token": None}]},
    '{"schema_version":"loom.local-identities/v1","schema_version":"duplicate","identities":[]}',
    '{"schema_version":"loom.local-identities/v1","identities":[{"tenant_id":"a","subject_id":"s","roles":["viewer"],"token":"first","token":"second"}]}',
    '{"broken": "' + OPERATOR_KEY,
])
def test_malformed_private_configs_have_static_non_secret_exceptions(data):
    with tempfile.TemporaryDirectory() as tmp:
        path = private_file(tmp, data)
        with pytest.raises(AuthConfigError) as caught:
            load_identities(path)
        rendered = "".join(traceback.format_exception_only(caught.type, caught.value))
        assert str(caught.value) == "LOCAL_AUTH_CONFIG_INVALID"
        assert OPERATOR_KEY not in rendered and str(path) not in rendered


def test_load_rejects_duplicate_identity_rows():
    with tempfile.TemporaryDirectory() as tmp:
        for data in (document(identity(), identity()), document(identity(), identity("viewer", token=OPERATOR_KEY))):
            with pytest.raises(AuthConfigError):
                load_identities(private_file(tmp, data))


def test_oversized_missing_directory_and_public_paths_are_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        path = private_file(tmp, " " * 65537)
        for target in (path, Path(tmp) / "missing", Path(tmp), Path(__file__).resolve()):
            with pytest.raises(AuthConfigError):
                load_identities(target)


def test_private_file_symlink_is_rejected_when_platform_permits_it():
    with tempfile.TemporaryDirectory() as tmp:
        target = private_file(tmp, document())
        link = Path(tmp) / "linked.json"
        try:
            link.symlink_to(target)
        except OSError:
            # Windows may deny symlink creation without developer mode; the
            # public-path/missing/size cases still exercise the loader boundary.
            return
        with pytest.raises(AuthConfigError):
            load_identities(link)


def test_posix_group_access_is_rejected_without_changing_permissions():
    if os.name == "nt":
        return
    with tempfile.TemporaryDirectory() as tmp:
        path = private_file(tmp, document())
        path.chmod(0o640)
        with pytest.raises(AuthConfigError):
            load_identities(path)
        assert path.stat().st_mode & 0o777 == 0o640
