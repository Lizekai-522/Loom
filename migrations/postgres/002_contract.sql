-- Loom cross-machine contract tables (monsoon-harness plan v2.2, batch 3).
-- Apply after 001_core.sql as loom_owner on the same dedicated database.
-- Mirrors the SQLite derived tables in src/loom/contract.py: requester
-- attribution parsed at goal creation, and the append-only tenant
-- execution-domain policy. Tenant isolation is enforced by row-level security
-- ("the query itself cannot see other tenants"), not by filtering afterwards.
BEGIN;
SET LOCAL ROLE loom_owner;

CREATE TABLE loom.goal_requesters (
    tenant_id uuid NOT NULL,
    run_id uuid NOT NULL,
    requester text CHECK (requester IS NULL OR requester ~ '^[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}$'),
    source text NOT NULL CHECK (source IN ('description-prefix', 'none')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, run_id),
    FOREIGN KEY (tenant_id, run_id) REFERENCES loom.runs (tenant_id, run_id),
    CHECK ((source = 'none') = (requester IS NULL))
);
CREATE INDEX goal_requesters_by_user ON loom.goal_requesters (tenant_id, requester);

CREATE TABLE loom.tenant_execution_policy (
    tenant_id uuid NOT NULL REFERENCES loom.tenants (tenant_id),
    revision bigint NOT NULL CHECK (revision > 0),
    default_execution_domain text NOT NULL CHECK (default_execution_domain IN ('local', 'remote')),
    actor text NOT NULL CHECK (length(btrim(actor)) > 0),
    reason text NOT NULL CHECK (length(btrim(reason)) > 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, revision)
);

CREATE FUNCTION loom.reject_contract_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'contract records are append-only';
END
$$;
CREATE TRIGGER goal_requesters_append_only BEFORE UPDATE OR DELETE ON loom.goal_requesters
    FOR EACH ROW EXECUTE FUNCTION loom.reject_contract_mutation();
CREATE TRIGGER tenant_execution_policy_append_only BEFORE UPDATE OR DELETE ON loom.tenant_execution_policy
    FOR EACH ROW EXECUTE FUNCTION loom.reject_contract_mutation();

DO $contract_rls$
DECLARE
    table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY['goal_requesters', 'tenant_execution_policy'] LOOP
        EXECUTE format('ALTER TABLE loom.%I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format('ALTER TABLE loom.%I FORCE ROW LEVEL SECURITY', table_name);
        EXECUTE format('CREATE POLICY tenant_business ON loom.%I FOR ALL TO loom_app USING (loom.can_access_tenant(tenant_id, ''business'')) WITH CHECK (loom.can_access_tenant(tenant_id, ''business''))', table_name);
        EXECUTE format('CREATE POLICY tenant_audit ON loom.%I FOR SELECT TO loom_auditor USING (loom.can_access_tenant(tenant_id, ''audit''))', table_name);
    END LOOP;
END
$contract_rls$;

-- Business logins stay read-only like 001_core until the PostgreSQL writer adapter is reviewed.
GRANT SELECT ON loom.goal_requesters, loom.tenant_execution_policy TO loom_app;
GRANT SELECT ON loom.goal_requesters, loom.tenant_execution_policy TO loom_auditor;

COMMIT;
