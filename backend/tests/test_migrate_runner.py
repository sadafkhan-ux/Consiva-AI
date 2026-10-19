"""The migration runner (migrate.py): what makes `docker compose up` safe to run on every deploy.

Two layers:

  * pure tests of the planning and lint logic -- no database, always run;
  * real-database tests, each in its OWN temporary database that the test creates and drops. They
    run only when TEST_DATABASE_URL names a server (never DATABASE_URL, never the deployment), and
    skip otherwise -- the same rule as every other committing test in this suite (tests/live_db.py).

The properties held here are the ones the deployment depends on: nothing is marked applied unless
it completed, a failure rolls the file back whole, two runners serialise, an unsafe state is
refused BEFORE anything runs, and every outcome has its own exit code for the orchestrator.
"""

import asyncio
import re
import uuid
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import asyncpg
import pytest

import migrate
from tests.live_db import WRITABLE_SKIP_REASON, as_sync, writable_dsn

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"


# ── Pure logic ───────────────────────────────────────────────────────────────────

def test_the_real_migration_files_pass_every_file_level_check():
    versions = [p.stem for p in migrate.discover_migrations()]
    assert versions, "no migrations discovered"
    assert migrate.validate_files(versions) == []
    for path in migrate.discover_migrations():
        found = migrate.non_transactional_statements(path.read_text(encoding="utf-8"))
        assert not found, f"{path.name} has a statement that cannot run in a transaction: {found}"


def test_duplicate_and_malformed_names_are_reported():
    problems = migrate.validate_files(["0001_a", "0001_b", "bad name", "12_short", "0005_ok"])
    text = " | ".join(problems)
    assert "share number 0001" in text
    assert "bad name.sql" in text and "12_short.sql" in text
    assert "0005_ok" not in text


def test_plan_separates_pending_out_of_order_and_unknown():
    plan = migrate.make_plan(
        ["0001_a", "0002_b", "0003_c", "0004_d"], {"0001_a", "0003_c", "0099_future"},
    )
    assert plan.pending == ["0002_b", "0004_d"]
    assert plan.out_of_order == ["0002_b"], "0002 is below an applied 0003; 0004 is simply next"
    assert plan.unknown_applied == ["0099_future"]


def test_a_clean_sequence_has_nothing_out_of_order():
    plan = migrate.make_plan(["0001_a", "0002_b", "0003_c"], {"0001_a"})
    assert plan.pending == ["0002_b", "0003_c"] and plan.out_of_order == []


def test_a_fresh_database_has_no_out_of_order_migrations():
    plan = migrate.make_plan(["0001_a", "0002_b"], set())
    assert plan.pending == ["0001_a", "0002_b"] and plan.out_of_order == []


@pytest.mark.parametrize("sql,expected", [
    ("create unique index concurrently i on t (x);", ["CREATE UNIQUE INDEX"]),
    ("DROP INDEX CONCURRENTLY i;", ["DROP INDEX CONCURRENTLY"]),
    ("vacuum full t;", ["VACUUM"]),
    ("alter type status add value 'x';", ["ALTER TYPE STATUS"]),
    ("create database d;", ["CREATE DATABASE"]),
    # Mentioned only in comments (migration 0017 does exactly this): not a statement.
    ("-- not CONCURRENTLY: create index concurrently would fail\nselect 1;", []),
    ("/* VACUUM and ALTER SYSTEM are not used here */ select 1;", []),
    ("create index if not exists i on t (x);", []),
])
def test_non_transactional_statements_are_found_but_comments_are_ignored(sql, expected):
    assert migrate.non_transactional_statements(sql) == expected


def test_the_no_transaction_marker_must_be_near_the_top_of_the_file():
    assert migrate.wants_no_transaction("-- migrate:no-transaction\ncreate index concurrently i on t(x);")
    assert not migrate.wants_no_transaction("select 1;\n" * 30 + "-- migrate:no-transaction")
    assert not migrate.wants_no_transaction("-- migrate: no-transaction maybe\nselect 1;")


def test_the_target_description_never_contains_the_password():
    text = migrate.describe_target("postgresql://consiva:s3cr3t-pw@db:5432/consiva")
    assert text == "host=db port=5432 database=consiva user=consiva"
    assert "s3cr3t" not in text


def test_the_asyncpg_driver_qualifier_is_stripped(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert migrate._load_database_url("postgresql+asyncpg://u:p@h/d") == "postgresql://u:p@h/d"


def test_environment_beats_the_env_file(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://from-env:p@h/d")
    assert migrate._load_database_url(None).startswith("postgresql://from-env:")


def test_exit_codes_are_distinct_so_the_orchestrator_can_tell_failures_apart():
    codes = [migrate.EXIT_OK, migrate.EXIT_FAILED, migrate.EXIT_PENDING, migrate.EXIT_REFUSED,
             migrate.EXIT_DB_UNREACHABLE, migrate.EXIT_LOCK_TIMEOUT]
    assert len(set(codes)) == len(codes) and migrate.EXIT_OK == 0
    assert all(c != 0 for c in codes[1:]), "every non-success outcome is a non-zero exit"


# ── Real database ────────────────────────────────────────────────────────────────

needs_db = pytest.mark.skipif(not writable_dsn(), reason=WRITABLE_SKIP_REASON)


def _with_database(dsn: str, name: str) -> str:
    parsed = urlparse(as_sync(dsn))
    return urlunparse(parsed._replace(path=f"/{name}"))


@pytest.fixture
async def temp_db():
    """A brand-new empty database on the TEST server, dropped afterwards. Yields its URL."""
    admin = as_sync(writable_dsn())
    name = f"migtest_{uuid.uuid4().hex[:12]}"
    conn = await asyncpg.connect(admin)
    try:
        await conn.execute(f'create database "{name}"')
    finally:
        await conn.close()
    try:
        yield _with_database(writable_dsn(), name)
    finally:
        conn = await asyncpg.connect(admin)
        try:
            await conn.execute(f'drop database if exists "{name}" with (force)')
        finally:
            await conn.close()


def _write(directory: Path, files: dict[str, str]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name, sql in files.items():
        (directory / f"{name}.sql").write_text(sql, encoding="utf-8")
    return directory


async def _versions(url: str) -> list[str]:
    conn = await asyncpg.connect(url)
    try:
        return [r["version"] for r in await conn.fetch("select version from schema_migrations order by version")]
    finally:
        await conn.close()


async def _scalar(url: str, query: str):
    conn = await asyncpg.connect(url)
    try:
        return await conn.fetchval(query)
    finally:
        await conn.close()


async def _run(url, directory, **kw) -> int:
    kw.setdefault("lock_timeout", 20)
    return await migrate.run(url, migrations_dir=directory, **kw)


BASIC = {
    "0001_one": "create table t_one (id int primary key);",
    "0002_two": "create table t_two (id int primary key);",
    "0003_three": "create table t_three (id int primary key);",
}


@needs_db
async def test_a_fresh_database_gets_every_migration_in_order_and_a_rerun_is_a_no_op(temp_db, tmp_path, capsys):
    directory = _write(tmp_path / "m", BASIC)

    assert await _run(temp_db, directory) == migrate.EXIT_OK
    assert await _versions(temp_db) == ["0001_one", "0002_two", "0003_three"]
    assert "Applying 3 pending" in capsys.readouterr().out

    assert await _run(temp_db, directory) == migrate.EXIT_OK
    assert "Nothing to do" in capsys.readouterr().out
    assert await _versions(temp_db) == ["0001_one", "0002_two", "0003_three"], "nothing re-recorded"


@needs_db
async def test_an_existing_database_runs_only_what_is_pending(temp_db, tmp_path, capsys):
    directory = _write(tmp_path / "m", BASIC)
    assert await _run(temp_db, _write(tmp_path / "first", {"0001_one": BASIC["0001_one"]})) == 0
    capsys.readouterr()

    assert await _run(temp_db, directory) == migrate.EXIT_OK

    out = capsys.readouterr().out
    assert "Applying 2 pending" in out and "0001_one" not in out
    assert await _versions(temp_db) == ["0001_one", "0002_two", "0003_three"]


@needs_db
async def test_a_failing_migration_rolls_back_whole_is_not_recorded_and_the_run_resumes(temp_db, tmp_path, capsys):
    directory = _write(tmp_path / "m", {
        "0001_ok": "create table t_ok (id int);",
        "0002_half": "create table t_half (id int); alter table does_not_exist add column x int;",
        "0003_after": "create table t_after (id int);",
    })

    assert await _run(temp_db, directory) == migrate.EXIT_FAILED

    err = capsys.readouterr().err
    assert "0002_half failed and was rolled back" in err and "NOT recorded" in err
    assert await _versions(temp_db) == ["0001_ok"], "earlier migration kept; failed and later ones not recorded"
    assert await _scalar(temp_db, "select to_regclass('t_half') is null"), "half-applied work was rolled back"
    assert await _scalar(temp_db, "select to_regclass('t_after') is null"), "nothing after the failure ran"

    # Fix the file; the next run resumes at exactly that migration.
    _write(directory, {"0002_half": "create table t_half (id int);"})
    assert await _run(temp_db, directory) == migrate.EXIT_OK
    assert await _versions(temp_db) == ["0001_ok", "0002_half", "0003_after"]


@needs_db
async def test_a_migration_and_its_history_record_commit_or_roll_back_together(temp_db, tmp_path, capsys):
    """The guarantee the explicit per-file transaction exists for. This migration succeeds on its
    own but makes the history insert that follows it impossible. If the migration were committed
    separately from its record, the schema would change with NO record that it ever ran -- and the
    next deployment would run it again. Both must roll back together."""
    directory = _write(tmp_path / "m", {
        "0001_base": "create table t_base (id int);",
        "0002_breaks_the_record": "create table t_atomic (id int); alter table schema_migrations rename to gone;",
    })

    assert await _run(temp_db, directory) == migrate.EXIT_FAILED

    assert await _scalar(temp_db, "select to_regclass('t_atomic') is null"), (
        "the migration's own change was committed even though its record could not be"
    )
    assert await _versions(temp_db) == ["0001_base"], "the history table is intact and unchanged"


@needs_db
async def test_two_deployments_racing_apply_each_migration_exactly_once(temp_db, tmp_path):
    """A slow migration holds the lock; the second runner must wait, then find nothing to do.
    Without the advisory lock both would run 0002 and one would fail on the duplicate table."""
    directory = _write(tmp_path / "m", {
        "0001_base": "create table counter (n int);",
        "0002_slow": "select pg_sleep(1.5); insert into counter values (1);",
    })

    first, second = await asyncio.gather(
        _run(temp_db, directory), _delayed(0.4, _run(temp_db, directory)),
    )

    assert (first, second) == (migrate.EXIT_OK, migrate.EXIT_OK)
    assert await _scalar(temp_db, "select count(*) from counter") == 1, "the migration body ran once"
    assert await _versions(temp_db) == ["0001_base", "0002_slow"]


async def _delayed(seconds, coro):
    await asyncio.sleep(seconds)
    return await coro


@needs_db
async def test_a_runner_gives_up_with_its_own_exit_code_if_the_lock_never_frees(temp_db, tmp_path):
    directory = _write(tmp_path / "m", BASIC)
    holder = await asyncpg.connect(temp_db)
    try:
        await holder.fetchval("select pg_advisory_lock($1)", migrate.ADVISORY_LOCK_KEY)

        code = await _run(temp_db, directory, lock_timeout=1)

        assert code == migrate.EXIT_LOCK_TIMEOUT
        assert not await _scalar(temp_db, "select to_regclass('schema_migrations') is not null"), (
            "a runner that never got the lock must not have written anything"
        )
    finally:
        await holder.close()


@needs_db
async def test_the_lock_is_released_when_the_runner_ends_even_after_a_failure(temp_db, tmp_path):
    bad = _write(tmp_path / "bad", {"0001_bad": "select 1/0;"})
    assert await _run(temp_db, bad) == migrate.EXIT_FAILED

    good = _write(tmp_path / "good", {"0001_bad": "create table t_good (id int);"})
    assert await _run(temp_db, good, lock_timeout=2) == migrate.EXIT_OK, "a failed run must not wedge the next"


@needs_db
async def test_an_existing_schema_with_an_empty_history_is_refused_never_auto_baselined(temp_db, tmp_path, capsys):
    conn = await asyncpg.connect(temp_db)
    try:
        await conn.execute("create table consent_scans (id int)")   # someone built this by hand
    finally:
        await conn.close()
    directory = _write(tmp_path / "m", BASIC)

    assert await _run(temp_db, directory) == migrate.EXIT_REFUSED

    err = capsys.readouterr().err
    assert "history is EMPTY" in err and "--baseline-through" in err
    assert await _versions(temp_db) == [], "nothing recorded and nothing run"
    assert await _scalar(temp_db, "select to_regclass('t_one') is null")


@needs_db
async def test_baselining_records_without_running_and_only_when_asked(temp_db, tmp_path, capsys):
    conn = await asyncpg.connect(temp_db)
    try:
        await conn.execute("create table consent_scans (id int); create table t_one (id int primary key);")
    finally:
        await conn.close()
    directory = _write(tmp_path / "m", BASIC)

    assert await _run(temp_db, directory, baseline_through="0001_one") == migrate.EXIT_OK
    assert await _versions(temp_db) == ["0001_one"]
    assert "WITHOUT running them" in capsys.readouterr().out

    # Now a normal run is allowed, and applies only what was not baselined.
    assert await _run(temp_db, directory) == migrate.EXIT_OK
    assert await _versions(temp_db) == ["0001_one", "0002_two", "0003_three"]


@needs_db
async def test_baselining_an_unknown_version_is_refused(temp_db, tmp_path):
    directory = _write(tmp_path / "m", BASIC)
    assert await _run(temp_db, directory, baseline_through="0099_nope") == migrate.EXIT_REFUSED
    assert await _versions(temp_db) == []


@needs_db
async def test_a_migration_numbered_below_an_applied_one_is_refused_unless_allowed(temp_db, tmp_path, capsys):
    assert await _run(temp_db, _write(tmp_path / "a", {
        "0001_one": BASIC["0001_one"], "0003_three": BASIC["0003_three"],
    })) == 0
    capsys.readouterr()
    full = _write(tmp_path / "b", BASIC)         # now 0002 appears, below the applied 0003

    assert await _run(temp_db, full) == migrate.EXIT_REFUSED
    assert "numbered BELOW" in capsys.readouterr().err
    assert await _scalar(temp_db, "select to_regclass('t_two') is null"), "refused before running anything"

    assert await _run(temp_db, full, allow_out_of_order=True) == migrate.EXIT_OK
    assert await _versions(temp_db) == ["0001_one", "0002_two", "0003_three"]


@needs_db
async def test_a_statement_that_cannot_run_in_a_transaction_is_refused_before_ANY_file_runs(temp_db, tmp_path, capsys):
    directory = _write(tmp_path / "m", {
        "0001_fine": "create table t_fine (id int);",
        "0002_needs_autocommit": "create index concurrently idx_x on t_fine (id);",
    })

    assert await _run(temp_db, directory) == migrate.EXIT_REFUSED

    assert "cannot run inside a transaction" in capsys.readouterr().err
    assert await _scalar(temp_db, "select to_regclass('t_fine') is null"), (
        "the fine file must not run either: the whole pending set is checked first"
    )
    assert await _versions(temp_db) == []


@needs_db
async def test_an_explicitly_marked_non_transactional_migration_runs_and_is_recorded_after_success(temp_db, tmp_path):
    directory = _write(tmp_path / "m", {
        "0001_table": "create table t_idx (id int);",
        "0002_index": "-- migrate:no-transaction\ncreate index concurrently idx_t on t_idx (id);",
    })

    assert await _run(temp_db, directory) == migrate.EXIT_OK
    assert await _versions(temp_db) == ["0001_table", "0002_index"]
    assert await _scalar(temp_db, "select to_regclass('idx_t') is not null")


@needs_db
async def test_status_and_check_are_read_only_and_check_reports_pending(temp_db, tmp_path, capsys):
    directory = _write(tmp_path / "m", BASIC)

    assert await _run(temp_db, directory, status_only=True) == migrate.EXIT_OK
    assert await _run(temp_db, directory, check_only=True) == migrate.EXIT_PENDING
    assert not await _scalar(temp_db, "select to_regclass('schema_migrations') is not null"), (
        "--status/--check must not create the history table on a database they only inspect"
    )
    assert "PENDING" in capsys.readouterr().out

    assert await _run(temp_db, directory) == migrate.EXIT_OK
    assert await _run(temp_db, directory, check_only=True) == migrate.EXIT_OK


@needs_db
async def test_a_version_the_database_has_but_the_checkout_lacks_is_a_note_not_a_failure(temp_db, tmp_path, capsys):
    assert await _run(temp_db, _write(tmp_path / "new", BASIC)) == 0
    capsys.readouterr()

    older = _write(tmp_path / "old", {"0001_one": BASIC["0001_one"], "0002_two": BASIC["0002_two"]})
    assert await _run(temp_db, older) == migrate.EXIT_OK        # a rollback to an older image

    out = capsys.readouterr().out
    assert "0003_three is recorded as applied but is not in this checkout" in out


@needs_db
async def test_duplicate_migration_numbers_are_refused_before_connecting_to_anything_risky(temp_db, tmp_path, capsys):
    directory = _write(tmp_path / "m", {"0001_a": "select 1;", "0001_b": "select 1;"})

    assert await _run(temp_db, directory) == migrate.EXIT_REFUSED
    assert "share number 0001" in capsys.readouterr().err
    assert await _versions_or_none(temp_db) is None


async def _versions_or_none(url):
    if not await _scalar(url, "select to_regclass('schema_migrations') is not null"):
        return None
    return await _versions(url)


@needs_db
async def test_the_ddl_lock_timeout_fails_a_migration_stuck_behind_a_query_instead_of_hanging(temp_db, tmp_path):
    directory = _write(tmp_path / "m", {
        "0001_base": "create table busy (id int);",
        "0002_alter": "alter table busy add column x int;",
    })
    assert await _run(temp_db, _write(tmp_path / "first", {"0001_base": "create table busy (id int);"})) == 0
    blocker = await asyncpg.connect(temp_db)
    try:
        tx = blocker.transaction()
        await tx.start()
        await blocker.execute("lock table busy in access exclusive mode")     # an app query holding it

        started = asyncio.get_event_loop().time()
        code = await _run(temp_db, directory, ddl_lock_timeout=1)

        assert code == migrate.EXIT_FAILED
        assert asyncio.get_event_loop().time() - started < 15, "it failed fast instead of hanging"
        assert await _versions(temp_db) == ["0001_base"], "the stuck migration is not recorded"
        await tx.rollback()
    finally:
        await blocker.close()
    assert await _run(temp_db, directory) == migrate.EXIT_OK, "and it succeeds once the table is free"


async def test_an_unreachable_database_exits_4_quickly_without_leaking_the_password(capsys):
    code = await migrate.run("postgresql://consiva:topsecret-pw@127.0.0.1:1/consiva", wait_for_db=0)

    assert code == migrate.EXIT_DB_UNREACHABLE
    captured = capsys.readouterr()
    assert "could not connect" in captured.err
    assert "topsecret-pw" not in captured.out + captured.err


@needs_db
async def test_a_missing_database_is_a_configuration_error_not_something_to_wait_for(temp_db, capsys):
    started = asyncio.get_event_loop().time()
    code = await migrate.run(_with_database(writable_dsn(), "no_such_db_" + uuid.uuid4().hex[:8]), wait_for_db=60)

    assert code == migrate.EXIT_DB_UNREACHABLE
    assert asyncio.get_event_loop().time() - started < 10, "must not retry a permanent error for a minute"
    assert "configuration error" in capsys.readouterr().err


@needs_db
async def test_the_real_migrations_build_an_empty_database_and_a_rerun_is_a_no_op(temp_db, capsys):
    """The same thing CI and a fresh Docker deployment do, against the actual migration files."""
    assert await migrate.run(temp_db, lock_timeout=30) == migrate.EXIT_OK
    expected = [p.stem for p in migrate.discover_migrations()]
    assert await _versions(temp_db) == expected
    capsys.readouterr()

    assert await migrate.run(temp_db) == migrate.EXIT_OK
    assert "Nothing to do" in capsys.readouterr().out
    assert await migrate.run(temp_db, check_only=True) == migrate.EXIT_OK


def test_the_runner_never_issues_a_destructive_statement_itself():
    """It runs migration files; it does not drop, truncate or recreate anything of its own."""
    source = Path(migrate.__file__).read_text(encoding="utf-8")
    code = re.sub(r'""".*?"""', "", source, flags=re.DOTALL)        # docstrings
    code = re.sub(r"#[^\n]*", "", code)                              # comments
    for word in ("drop table", "drop database", "drop schema", "truncate", "delete from"):
        assert word not in code.lower(), f"migrate.py itself contains {word!r}"
