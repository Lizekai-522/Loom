-- Loom v2.1 PostgreSQL 16+ schema candidate.
-- Applied to a disposable PostgreSQL 16.15 cluster on 2026-10-23; 14 live
-- constraints/RLS checks passed. PostgreSQL Store/production writer procedures,
-- multi-host scheduling and disaster recovery are NOT implemented/validated.
-- Read docs/postgres-integration.md before running. Apply once to an empty,
-- dedicated database. Do not apply to the current SQLite database.
--
-- DBA prerequisite: pre-create NOLOGIN, NOSUPERUSER, NOBYPASSRLS group roles
-- loom_owner, loom_app, loom_gateway, loom_auditor, loom_scope_admin.
-- The migration connection must be able to SET ROLE loom_owner; loom_owner
-- must have CREATE on this database. No passwords or runtime LOGIN roles are
-- created here. Keep runtime principals out of loom_owner/loom_scope_admin.
-- Runtime grants are SELECT-only on purpose. Production command procedures,
-- fine-grained writer grants and the PostgreSQL adapter are a later milestone.

BEGIN;

DO $roles$
DECLARE
    role_name text;
BEGIN
    FOREACH role_name IN ARRAY ARRAY[
        'loom_owner', 'loom_app', 'loom_gateway', 'loom_auditor', 'loom_scope_admin'
    ] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = role_name) THEN
            RAISE EXCEPTION 'Required pre-provisioned role is absent: %', role_name;
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles
            WHERE rolname = role_name AND (rolsuper OR rolbypassrls OR rolcanlogin)
        ) THEN
            RAISE EXCEPTION 'Role % must be NOLOGIN, NOSUPERUSER and NOBYPASSRLS', role_name;
        END IF;
    END LOOP;
END
$roles$;

SET LOCAL ROLE loom_owner;
CREATE SCHEMA loom AUTHORIZATION loom_owner;
REVOKE ALL ON SCHEMA loom FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA loom REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA loom REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;

CREATE TABLE loom.tenants (
    tenant_id uuid PRIMARY KEY,
    display_name text NOT NULL CHECK (length(btrim(display_name)) > 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- Administrative authorization data. session_user is the authenticated database
-- login; a freely settable custom GUC is deliberately not the identity source.
CREATE TABLE loom.principal_scopes (
    principal_name name NOT NULL,
    tenant_id uuid NOT NULL REFERENCES loom.tenants (tenant_id),
    access_mode text NOT NULL CHECK (access_mode IN ('business', 'gateway', 'audit')),
    expires_at timestamptz,
    granted_reason text NOT NULL CHECK (length(btrim(granted_reason)) > 0),
    PRIMARY KEY (principal_name, tenant_id, access_mode)
);

CREATE FUNCTION loom.can_access_tenant(p_tenant uuid, p_mode text)
RETURNS boolean
LANGUAGE sql STABLE SECURITY INVOKER
SET search_path = pg_catalog, loom
AS $function$
    SELECT EXISTS (
        SELECT 1 FROM loom.principal_scopes AS s
        WHERE s.principal_name = session_user
          AND s.tenant_id = p_tenant AND s.access_mode = p_mode
          AND (s.expires_at IS NULL OR s.expires_at > statement_timestamp())
    )
$function$;

CREATE TABLE loom.runs (
    tenant_id uuid NOT NULL REFERENCES loom.tenants (tenant_id),
    run_id uuid NOT NULL,
    ruleset_version text NOT NULL CHECK (length(btrim(ruleset_version)) > 0),
    anchor_digest bytea NOT NULL CHECK (octet_length(anchor_digest) = 32),
    state text NOT NULL DEFAULT 'open' CHECK (state IN (
        'open', 'succeeded', 'stalled', 'budget_exhausted', 'awaiting_human',
        'failed', 'canceled', 'accepted_with_exception'
    )),
    revision bigint NOT NULL DEFAULT 1 CHECK (revision > 0),
    last_event_seq bigint NOT NULL DEFAULT 0 CHECK (last_event_seq >= 0),
    last_event_hash bytea NOT NULL DEFAULT decode(repeat('00', 32), 'hex')
        CHECK (octet_length(last_event_hash) = 32),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, run_id)
);

CREATE TABLE loom.goals (
    tenant_id uuid NOT NULL,
    run_id uuid NOT NULL,
    goal_id uuid NOT NULL,
    title text NOT NULL CHECK (length(btrim(title)) > 0),
    acceptance jsonb NOT NULL CHECK (
        jsonb_typeof(acceptance) = 'array' AND jsonb_array_length(acceptance) > 0
    ),
    anchor_digest bytea NOT NULL CHECK (octet_length(anchor_digest) = 32),
    PRIMARY KEY (tenant_id, run_id, goal_id),
    FOREIGN KEY (tenant_id, run_id) REFERENCES loom.runs (tenant_id, run_id)
);

CREATE TABLE loom.facts (
    tenant_id uuid NOT NULL,
    run_id uuid NOT NULL,
    fact_id uuid NOT NULL,
    fact_kind text NOT NULL CHECK (fact_kind IN ('anchor', 'observation', 'artifact')),
    content_digest bytea NOT NULL CHECK (octet_length(content_digest) = 32),
    source_ref text NOT NULL CHECK (length(btrim(source_ref)) > 0),
    PRIMARY KEY (tenant_id, run_id, fact_id),
    FOREIGN KEY (tenant_id, run_id) REFERENCES loom.runs (tenant_id, run_id)
);

CREATE TABLE loom.intents (
    tenant_id uuid NOT NULL,
    run_id uuid NOT NULL,
    intent_id uuid NOT NULL,
    goal_id uuid NOT NULL,
    state text NOT NULL DEFAULT 'open' CHECK (state IN (
        'open', 'succeeded', 'stalled', 'awaiting_human', 'failed', 'canceled'
    )),
    PRIMARY KEY (tenant_id, run_id, intent_id),
    FOREIGN KEY (tenant_id, run_id, goal_id)
        REFERENCES loom.goals (tenant_id, run_id, goal_id)
);

CREATE TABLE loom.intent_facts (
    tenant_id uuid NOT NULL,
    run_id uuid NOT NULL,
    intent_id uuid NOT NULL,
    fact_id uuid NOT NULL,
    PRIMARY KEY (tenant_id, run_id, intent_id, fact_id),
    FOREIGN KEY (tenant_id, run_id, intent_id)
        REFERENCES loom.intents (tenant_id, run_id, intent_id),
    FOREIGN KEY (tenant_id, run_id, fact_id)
        REFERENCES loom.facts (tenant_id, run_id, fact_id)
);

CREATE TABLE loom.tasks (
    tenant_id uuid NOT NULL,
    run_id uuid NOT NULL,
    task_id uuid NOT NULL,
    intent_id uuid NOT NULL,
    objective_id text NOT NULL CHECK (length(btrim(objective_id)) > 0),
    input_contract jsonb NOT NULL CHECK (jsonb_typeof(input_contract) = 'object'),
    output_criteria jsonb NOT NULL CHECK (
        jsonb_typeof(output_criteria) = 'array' AND jsonb_array_length(output_criteria) > 0
    ),
    scope jsonb NOT NULL CHECK (jsonb_typeof(scope) = 'array' AND jsonb_array_length(scope) > 0),
    channel text NOT NULL CHECK (channel IN ('business', 'verification')),
    priority integer NOT NULL DEFAULT 0,
    state text NOT NULL DEFAULT 'pending' CHECK (state IN (
        'pending', 'running', 'submitted', 'verified', 'failed', 'stale',
        'rejected_by_gate', 'indeterminate', 'awaiting_human', 'canceled',
        'accepted_with_exception'
    )),
    current_epoch bigint NOT NULL DEFAULT 0 CHECK (current_epoch >= 0),
    graph_revision bigint NOT NULL CHECK (graph_revision > 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, run_id, task_id),
    FOREIGN KEY (tenant_id, run_id, intent_id)
        REFERENCES loom.intents (tenant_id, run_id, intent_id)
);
CREATE INDEX tasks_ready ON loom.tasks (tenant_id, run_id, channel, priority DESC, created_at)
    WHERE state = 'pending';

CREATE TABLE loom.task_dependencies (
    tenant_id uuid NOT NULL,
    run_id uuid NOT NULL,
    task_id uuid NOT NULL,
    prerequisite_id uuid NOT NULL,
    PRIMARY KEY (tenant_id, run_id, task_id, prerequisite_id),
    CHECK (task_id <> prerequisite_id),
    FOREIGN KEY (tenant_id, run_id, task_id)
        REFERENCES loom.tasks (tenant_id, run_id, task_id),
    FOREIGN KEY (tenant_id, run_id, prerequisite_id)
        REFERENCES loom.tasks (tenant_id, run_id, task_id)
);

CREATE TABLE loom.execution_claims (
    tenant_id uuid NOT NULL,
    run_id uuid NOT NULL,
    task_id uuid NOT NULL,
    epoch bigint NOT NULL CHECK (epoch > 0),
    worker_id text NOT NULL CHECK (length(btrim(worker_id)) > 0),
    channel text NOT NULL CHECK (channel IN ('business', 'verification')),
    state text NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'completed', 'expired', 'revoked')),
    issued_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    lease_expires_at timestamptz NOT NULL,
    heartbeat_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, run_id, task_id, epoch),
    CHECK (lease_expires_at > issued_at),
    FOREIGN KEY (tenant_id, run_id, task_id)
        REFERENCES loom.tasks (tenant_id, run_id, task_id)
);
CREATE UNIQUE INDEX one_active_claim_per_task
    ON loom.execution_claims (tenant_id, run_id, task_id) WHERE state = 'active';
CREATE INDEX claim_expiry ON loom.execution_claims (lease_expires_at) WHERE state = 'active';

CREATE FUNCTION loom.issue_claim_epoch()
RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER
SET search_path = pg_catalog, loom
AS $function$
DECLARE
    previous_epoch bigint;
    task_channel text;
    task_state text;
    run_state text;
BEGIN
    SELECT t.current_epoch, t.channel, t.state, r.state
      INTO STRICT previous_epoch, task_channel, task_state, run_state
      FROM loom.tasks AS t
      JOIN loom.runs AS r USING (tenant_id, run_id)
     WHERE t.tenant_id = NEW.tenant_id AND t.run_id = NEW.run_id AND t.task_id = NEW.task_id
     FOR UPDATE OF t, r;
    IF NEW.state <> 'active' OR task_state <> 'pending'
       OR run_state <> 'open' OR NEW.channel <> task_channel THEN
        RAISE EXCEPTION 'Cannot issue claim for this state or channel';
    END IF;
    IF EXISTS (
        SELECT 1 FROM loom.task_dependencies AS d
        JOIN loom.tasks AS p ON p.tenant_id = d.tenant_id AND p.run_id = d.run_id
            AND p.task_id = d.prerequisite_id
        WHERE d.tenant_id = NEW.tenant_id AND d.run_id = NEW.run_id
            AND d.task_id = NEW.task_id AND p.state <> 'verified'
    ) THEN
        RAISE EXCEPTION 'Task prerequisites are not verified';
    END IF;
    NEW.epoch := previous_epoch + 1;
    NEW.issued_at := clock_timestamp();
    NEW.heartbeat_at := NEW.issued_at;
    IF NEW.lease_expires_at <= NEW.issued_at
       OR NEW.lease_expires_at > NEW.issued_at + interval '1 hour' THEN
        RAISE EXCEPTION 'Claim lease must be within one database-clock hour';
    END IF;
    UPDATE loom.tasks SET current_epoch = NEW.epoch, state = 'running'
     WHERE tenant_id = NEW.tenant_id AND run_id = NEW.run_id AND task_id = NEW.task_id;
    RETURN NEW;
END
$function$;
CREATE TRIGGER claims_issue_epoch BEFORE INSERT ON loom.execution_claims
    FOR EACH ROW EXECUTE FUNCTION loom.issue_claim_epoch();

CREATE TABLE loom.artifacts (
    tenant_id uuid NOT NULL,
    run_id uuid NOT NULL,
    artifact_id uuid NOT NULL,
    task_id uuid NOT NULL,
    claim_epoch bigint NOT NULL,
    content_digest bytea NOT NULL CHECK (octet_length(content_digest) = 32),
    evidence_revision text NOT NULL CHECK (length(btrim(evidence_revision)) > 0),
    ruleset_version text NOT NULL,
    object_ref text NOT NULL CHECK (length(btrim(object_ref)) > 0),
    state text NOT NULL DEFAULT 'submitted' CHECK (state IN (
        'submitted', 'verified', 'rejected', 'disputed', 'stale', 'accepted_with_exception'
    )),
    PRIMARY KEY (tenant_id, run_id, artifact_id),
    UNIQUE (tenant_id, run_id, artifact_id, content_digest, evidence_revision, ruleset_version),
    FOREIGN KEY (tenant_id, run_id, task_id, claim_epoch)
        REFERENCES loom.execution_claims (tenant_id, run_id, task_id, epoch)
);

-- Immutable attestations. Cryptographic verification is the trusted verifier's
-- job; existence of a row or two equal conclusions alone is NOT Gate-V success.
CREATE TABLE loom.verification_attestations (
    tenant_id uuid NOT NULL,
    run_id uuid NOT NULL,
    attestation_id uuid NOT NULL,
    artifact_id uuid NOT NULL,
    content_digest bytea NOT NULL,
    evidence_revision text NOT NULL,
    ruleset_version text NOT NULL,
    verifier_id text NOT NULL CHECK (length(btrim(verifier_id)) > 0),
    model_id text NOT NULL CHECK (length(btrim(model_id)) > 0),
    decision text NOT NULL CHECK (decision IN ('pass', 'reject', 'abstain')),
    signed_payload bytea NOT NULL CHECK (octet_length(signed_payload) > 0),
    signature bytea NOT NULL CHECK (octet_length(signature) > 0),
    key_ref text NOT NULL CHECK (length(btrim(key_ref)) > 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, run_id, attestation_id),
    UNIQUE (tenant_id, run_id, artifact_id, verifier_id, content_digest, evidence_revision, ruleset_version),
    FOREIGN KEY (tenant_id, run_id, artifact_id, content_digest, evidence_revision, ruleset_version)
        REFERENCES loom.artifacts (tenant_id, run_id, artifact_id, content_digest, evidence_revision, ruleset_version)
);

CREATE TABLE loom.events (
    tenant_id uuid NOT NULL,
    run_id uuid NOT NULL,
    event_id uuid NOT NULL,
    chain_id uuid NOT NULL,
    seq bigint NOT NULL CHECK (seq > 0),
    schema_version integer NOT NULL CHECK (schema_version > 0),
    run_revision bigint NOT NULL CHECK (run_revision > 0),
    occurred_at_us bigint NOT NULL CHECK (occurred_at_us > 0),
    event_type text NOT NULL CHECK (length(btrim(event_type)) > 0),
    actor_id text NOT NULL CHECK (length(btrim(actor_id)) > 0),
    submission_key text,
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    prev_hash bytea NOT NULL CHECK (octet_length(prev_hash) = 32),
    envelope jsonb NOT NULL,
    envelope_canonical text NOT NULL,
    event_hash bytea NOT NULL CHECK (octet_length(event_hash) = 32),
    PRIMARY KEY (tenant_id, run_id, seq),
    UNIQUE (tenant_id, run_id, event_id),
    UNIQUE (tenant_id, run_id, seq, event_hash),
    UNIQUE (tenant_id, event_id),
    CHECK (chain_id = run_id),
    CHECK (envelope_canonical::jsonb = envelope),
    CHECK (event_hash = sha256(convert_to(envelope_canonical, 'UTF8'))),
    CHECK (envelope = jsonb_build_object(
        'schema_version', schema_version, 'event_id', event_id::text,
        'tenant_id', tenant_id::text, 'run_id', run_id::text, 'chain_id', chain_id::text,
        'seq', seq, 'run_revision', run_revision, 'occurred_at_us', occurred_at_us,
        'type', event_type, 'actor', actor_id, 'submission_key', submission_key,
        'payload', payload, 'prev_hash', encode(prev_hash, 'hex')
    )),
    FOREIGN KEY (tenant_id, run_id) REFERENCES loom.runs (tenant_id, run_id)
);

CREATE TABLE loom.command_receipts (
    tenant_id uuid NOT NULL,
    command_key text NOT NULL CHECK (length(command_key) BETWEEN 1 AND 200),
    run_id uuid NOT NULL,
    operation text NOT NULL CHECK (length(btrim(operation)) > 0),
    request_digest bytea NOT NULL CHECK (octet_length(request_digest) = 32),
    result jsonb NOT NULL,
    result_event_id uuid NOT NULL,
    PRIMARY KEY (tenant_id, command_key),
    FOREIGN KEY (tenant_id, run_id, result_event_id)
        REFERENCES loom.events (tenant_id, run_id, event_id)
);

CREATE TABLE loom.outbox (
    tenant_id uuid NOT NULL,
    run_id uuid NOT NULL,
    event_id uuid NOT NULL,
    state text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'publishing', 'published')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    publish_lease_until timestamptz,
    broker_ack text,
    published_at timestamptz,
    PRIMARY KEY (tenant_id, run_id, event_id),
    CHECK ((state = 'published') = (published_at IS NOT NULL AND broker_ack IS NOT NULL)),
    FOREIGN KEY (tenant_id, run_id, event_id)
        REFERENCES loom.events (tenant_id, run_id, event_id)
);
CREATE INDEX outbox_pending ON loom.outbox (state, publish_lease_until) WHERE state <> 'published';

CREATE TABLE loom.inbox (
    tenant_id uuid NOT NULL,
    run_id uuid NOT NULL,
    consumer_id text NOT NULL,
    event_id uuid NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, run_id, consumer_id, event_id),
    FOREIGN KEY (tenant_id, run_id, event_id)
        REFERENCES loom.events (tenant_id, run_id, event_id)
);

CREATE TABLE loom.budget_accounts (
    tenant_id uuid NOT NULL,
    run_id uuid NOT NULL,
    cost_limit_microusd bigint NOT NULL CHECK (cost_limit_microusd >= 0),
    token_limit bigint NOT NULL CHECK (token_limit >= 0),
    reserved_microusd bigint NOT NULL DEFAULT 0 CHECK (reserved_microusd >= 0),
    spent_microusd bigint NOT NULL DEFAULT 0 CHECK (spent_microusd >= 0),
    reserved_tokens bigint NOT NULL DEFAULT 0 CHECK (reserved_tokens >= 0),
    spent_tokens bigint NOT NULL DEFAULT 0 CHECK (spent_tokens >= 0),
    state text NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'frozen', 'overrun')),
    PRIMARY KEY (tenant_id, run_id),
    -- Preserve actual overbilling rather than rejecting its accounting record.
    CHECK (state = 'overrun' OR (
        spent_microusd + reserved_microusd <= cost_limit_microusd
        AND spent_tokens + reserved_tokens <= token_limit
    )),
    FOREIGN KEY (tenant_id, run_id) REFERENCES loom.runs (tenant_id, run_id)
);

CREATE TABLE loom.budget_reservations (
    tenant_id uuid NOT NULL,
    run_id uuid NOT NULL,
    reservation_id uuid NOT NULL,
    task_id uuid NOT NULL,
    claim_epoch bigint NOT NULL,
    request_digest bytea NOT NULL CHECK (octet_length(request_digest) = 32),
    max_cost_microusd bigint NOT NULL CHECK (max_cost_microusd >= 0),
    max_tokens bigint NOT NULL CHECK (max_tokens >= 0),
    actual_cost_microusd bigint CHECK (actual_cost_microusd >= 0),
    actual_tokens bigint CHECK (actual_tokens >= 0),
    state text NOT NULL DEFAULT 'reserved' CHECK (state IN ('reserved', 'unknown', 'settled', 'released', 'overrun')),
    PRIMARY KEY (tenant_id, run_id, reservation_id),
    FOREIGN KEY (tenant_id, run_id) REFERENCES loom.budget_accounts (tenant_id, run_id),
    FOREIGN KEY (tenant_id, run_id, task_id, claim_epoch)
        REFERENCES loom.execution_claims (tenant_id, run_id, task_id, epoch)
);

CREATE TABLE loom.budget_ledger (
    tenant_id uuid NOT NULL,
    run_id uuid NOT NULL,
    entry_id uuid NOT NULL,
    reservation_id uuid NOT NULL,
    event_id uuid NOT NULL,
    entry_kind text NOT NULL CHECK (entry_kind IN ('reserve', 'settle', 'release', 'overrun')),
    reserved_delta_microusd bigint NOT NULL,
    spent_delta_microusd bigint NOT NULL,
    reserved_delta_tokens bigint NOT NULL,
    spent_delta_tokens bigint NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, run_id, entry_id),
    FOREIGN KEY (tenant_id, run_id, reservation_id)
        REFERENCES loom.budget_reservations (tenant_id, run_id, reservation_id),
    FOREIGN KEY (tenant_id, run_id, event_id)
        REFERENCES loom.events (tenant_id, run_id, event_id)
);

CREATE TABLE loom.tool_calls (
    tenant_id uuid NOT NULL,
    run_id uuid NOT NULL,
    call_id uuid NOT NULL,
    task_id uuid NOT NULL,
    claim_epoch bigint NOT NULL,
    logical_operation_id text NOT NULL CHECK (length(btrim(logical_operation_id)) > 0),
    tool_version text NOT NULL,
    request_digest bytea NOT NULL CHECK (octet_length(request_digest) = 32),
    state text NOT NULL CHECK (state IN (
        'intent', 'dispatched', 'done', 'indeterminate', 'confirmed',
        'absent', 'partial', 'compensation_required'
    )),
    idempotent boolean NOT NULL DEFAULT false,
    supports_idempotency_key boolean NOT NULL DEFAULT false,
    provider_key text,
    provider_key_valid_until timestamptz,
    result_ref text,
    issued_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, run_id, call_id),
    -- Independent of claim_epoch: retries/new workers cannot mint a second
    -- logical operation merely by incrementing the attempt number.
    UNIQUE (tenant_id, run_id, task_id, logical_operation_id),
    CHECK (NOT supports_idempotency_key OR provider_key IS NOT NULL),
    FOREIGN KEY (tenant_id, run_id, task_id, claim_epoch)
        REFERENCES loom.execution_claims (tenant_id, run_id, task_id, epoch)
);

CREATE TABLE loom.audit_checkpoints (
    tenant_id uuid NOT NULL,
    run_id uuid NOT NULL,
    checkpoint_id uuid NOT NULL,
    last_seq bigint NOT NULL CHECK (last_seq > 0),
    head_hash bytea NOT NULL CHECK (octet_length(head_hash) = 32),
    signer_key_ref text NOT NULL CHECK (length(btrim(signer_key_ref)) > 0),
    signature bytea NOT NULL CHECK (octet_length(signature) > 0),
    external_anchor_ref text NOT NULL CHECK (length(btrim(external_anchor_ref)) > 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, run_id, checkpoint_id),
    FOREIGN KEY (tenant_id, run_id, last_seq, head_hash)
        REFERENCES loom.events (tenant_id, run_id, seq, event_hash)
);

CREATE FUNCTION loom.reject_mutation()
RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER
SET search_path = pg_catalog, loom
AS $function$
BEGIN
    RAISE EXCEPTION '% is append-only; % is forbidden', TG_TABLE_NAME, TG_OP;
END
$function$;

CREATE FUNCTION loom.protect_frozen_baseline()
RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER
SET search_path = pg_catalog, loom
AS $function$
BEGIN
    IF NEW.anchor_digest IS DISTINCT FROM OLD.anchor_digest THEN
        RAISE EXCEPTION 'Frozen Anchor changes require a new versioned Goal/run';
    END IF;
    IF TG_TABLE_NAME = 'runs' THEN
        IF NEW.ruleset_version IS DISTINCT FROM OLD.ruleset_version THEN
            RAISE EXCEPTION 'An in-flight run cannot replace its pinned ruleset';
        END IF;
    ELSIF TG_TABLE_NAME = 'goals' THEN
        IF NEW.acceptance IS DISTINCT FROM OLD.acceptance THEN
            RAISE EXCEPTION 'Frozen acceptance changes require a new versioned Goal';
        END IF;
    END IF;
    RETURN NEW;
END
$function$;
CREATE TRIGGER runs_frozen_baseline BEFORE UPDATE ON loom.runs
    FOR EACH ROW EXECUTE FUNCTION loom.protect_frozen_baseline();
CREATE TRIGGER goals_frozen_baseline BEFORE UPDATE ON loom.goals
    FOR EACH ROW EXECUTE FUNCTION loom.protect_frozen_baseline();

CREATE FUNCTION loom.enforce_event_chain()
RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER
SET search_path = pg_catalog, loom
AS $function$
DECLARE
    old_seq bigint;
    old_hash bytea;
    current_revision bigint;
BEGIN
    SELECT last_event_seq, last_event_hash, revision
      INTO STRICT old_seq, old_hash, current_revision
      FROM loom.runs
     WHERE tenant_id = NEW.tenant_id AND run_id = NEW.run_id FOR UPDATE;
    IF NEW.seq <> old_seq + 1 OR NEW.prev_hash <> old_hash
       OR NEW.run_revision <> current_revision THEN
        RAISE EXCEPTION 'Stale or noncontiguous event chain append';
    END IF;
    UPDATE loom.runs SET last_event_seq = NEW.seq, last_event_hash = NEW.event_hash
     WHERE tenant_id = NEW.tenant_id AND run_id = NEW.run_id;
    RETURN NEW;
END
$function$;
CREATE TRIGGER events_chain_order BEFORE INSERT ON loom.events
    FOR EACH ROW EXECUTE FUNCTION loom.enforce_event_chain();

CREATE FUNCTION loom.enqueue_event()
RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER
SET search_path = pg_catalog, loom
AS $function$
BEGIN
    INSERT INTO loom.outbox (tenant_id, run_id, event_id)
    VALUES (NEW.tenant_id, NEW.run_id, NEW.event_id);
    RETURN NEW;
END
$function$;
CREATE TRIGGER events_outbox AFTER INSERT ON loom.events
    FOR EACH ROW EXECUTE FUNCTION loom.enqueue_event();

DO $immutable$
DECLARE
    table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'facts', 'events', 'command_receipts', 'verification_attestations',
        'budget_ledger', 'audit_checkpoints'
    ] LOOP
        EXECUTE format('CREATE TRIGGER no_row_mutation BEFORE UPDATE OR DELETE ON loom.%I FOR EACH ROW EXECUTE FUNCTION loom.reject_mutation()', table_name);
        EXECUTE format('CREATE TRIGGER no_truncate BEFORE TRUNCATE ON loom.%I FOR EACH STATEMENT EXECUTE FUNCTION loom.reject_mutation()', table_name);
    END LOOP;
END
$immutable$;

ALTER TABLE loom.principal_scopes ENABLE ROW LEVEL SECURITY;
ALTER TABLE loom.principal_scopes FORCE ROW LEVEL SECURITY;
CREATE POLICY principal_scope_self ON loom.principal_scopes FOR SELECT
    TO loom_app, loom_gateway, loom_auditor
    USING (principal_name = session_user);
CREATE POLICY principal_scope_admin ON loom.principal_scopes FOR ALL TO loom_scope_admin
    USING (true) WITH CHECK (true);

DO $tenant_rls$
DECLARE
    table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'tenants', 'runs', 'goals', 'facts', 'intents', 'intent_facts',
        'tasks', 'task_dependencies', 'execution_claims', 'artifacts',
        'verification_attestations', 'events', 'command_receipts', 'outbox',
        'inbox', 'budget_accounts', 'budget_reservations', 'budget_ledger',
        'tool_calls', 'audit_checkpoints'
    ] LOOP
        EXECUTE format('ALTER TABLE loom.%I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format('ALTER TABLE loom.%I FORCE ROW LEVEL SECURITY', table_name);
        EXECUTE format('CREATE POLICY tenant_business ON loom.%I FOR ALL TO loom_app USING (loom.can_access_tenant(tenant_id, ''business'')) WITH CHECK (loom.can_access_tenant(tenant_id, ''business''))', table_name);
        EXECUTE format('CREATE POLICY tenant_gateway ON loom.%I FOR ALL TO loom_gateway USING (loom.can_access_tenant(tenant_id, ''gateway'')) WITH CHECK (loom.can_access_tenant(tenant_id, ''gateway''))', table_name);
        EXECUTE format('CREATE POLICY tenant_audit ON loom.%I FOR SELECT TO loom_auditor USING (loom.can_access_tenant(tenant_id, ''audit''))', table_name);
    END LOOP;
END
$tenant_rls$;
CREATE POLICY tenant_metadata_admin ON loom.tenants FOR ALL TO loom_scope_admin
    USING (true) WITH CHECK (true);

GRANT USAGE ON SCHEMA loom TO loom_app, loom_gateway, loom_auditor, loom_scope_admin;
GRANT SELECT ON ALL TABLES IN SCHEMA loom TO loom_app, loom_gateway, loom_auditor;
GRANT SELECT, INSERT, UPDATE, DELETE ON loom.principal_scopes TO loom_scope_admin;
GRANT SELECT, INSERT, UPDATE ON loom.tenants TO loom_scope_admin;
GRANT EXECUTE ON FUNCTION loom.can_access_tenant(uuid, text)
    TO loom_app, loom_gateway, loom_auditor;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER
    ON ALL TABLES IN SCHEMA loom FROM loom_app, loom_gateway, loom_auditor;
REVOKE EXECUTE ON FUNCTION loom.reject_mutation(), loom.enforce_event_chain(),
    loom.enqueue_event(), loom.issue_claim_epoch(), loom.protect_frozen_baseline() FROM PUBLIC;

COMMENT ON SCHEMA loom IS
    'Loom v2.1 candidate PostgreSQL schema; adapter and production write procedures are not implemented.';
COMMENT ON TABLE loom.principal_scopes IS
    'DBA-administered session_user-to-tenant scopes; runtime/audit roles cannot edit authorization.';
COMMENT ON TABLE loom.events IS
    'Exact UTF-8 envelope bytes and typed metadata are hash-bound; external checkpoint verification remains required.';
COMMENT ON TABLE loom.execution_claims IS
    'Only one active row per task; expired leases must be atomically revoked before replacement; gateway rechecks database time and epoch.';
COMMENT ON TABLE loom.budget_accounts IS
    'Future command procedures must lock before reserving; a table constraint alone does not reconcile ledger entries.';

COMMIT;
