"""Real PostgreSQL constraints/RLS tests, NOT a PostgreSQL Store adapter test.

No configuration means an explicit skip. Once LOOM_POSTGRES_TEST_CONFIG is set,
missing drivers, invalid configuration, an unavailable server, migration errors,
or test failures are errors, never converted into a successful skip.

The configured database's loom schema is reset. Only an explicitly disposable,
loopback cluster with a matching data directory and loom_test_* database is
accepted. Credentials remain in a work-directory JSON file, not this source.
"""

from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from threading import Barrier
import unittest
from uuid import UUID, uuid4


CONFIG_ENV = "LOOM_POSTGRES_TEST_CONFIG"
PROJECT = Path(__file__).resolve().parents[2]
TENANT_A = UUID("11111111-1111-4111-8111-111111111111")
TENANT_B = UUID("22222222-2222-4222-8222-222222222222")
RUN_A = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
RUN_B = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
GOAL_A = UUID("a1111111-1111-4111-8111-111111111111")
GOAL_B = UUID("b1111111-1111-4111-8111-111111111111")
INTENT_A = UUID("a2222222-2222-4222-8222-222222222222")
INTENT_B = UUID("b2222222-2222-4222-8222-222222222222")


def load_config():
    configured = os.environ.get(CONFIG_ENV)
    if not configured:
        raise RuntimeError("explicit PostgreSQL test configuration is required")
    path = Path(configured)
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("disposable_cluster") is not True:
        raise RuntimeError("PostgreSQL tests require an explicitly disposable cluster")
    if data.get("host") not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("PostgreSQL live tests are restricted to loopback")
    if not str(data.get("database", "")).startswith("loom_test_"):
        raise RuntimeError("refusing schema reset outside a loom_test_* database")
    if not data.get("data_directory") or not data.get("admin_password"):
        raise RuntimeError("test cluster identity and private credentials are required")
    return data


def connect_options(config, *, user=None, database=None):
    account = config["test_users"][user] if user else {
        "user": config["admin_user"], "password": config["admin_password"]
    }
    return {
        "host": config["host"], "port": config["port"],
        "dbname": database or config["database"],
        "user": account["user"], "password": account["password"],
        "connect_timeout": 5, "application_name": "loom-disposable-integration-test",
    }


class PostgreSQLConnectionFailure(RuntimeError):
    """Safe diagnostics only: never retain the driver's exception or conninfo."""

    def __init__(self, category, sqlstate=None, *, credential_setup=False):
        self.driver_category, self.sqlstate = category, sqlstate
        prefix = "POSTGRES_TEST_CREDENTIAL_SETUP_FAILED" if credential_setup else "POSTGRES_TEST_CONNECT_FAILED"
        message = prefix + ": " + category
        if sqlstate is not None:
            message += " sqlstate=" + sqlstate
        super().__init__(message)


def _safe_failure_details(failure):
    __tracebackhide__ = True
    name = type(failure).__name__
    category = name if name in {
        "ConnectionTimeout", "OperationalError", "InterfaceError", "ProgrammingError",
        "TypeError", "ValueError", "KeyError", "OSError",
    } else "DriverError"
    state = getattr(failure, "sqlstate", None)
    return category, state if isinstance(state, str) and re.fullmatch(r"[0-9A-Z]{5}", state) else None


def connect_private(driver, config, *, user=None, database=None, autocommit=False):
    # A psycopg connection traceback includes the expanded password/conninfo.
    # Hide this frame even when pytest is invoked directly with --showlocals.
    __tracebackhide__ = True
    try:
        return driver.connect(**connect_options(config, user=user, database=database), autocommit=autocommit)
    except Exception as failure:
        category, sqlstate = _safe_failure_details(failure)
    # Raise outside the except block. The public exception has no chained
    # context/traceback pointing to psycopg's private keyword arguments.
    raise PostgreSQLConnectionFailure(category, sqlstate) from None


def set_private_role_password(admin, sql, account):
    # ALTER ROLE requires a literal. A failed execute must not expose the
    # composed SQL containing that literal in a driver traceback either.
    __tracebackhide__ = True
    try:
        admin.execute(sql.SQL("ALTER ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD {}").format(
            sql.Identifier(account["user"]), sql.Literal(account["password"])))
        return
    except Exception as failure:
        category, sqlstate = _safe_failure_details(failure)
    raise PostgreSQLConnectionFailure(category, sqlstate, credential_setup=True) from None


@unittest.skipUnless(os.environ.get(CONFIG_ENV), f"set {CONFIG_ENV} for a disposable PostgreSQL cluster")
class PostgreSQLLiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import psycopg
            from psycopg import sql
            from psycopg.types.json import Jsonb
        except ImportError as exc:
            raise RuntimeError("configured PostgreSQL integration requires psycopg[binary]") from exc
        cls.pg, cls.sql, cls.Jsonb = psycopg, sql, Jsonb
        cls.config = load_config()
        with connect_private(psycopg, cls.config, database="postgres", autocommit=True) as admin:
            actual_data = admin.execute("SELECT current_setting('data_directory')").fetchone()[0]
            if Path(actual_data).resolve() != Path(cls.config["data_directory"]).resolve():
                raise RuntimeError("connected cluster data directory does not match disposable configuration")
            if int(admin.execute("SHOW server_version_num").fetchone()[0]) < 160000:
                raise RuntimeError("PostgreSQL 16 or newer is required")
            if cls.config.get("cluster_system_identifier"):
                actual_id = str(admin.execute("SELECT system_identifier FROM pg_control_system()").fetchone()[0])
                if actual_id != str(cls.config["cluster_system_identifier"]):
                    raise RuntimeError("disposable cluster system identifier mismatch")
            for role in ("loom_owner", "loom_app", "loom_gateway", "loom_auditor", "loom_scope_admin"):
                if not admin.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone():
                    admin.execute(sql.SQL("CREATE ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS").format(sql.Identifier(role)))
            if not admin.execute("SELECT 1 FROM pg_database WHERE datname=%s", (cls.config["database"],)).fetchone():
                admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(cls.config["database"])))
            admin.execute(sql.SQL("GRANT CREATE ON DATABASE {} TO loom_owner").format(sql.Identifier(cls.config["database"])))

        # Serialize destructive fixture setup across separate check processes.
        cls.fixture_lock = connect_private(psycopg, cls.config, autocommit=True)
        cls.addClassCleanup(cls.fixture_lock.close)
        cls.fixture_lock.execute("SET statement_timeout='30s'")
        cls.fixture_lock.execute("SELECT pg_advisory_lock(16001520261023)")

        # Destructive only within the checked fresh loopback test database.
        with connect_private(psycopg, cls.config, autocommit=True) as admin:
            admin.execute("DROP SCHEMA IF EXISTS loom CASCADE")
            migration = (PROJECT / "migrations/postgres/001_core.sql").read_text(encoding="utf-8")
            admin.execute(migration, prepare=False)
            for role_name, group in (("loom_test_app_a", "loom_app"), ("loom_test_app_b", "loom_app"), ("loom_test_audit_a", "loom_auditor")):
                account = cls.config["test_users"][role_name]
                if not admin.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (account["user"],)).fetchone():
                    admin.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS").format(sql.Identifier(account["user"])))
                set_private_role_password(admin, sql, account)
                admin.execute(sql.SQL("REVOKE loom_owner, loom_app, loom_gateway, loom_auditor, loom_scope_admin FROM {}").format(sql.Identifier(account["user"])))
                admin.execute(sql.SQL("GRANT {} TO {}").format(sql.Identifier(group), sql.Identifier(account["user"])))
                admin.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(cls.config["database"]), sql.Identifier(account["user"])))
            cls._seed(admin)

    @classmethod
    def _seed(cls, admin):
        for tenant, run, goal, intent, label in ((TENANT_A, RUN_A, GOAL_A, INTENT_A, "A"), (TENANT_B, RUN_B, GOAL_B, INTENT_B, "B")):
            anchor = sha256(("anchor-" + label).encode()).digest()
            fact = uuid4()
            admin.execute("INSERT INTO loom.tenants(tenant_id,display_name) VALUES (%s,%s)", (tenant, "Tenant " + label))
            admin.execute("INSERT INTO loom.runs(tenant_id,run_id,ruleset_version,anchor_digest) VALUES (%s,%s,'kernel-v1',%s)", (tenant, run, anchor))
            admin.execute("INSERT INTO loom.goals(tenant_id,run_id,goal_id,title,acceptance,anchor_digest) VALUES (%s,%s,%s,%s,%s,%s)", (tenant, run, goal, "Goal " + label, cls.Jsonb(["tests.pass"]), anchor))
            admin.execute("INSERT INTO loom.facts(tenant_id,run_id,fact_id,fact_kind,content_digest,source_ref) VALUES (%s,%s,%s,'anchor',%s,%s)", (tenant, run, fact, anchor, "anchor:" + label))
            admin.execute("INSERT INTO loom.intents(tenant_id,run_id,intent_id,goal_id) VALUES (%s,%s,%s,%s)", (tenant, run, intent, goal))
            admin.execute("INSERT INTO loom.intent_facts VALUES (%s,%s,%s,%s)", (tenant, run, intent, fact))
        for name, tenant, mode in (("loom_test_app_a", TENANT_A, "business"), ("loom_test_app_b", TENANT_B, "business"), ("loom_test_audit_a", TENANT_A, "audit")):
            admin.execute("INSERT INTO loom.principal_scopes(principal_name,tenant_id,access_mode,granted_reason) VALUES (%s,%s,%s,'disposable synthetic integration fixture')", (cls.config["test_users"][name]["user"], tenant, mode))

    def setUp(self):
        self.admin = connect_private(self.pg, self.config)
        self.admin.execute("BEGIN")

    def tearDown(self):
        self.admin.rollback()
        self.admin.close()

    def expect_database_error(self, sqlstate, operation):
        with self.assertRaises(self.pg.Error) as caught:
            with self.admin.transaction():
                operation()
        self.assertEqual(caught.exception.sqlstate, sqlstate)

    def new_task(self, *, task_id=None, channel="business", connection=None):
        db = connection or self.admin
        task_id = task_id or uuid4()
        db.execute("""INSERT INTO loom.tasks(tenant_id,run_id,task_id,intent_id,objective_id,
            input_contract,output_criteria,scope,channel,graph_revision)
            VALUES (%s,%s,%s,%s,'one-objective',%s,%s,%s,%s,1)""",
            (TENANT_A, RUN_A, task_id, INTENT_A, self.Jsonb({}), self.Jsonb(["tests.pass"]), self.Jsonb(["local-fixture"]), channel))
        return task_id

    def issue_claim(self, task_id, *, worker="worker-a", connection=None):
        db = connection or self.admin
        return db.execute("""INSERT INTO loom.execution_claims(tenant_id,run_id,task_id,epoch,
            worker_id,channel,lease_expires_at) VALUES (%s,%s,%s,0,%s,'business',
            clock_timestamp()+interval '30 seconds') RETURNING epoch,issued_at,lease_expires_at""",
            (TENANT_A, RUN_A, task_id, worker)).fetchone()

    def append_event(self, *, typed_changes=None, hash_override=None):
        seq, prior, revision = self.admin.execute("SELECT last_event_seq,last_event_hash,revision FROM loom.runs WHERE tenant_id=%s AND run_id=%s", (TENANT_A, RUN_A)).fetchone()
        event_id = uuid4()
        occurred = self.admin.execute("SELECT (extract(epoch FROM clock_timestamp())*1000000)::bigint").fetchone()[0]
        body = {"schema_version": 1, "event_id": str(event_id), "tenant_id": str(TENANT_A),
                "run_id": str(RUN_A), "chain_id": str(RUN_A), "seq": seq + 1,
                "run_revision": revision, "occurred_at_us": occurred, "type": "test.recorded",
                "actor": "integration:trusted", "submission_key": None,
                "payload": {"synthetic": True}, "prev_hash": bytes(prior).hex()}
        encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        values = {"tenant_id": TENANT_A, "run_id": RUN_A, "event_id": event_id, "chain_id": RUN_A,
                  "seq": seq + 1, "schema_version": 1, "run_revision": revision,
                  "occurred_at_us": occurred, "event_type": body["type"], "actor_id": body["actor"],
                  "submission_key": None, "payload": self.Jsonb(body["payload"]), "prev_hash": bytes(prior),
                  "envelope": self.Jsonb(body), "envelope_canonical": encoded,
                  "event_hash": hash_override or sha256(encoded.encode()).digest()}
        values.update(typed_changes or {})
        columns = tuple(values)
        statement = self.sql.SQL("INSERT INTO loom.events ({}) VALUES ({})").format(
            self.sql.SQL(",").join(map(self.sql.Identifier, columns)),
            self.sql.SQL(",").join(self.sql.Placeholder() for _ in columns))
        self.admin.execute(statement, tuple(values[column] for column in columns))
        return event_id

    def test_real_server_version_and_all_tenant_tables_force_rls(self):
        self.assertGreaterEqual(int(self.admin.execute("SHOW server_version_num").fetchone()[0]), 160000)
        tables = self.admin.execute("""SELECT c.relname,c.relrowsecurity,c.relforcerowsecurity
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='loom' AND c.relkind='r'""").fetchall()
        self.assertEqual(len(tables), 21)
        self.assertTrue(all(rls and forced for _, rls, forced in tables))

    def test_two_tenant_business_logins_see_only_their_own_rows(self):
        for user, expected in (("loom_test_app_a", TENANT_A), ("loom_test_app_b", TENANT_B)):
            with self.subTest(user=user), connect_private(self.pg, self.config, user=user) as app:
                self.assertEqual(app.execute("SELECT tenant_id FROM loom.runs").fetchall(), [(expected,)])
                app.execute("SELECT set_config('loom.tenant_id',%s,false)", (str(TENANT_B if expected == TENANT_A else TENANT_A),))
                self.assertEqual(app.execute("SELECT tenant_id FROM loom.runs").fetchall(), [(expected,)])

    def test_auditor_has_explicit_scope_without_bypassrls_or_owner(self):
        user = "loom_test_audit_a"
        with connect_private(self.pg, self.config, user=user) as audit:
            self.assertEqual(audit.execute("SELECT tenant_id FROM loom.runs").fetchall(), [(TENANT_A,)])
            flags = audit.execute("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=session_user").fetchone()
            self.assertEqual(flags, (False, False))
            self.assertFalse(audit.execute("SELECT pg_has_role(session_user,'loom_owner','MEMBER')").fetchone()[0])
            with self.assertRaises(self.pg.errors.InsufficientPrivilege):
                audit.execute("UPDATE loom.principal_scopes SET access_mode='business'")

    def test_business_role_is_read_only_until_command_adapter_is_reviewed(self):
        with connect_private(self.pg, self.config, user="loom_test_app_a") as app:
            with self.assertRaises(self.pg.errors.InsufficientPrivilege):
                app.execute("UPDATE loom.runs SET state='succeeded'")

    def test_composite_foreign_key_rejects_cross_tenant_run_references(self):
        self.expect_database_error("23503", lambda: self.admin.execute(
            "INSERT INTO loom.intents(tenant_id,run_id,intent_id,goal_id) VALUES (%s,%s,%s,%s)",
            (TENANT_A, RUN_A, uuid4(), GOAL_B)))

    def test_frozen_anchor_ruleset_and_acceptance_cannot_be_replaced(self):
        for statement, parameters in (
            ("UPDATE loom.runs SET anchor_digest=%s WHERE tenant_id=%s AND run_id=%s", (b"x" * 32, TENANT_A, RUN_A)),
            ("UPDATE loom.runs SET ruleset_version='weaker' WHERE tenant_id=%s AND run_id=%s", (TENANT_A, RUN_A)),
            ("UPDATE loom.goals SET acceptance=%s WHERE tenant_id=%s AND run_id=%s", (self.Jsonb(["always.true"]), TENANT_A, RUN_A)),
        ):
            with self.subTest(statement=statement):
                self.expect_database_error("P0001", lambda: self.admin.execute(statement, parameters))

    def test_event_append_is_hash_bound_and_creates_same_transaction_outbox(self):
        event_id = self.append_event()
        self.assertEqual(self.admin.execute("SELECT count(*) FROM loom.outbox WHERE tenant_id=%s AND run_id=%s AND event_id=%s", (TENANT_A, RUN_A, event_id)).fetchone()[0], 1)
        head = self.admin.execute("SELECT last_event_seq,last_event_hash FROM loom.runs WHERE tenant_id=%s AND run_id=%s", (TENANT_A, RUN_A)).fetchone()
        stored = self.admin.execute("SELECT seq,event_hash FROM loom.events WHERE tenant_id=%s AND run_id=%s AND event_id=%s", (TENANT_A, RUN_A, event_id)).fetchone()
        self.assertEqual(head, stored)

    def test_wrong_actor_or_hash_rejected_and_failed_append_does_not_advance_head(self):
        prior = self.admin.execute("SELECT last_event_seq,last_event_hash FROM loom.runs WHERE tenant_id=%s AND run_id=%s", (TENANT_A, RUN_A)).fetchone()
        for changed in (
            {"actor_id": "tampered:actor"}, {"event_type": "gate.passed"},
            {"chain_id": uuid4()}, {"schema_version": 2},
            {"submission_key": "replaced-key"}, {"payload": self.Jsonb({"synthetic": False})},
        ):
            with self.subTest(changed_field=next(iter(changed))):
                self.expect_database_error("23514", lambda: self.append_event(typed_changes=changed))
        self.expect_database_error("23514", lambda: self.append_event(hash_override=b"x" * 32))
        current = self.admin.execute("SELECT last_event_seq,last_event_hash FROM loom.runs WHERE tenant_id=%s AND run_id=%s", (TENANT_A, RUN_A)).fetchone()
        self.assertEqual(prior, current)

    def test_append_only_blocks_update_delete_and_truncate_even_for_test_admin(self):
        self.append_event()
        for statement in ("UPDATE loom.events SET actor_id='changed'", "DELETE FROM loom.events", "TRUNCATE loom.events CASCADE"):
            with self.subTest(statement=statement):
                self.expect_database_error("P0001", lambda: self.admin.execute(statement))

    def test_claim_epoch_is_database_issued_and_old_epoch_fails_eligibility(self):
        task = self.new_task()
        first = self.issue_claim(task)
        self.assertEqual(first[0], 1)
        self.assertGreater(first[2], first[1])
        self.expect_database_error("P0001", lambda: self.issue_claim(task, worker="competing-worker"))
        # Even an erroneous pending projection cannot bypass the active index.
        self.admin.execute("UPDATE loom.tasks SET state='pending' WHERE tenant_id=%s AND run_id=%s AND task_id=%s", (TENANT_A, RUN_A, task))
        self.expect_database_error("23505", lambda: self.issue_claim(task, worker="competing-worker"))
        self.assertEqual(self.admin.execute("SELECT current_epoch FROM loom.tasks WHERE tenant_id=%s AND run_id=%s AND task_id=%s", (TENANT_A, RUN_A, task)).fetchone()[0], 1)
        self.admin.execute("UPDATE loom.execution_claims SET state='expired' WHERE tenant_id=%s AND run_id=%s AND task_id=%s", (TENANT_A, RUN_A, task))
        self.admin.execute("UPDATE loom.tasks SET state='pending' WHERE tenant_id=%s AND run_id=%s AND task_id=%s", (TENANT_A, RUN_A, task))
        self.assertEqual(self.issue_claim(task, worker="replacement")[0], 2)
        active = self.admin.execute("""SELECT c.epoch FROM loom.execution_claims c
            JOIN loom.tasks t USING(tenant_id,run_id,task_id)
            WHERE c.tenant_id=%s AND c.run_id=%s AND c.task_id=%s
              AND c.state='active' AND c.epoch=t.current_epoch
              AND t.state='running'
              AND c.lease_expires_at>clock_timestamp()""", (TENANT_A, RUN_A, task)).fetchall()
        self.assertEqual(active, [(2,)])

    def test_unverified_prerequisite_and_paused_run_cannot_receive_claim(self):
        parent, child = self.new_task(), self.new_task()
        self.admin.execute("INSERT INTO loom.task_dependencies VALUES (%s,%s,%s,%s)", (TENANT_A, RUN_A, child, parent))
        self.expect_database_error("P0001", lambda: self.issue_claim(child))
        self.admin.execute("UPDATE loom.runs SET state='awaiting_human' WHERE tenant_id=%s AND run_id=%s", (TENANT_A, RUN_A))
        self.expect_database_error("P0001", lambda: self.issue_claim(parent))

    def test_budget_constraints_reject_overreservation_but_preserve_explicit_overrun(self):
        self.admin.execute("INSERT INTO loom.budget_accounts(tenant_id,run_id,cost_limit_microusd,token_limit) VALUES (%s,%s,100,100)", (TENANT_A, RUN_A))
        self.expect_database_error("23514", lambda: self.admin.execute("UPDATE loom.budget_accounts SET reserved_microusd=101 WHERE tenant_id=%s AND run_id=%s", (TENANT_A, RUN_A)))
        self.admin.execute("UPDATE loom.budget_accounts SET state='overrun',spent_microusd=101 WHERE tenant_id=%s AND run_id=%s", (TENANT_A, RUN_A))
        self.assertEqual(self.admin.execute("SELECT state,spent_microusd FROM loom.budget_accounts WHERE tenant_id=%s AND run_id=%s", (TENANT_A, RUN_A)).fetchone(), ("overrun", 101))

    def test_logical_tool_operation_is_unique_across_attempts(self):
        task = self.new_task()
        self.issue_claim(task)
        parameters = (TENANT_A, RUN_A, uuid4(), task, 1, "stable-logical-write", b"r" * 32)
        statement = """INSERT INTO loom.tool_calls(tenant_id,run_id,call_id,task_id,claim_epoch,
            logical_operation_id,tool_version,request_digest,state)
            VALUES (%s,%s,%s,%s,%s,%s,'fixture-v1',%s,'intent')"""
        self.admin.execute(statement, parameters)
        self.admin.execute("UPDATE loom.execution_claims SET state='expired' WHERE tenant_id=%s AND run_id=%s AND task_id=%s", (TENANT_A, RUN_A, task))
        self.admin.execute("UPDATE loom.tasks SET state='pending' WHERE tenant_id=%s AND run_id=%s AND task_id=%s", (TENANT_A, RUN_A, task))
        self.issue_claim(task, worker="replacement")
        self.expect_database_error("23505", lambda: self.admin.execute(statement, (TENANT_A, RUN_A, uuid4(), task, 2, "stable-logical-write", b"r" * 32)))

    def test_two_real_connections_cannot_issue_two_active_claims(self):
        task = self.new_task()
        self.admin.commit()  # make the synthetic task visible to both contenders
        barrier = Barrier(2)

        def compete(worker):
            with connect_private(self.pg, self.config) as db:
                barrier.wait(timeout=5)
                try:
                    with db.transaction():
                        return ("granted", self.issue_claim(task, worker=worker, connection=db)[0])
                except self.pg.Error as exc:
                    return ("rejected", exc.sqlstate)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(compete, ("worker-one", "worker-two")))
        self.assertEqual(sum(state == "granted" for state, _ in results), 1, results)
        self.assertEqual(self.admin.execute("SELECT count(*) FROM loom.execution_claims WHERE tenant_id=%s AND run_id=%s AND task_id=%s AND state='active'", (TENANT_A, RUN_A, task)).fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
