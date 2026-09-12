"""Agent 3 security invariants (prompt §37, §40).

Structural tests over the whole DSR surface rather than over one function: they
walk the modules and assert properties that must hold no matter what is added
later. A new repository function that forgets its org filter, or a new log line
that prints a credential, fails here rather than in production.
"""

import asyncio
import inspect
import pathlib
import re
import uuid

import pytest

from app.agents.dsr.connectors import base, factory
from app.agents.dsr.errors import SourceNotAuthorizedError
from app.db.models import DsrAction, DsrEvidence
from app.db.repositories import dsr_repository

DSR_SOURCE = pathlib.Path(__file__).parent.parent / "app" / "agents" / "dsr"


def _dsr_sources() -> list[pathlib.Path]:
    files = sorted(DSR_SOURCE.rglob("*.py"))
    files.append(pathlib.Path(__file__).parent.parent / "app" / "db" / "repositories" / "dsr_repository.py")
    files.append(pathlib.Path(__file__).parent.parent / "app" / "api" / "v1" / "routes" / "dsr.py")
    files.append(pathlib.Path(__file__).parent.parent / "app" / "services" / "dsr_run_service.py")
    return [f for f in files if f.exists()]


# ── Tenant isolation ─────────────────────────────────────────────────────────────

# The only functions allowed to take no org_id, each for a stated reason.
_ORGLESS_ALLOWED = {
    # Runs in the worker on behalf of every tenant, not a signed-in user.
    "list_overdue_requests",
}


def test_every_repository_function_is_org_scoped():
    """A lookup by id alone is an IDOR. No function here may offer one."""
    offenders = []
    for name, fn in vars(dsr_repository).items():
        if name.startswith("_") or not asyncio.iscoroutinefunction(fn):
            continue
        if name in _ORGLESS_ALLOWED:
            continue
        if "org_id" not in inspect.signature(fn).parameters:
            offenders.append(name)
    assert not offenders, f"repository functions without an org_id: {offenders}"


def test_the_one_unscoped_function_documents_why():
    doc = dsr_repository.list_overdue_requests.__doc__ or ""
    assert "worker" in doc.lower()
    assert "not org-scoped" in doc.lower() or "without an org filter" in doc.lower()


@pytest.mark.asyncio
async def test_evidence_cannot_be_written_into_another_tenants_case():
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    row = DsrEvidence(
        id=uuid.uuid4(), org_id=org_b, request_id=uuid.uuid4(), search_run_id=uuid.uuid4(),
        source_name="crm", table_name="customers", matched_column="email",
        identifier_kind="email", match_type="exact", confidence=1.0, record_reference={"id": 1},
    )
    with pytest.raises(ValueError, match="refusing to write evidence"):
        await dsr_repository.add_evidence(_NullDB(), org_a, [row])


@pytest.mark.asyncio
async def test_actions_cannot_be_written_into_another_tenants_plan():
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    row = DsrAction(
        id=uuid.uuid4(), org_id=org_b, request_id=uuid.uuid4(), plan_id=uuid.uuid4(),
        source_name="crm", table_name="customers", record_reference={"id": 1},
        operation="delete_record", operation_payload={}, reason="x", expected_result="y",
    )
    with pytest.raises(ValueError, match="refusing to write an action"):
        await dsr_repository.add_actions(_NullDB(), org_a, [row])


# ── No credentials or PII in logs (§40) ──────────────────────────────────────────

_LOG_CALL = re.compile(r"logger\.(?:info|warning|error|debug|exception)\((.*?)\)", re.DOTALL)
_FORBIDDEN_IN_LOGS = (
    "password", "secret", "credential", "api_key", "challenge",
    "requester_email", "record_snapshot", "raw_request", "operation_payload",
)


def test_no_dsr_log_line_interpolates_a_secret_or_personal_value():
    offenders = []
    for path in _dsr_sources():
        for call in _LOG_CALL.findall(path.read_text(encoding="utf-8")):
            lowered = call.lower()
            for bad in _FORBIDDEN_IN_LOGS:
                if bad in lowered:
                    offenders.append(f"{path.name}: {bad} in {call.strip()[:70]}")
    assert not offenders, f"log lines carry sensitive values: {offenders}"


def test_connector_config_repr_is_redacted():
    from app.agents.dsr.connectors.postgres import PostgresDsrConfig

    config = PostgresDsrConfig(
        host="h", port=5432, dbname="d", read_user="r", read_password="READ-PW",
        write_user="w", write_password="WRITE-PW",
    )
    for rendering in (repr(config), f"{config}", str(config)):
        assert "READ-PW" not in rendering
        assert "WRITE-PW" not in rendering


def test_a_credential_is_never_read_outside_the_factory():
    """os.getenv for a secret belongs in one audited place. A connector that read
    the environment itself could bypass the credential_ref indirection."""
    offenders = [
        path.name for path in _dsr_sources()
        if path.name != "factory.py" and re.search(r"os\.(getenv|environ)", path.read_text(encoding="utf-8"))
    ]
    assert not offenders, f"modules reading the environment directly: {offenders}"


# ── No arbitrary SQL (§16) ───────────────────────────────────────────────────────

def test_no_dsr_module_builds_sql_by_formatting_a_caller_value():
    """Values are bound parameters. The only interpolation permitted in a statement
    is an already-allowlisted, already-validated identifier via quote_identifier."""
    suspicious = []
    for path in _dsr_sources():
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or '"""' in stripped:
                continue
            if not re.search(r"\b(SELECT|INSERT|UPDATE|DELETE|FROM|WHERE)\b", stripped):
                continue
            # An f-string in a SQL line is only acceptable if every substitution is a
            # quoted identifier, a projection built from them, or an integer limit.
            for placeholder in re.findall(r"\{([^}]+)\}", stripped):
                # Each of these is a fragment built ONLY from already-validated,
                # already-quoted identifiers plus bound-parameter placeholders --
                # test_the_search_predicate_binds_its_value covers `predicate`
                # specifically, since it is the one that touches a caller's value.
                allowed = (
                    "quoted", "projection", "assignments", "where_sql", "predicate",
                    "limit", "quote_identifier", "int(",
                )
                if not any(token in placeholder for token in allowed):
                    suspicious.append(f"{path.name}: {{{placeholder}}} in {stripped[:70]}")
    assert not suspicious, f"possible SQL interpolation of a non-identifier: {suspicious}"


def test_the_search_predicate_binds_its_value():
    """`predicate` is the one SQL fragment built next to a requester's value. It
    must contain a bound-parameter placeholder and no interpolated value."""
    source = (DSR_SOURCE / "connectors" / "postgres.py").read_text(encoding="utf-8")
    block = source[source.index("predicate = ("):source.index("sql = f\"SELECT")]
    assert "$1" in block, "the predicate does not bind its value"
    assert "quoted_column" in block
    # The only names interpolated into it are the quoted identifier and the literal $1.
    placeholders = set(re.findall(r"\{([^}]+)\}", block))
    assert placeholders <= {"quoted_column"}, f"predicate interpolates {placeholders}"


def test_the_llm_has_no_path_to_the_connector():
    """§26: the model may not execute arbitrary SQL. The simplest guarantee is that
    no connector module imports an LLM client at all."""
    connectors = sorted((DSR_SOURCE / "connectors").rglob("*.py"))
    offenders = [
        p.name for p in connectors
        if re.search(r"import.*\b(llm|openai|anthropic|nvidia|groq)\b", p.read_text(encoding="utf-8"), re.IGNORECASE)
    ]
    assert not offenders, f"connector modules importing an LLM: {offenders}"


def test_no_dsr_module_currently_invokes_an_llm():
    """Classification and response generation are deterministic today. If that
    changes, this test should be updated deliberately -- not silently."""
    offenders = [
        p.name for p in _dsr_sources()
        if re.search(r"generate_structured|get_reasoning_llm_client", p.read_text(encoding="utf-8"))
    ]
    assert not offenders, f"DSR modules calling an LLM: {offenders}"


# ── Fail-closed authorization ────────────────────────────────────────────────────

def test_building_a_write_connector_for_an_unauthorized_source_is_refused():
    from types import SimpleNamespace

    authorization = SimpleNamespace(
        enabled=True, searchable_tables=["customers"], identity_tables=["customers"],
        identifier_columns={"customers": {"email": "email"}}, returnable_columns={},
        erasable_columns={}, allow_execution=False, write_credential_ref=None,
    )
    data_source = SimpleNamespace(
        name="crm", connector="postgres", config={"host": "h", "dbname": "d", "user": "u"},
        credential_ref="SRC_READ",
    )
    with pytest.raises(SourceNotAuthorizedError, match="not authorized for DSR execution"):
        factory.build_connector(
            data_source=data_source, authorization=authorization, for_execution=True
        )


def test_an_unknown_connector_type_cannot_serve_a_dsr():
    from types import SimpleNamespace

    authorization = SimpleNamespace(
        enabled=True, searchable_tables=[], identity_tables=[], identifier_columns={},
        returnable_columns={}, erasable_columns={}, allow_execution=False,
        write_credential_ref=None,
    )
    data_source = SimpleNamespace(name="weird", connector="carrier_pigeon", config={}, credential_ref=None)
    with pytest.raises(SourceNotAuthorizedError, match="no DSR implementation"):
        factory.build_connector(data_source=data_source, authorization=authorization)


def test_the_row_cap_is_a_hard_constant_not_configuration():
    """An administrator must not be able to raise the per-table cap to something that
    turns a mis-scoped identifier into a bulk export."""
    assert base.MAX_ROWS_PER_TABLE <= 1000


def test_agent_2s_read_only_connector_contract_is_untouched():
    """The reason Agent 3 has its own connector protocol at all. If this contract
    ever gains a write method, discovery gains write capability as a side effect."""
    from app.agents.ropa.connectors.base import SourceConnector

    # Protocol members split across dir() (methods) and __annotations__ (attributes).
    members = {m for m in dir(SourceConnector) if not m.startswith("_")}
    members |= set(getattr(SourceConnector, "__annotations__", {}))
    assert members == {"discover", "source_type", "connector_name"}, (
        f"Agent 2's connector protocol changed: {sorted(members)}"
    )
    # And the contract still says read-only in so many words.
    doc = SourceConnector.__doc__ or ""
    assert "never write to, modify, or delete" in doc


class _NullDB:
    def add(self, row):
        return None

    async def flush(self):
        return None
