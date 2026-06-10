"""Tests for the sync facade and bulk_insert helper on DatabaseManager.

These tests cover:
  - _run_sync event-loop guard (must raise in a running loop, must work outside)
  - Sync wrappers bridge to the async methods correctly
  - bulk_insert builds the right SQL, chunks at page_size, and rejects
    mismatched column counts

No real postgres required — asyncpg is mocked at the .pool level.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock, patch
import threading

import asyncpg.exceptions

import pytest

from common.db.postgres import DatabaseManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_manager_with_mock_pool(
    *,
    enabled: bool = True,
    pool: Any = None,
    pool_state: str = "connected",
) -> DatabaseManager:
    """Build a DatabaseManager with a mocked asyncpg pool.

    Bypasses the env-var validation by setting attributes directly.
    """
    mgr = DatabaseManager.__new__(DatabaseManager)
    mgr.enabled = enabled
    mgr.pool = pool
    mgr._connection_state = pool_state
    mgr._connection_error = None
    mgr.min_size = 0
    mgr.max_size = 5
    mgr.host = "fake-host"
    mgr.port = 5432
    mgr.database = "fake-db"
    mgr.user = "fake-user"
    mgr.password = "fake-pass"
    mgr._application_name = "test"
    mgr._search_path = "public"

    # Issue #7: cancellation-safety state (set by _with_retry on CancelledError,
    # consumed by ensure_connected).
    mgr._needs_pool_reset = False

    # Issues #7/#9: dedicated event loop state (set by _ensure_loop, used by
    # _run_sync). The test helper sets them to None so _ensure_loop knows
    # to start a fresh daemon thread if a test calls it.
    mgr._loop = None
    mgr._loop_thread = None
    mgr._loop_lock = threading.Lock()
    mgr._loop_started = threading.Event()
    mgr._loop_failed = None

    return mgr


def _fake_pool_with_execute(return_value: str = "INSERT 0 0") -> MagicMock:
    """AsyncMock pool whose acquire().execute(...) returns return_value."""
    pool = MagicMock()

    async def _execute(query, *args):
        return return_value

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=cm)
    cm.__aexit__ = AsyncMock(return_value=None)
    cm.execute = AsyncMock(side_effect=_execute)

    pool.acquire = MagicMock(return_value=cm)
    return pool, cm


# ---------------------------------------------------------------------------
# _run_sync event-loop guard
# ---------------------------------------------------------------------------


def test_run_sync_runs_coro_outside_loop():
    """Outside an event loop, _run_sync runs the coroutine via asyncio.run()."""
    mgr = _make_manager_with_mock_pool()

    async def _coro():
        return 42

    assert mgr._run_sync(_coro()) == 42


def test_run_sync_raises_in_running_loop():
    """Inside a running event loop, _run_sync raises RuntimeError
    pointing the caller at the async methods."""
    mgr = _make_manager_with_mock_pool()
    observed: list[BaseException] = []

    async def _coro():
        return None

    async def _runner():
        try:
            mgr._run_sync(_coro())
        except RuntimeError as e:
            observed.append(e)

    asyncio.run(_runner())

    assert len(observed) == 1
    assert "running event loop" in str(observed[0])
    assert "async methods" in str(observed[0])


# ---------------------------------------------------------------------------
# Sync wrappers bridge to the async methods
# ---------------------------------------------------------------------------


def test_execute_sync_bridges_to_execute():
    """execute_sync calls execute(query, *args) via _run_sync."""
    pool, cm = _fake_pool_with_execute(return_value="INSERT 0 7")
    mgr = _make_manager_with_mock_pool(pool=pool)
    # ensure_connected is a no-op when pool is set and state is "connected"
    mgr.ensure_connected = AsyncMock()

    # _execute_impl uses pool.acquire() → cm.execute(query, *args)
    result = mgr.execute_sync("INSERT INTO t VALUES ($1, $2)", "a", 1)
    assert result == "INSERT 0 7"
    cm.execute.assert_awaited_once()


def test_fetch_sync_bridges_to_fetch():
    pool = MagicMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=cm)
    cm.__aexit__ = AsyncMock(return_value=None)
    cm.fetch = AsyncMock(return_value=[{"id": 1}, {"id": 2}])
    pool.acquire = MagicMock(return_value=cm)
    mgr = _make_manager_with_mock_pool(pool=pool)
    mgr.ensure_connected = AsyncMock()

    rows = mgr.fetch_sync("SELECT * FROM t WHERE id = $1", 1)
    assert len(rows) == 2
    cm.fetch.assert_awaited_once()


def test_fetchrow_sync_returns_single_row():
    pool = MagicMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=cm)
    cm.__aexit__ = AsyncMock(return_value=None)
    cm.fetchrow = AsyncMock(return_value={"id": 1})
    pool.acquire = MagicMock(return_value=cm)
    mgr = _make_manager_with_mock_pool(pool=pool)
    mgr.ensure_connected = AsyncMock()

    row = mgr.fetchrow_sync("SELECT * FROM t WHERE id = $1", 1)
    assert row == {"id": 1}


def test_fetchval_sync_returns_scalar():
    pool = MagicMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=cm)
    cm.__aexit__ = AsyncMock(return_value=None)
    cm.fetchval = AsyncMock(return_value=42)
    pool.acquire = MagicMock(return_value=cm)
    mgr = _make_manager_with_mock_pool(pool=pool)
    mgr.ensure_connected = AsyncMock()

    val = mgr.fetchval_sync("SELECT count(*) FROM t")
    assert val == 42


def test_sync_wrappers_ensure_connected_each_call():
    """ensure_connected should be invoked through the bridge so the
    pool can lazily connect on first use."""
    pool = MagicMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=cm)
    cm.__aexit__ = AsyncMock(return_value=None)
    cm.fetchval = AsyncMock(return_value=1)
    pool.acquire = MagicMock(return_value=cm)
    mgr = _make_manager_with_mock_pool(pool=pool)
    mgr.ensure_connected = AsyncMock()

    mgr.fetchval_sync("SELECT 1")
    mgr.fetchval_sync("SELECT 2")

    assert mgr.ensure_connected.await_count == 2


# ---------------------------------------------------------------------------
# bulk_insert: SQL building, pagination, validation
# ---------------------------------------------------------------------------


def test_bulk_insert_empty_rows_returns_zero():
    """No rows means no SQL executed, returns 'INSERT 0'."""
    pool, cm = _fake_pool_with_execute()
    mgr = _make_manager_with_mock_pool(pool=pool)
    mgr.ensure_connected = AsyncMock()

    result = asyncio.run(mgr.bulk_insert("t", ["a", "b"], []))
    assert result == "INSERT 0"
    cm.execute.assert_not_awaited()
    mgr.ensure_connected.assert_not_awaited()


def test_bulk_insert_builds_correct_sql():
    """Single-page insert: builds 'INSERT INTO t (a, b) VALUES ($1, $2)'."""
    pool, cm = _fake_pool_with_execute(return_value="INSERT 0 3")
    mgr = _make_manager_with_mock_pool(pool=pool)
    mgr.ensure_connected = AsyncMock()

    asyncio.run(
        mgr.bulk_insert("widgets", ["name", "qty"], [("a", 1), ("b", 2), ("c", 3)])
    )
    cm.execute.assert_awaited_once()
    args, _ = cm.execute.call_args
    sql, *params = args
    assert sql == "INSERT INTO widgets (name, qty) VALUES ($1, $2)"
    # All six values (3 rows × 2 cols) flattened in order
    assert params == ["a", 1, "b", 2, "c", 3]


def test_bulk_insert_with_on_conflict_clause():
    """on_conflict is appended verbatim to the SQL."""
    pool, cm = _fake_pool_with_execute(return_value="INSERT 0 1")
    mgr = _make_manager_with_mock_pool(pool=pool)
    mgr.ensure_connected = AsyncMock()

    asyncio.run(
        mgr.bulk_insert(
            "patterns",
            ["repo_id", "name"],
            [(1, "x")],
            on_conflict="ON CONFLICT (repo_id, name) DO UPDATE SET description = EXCLUDED.description",
        )
    )
    args, _ = cm.execute.call_args
    sql = args[0]
    assert sql.startswith("INSERT INTO patterns (repo_id, name) VALUES ($1, $2) ")
    assert "ON CONFLICT (repo_id, name) DO UPDATE" in sql


def test_bulk_insert_rejects_mismatched_column_counts():
    pool, cm = _fake_pool_with_execute()
    mgr = _make_manager_with_mock_pool(pool=pool)
    mgr.ensure_connected = AsyncMock()

    with pytest.raises(ValueError, match="every row must have exactly 2 values"):
        asyncio.run(
            mgr.bulk_insert(
                "t", ["a", "b"], [("only-one",), ("two", "cols"), ("three", "cols")]
            )
        )
    # ensure_connected should NOT have been called since validation
    # fails before any SQL.
    mgr.ensure_connected.assert_not_awaited()
    cm.execute.assert_not_awaited()


def test_bulk_insert_paginates_at_page_size():
    """5 rows with page_size=2 → 3 pages of 2, 2, 1."""
    pool = MagicMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=cm)
    cm.__aexit__ = AsyncMock(return_value=None)
    cm.execute = AsyncMock(side_effect=["INSERT 0 2", "INSERT 0 2", "INSERT 0 1"])
    pool.acquire = MagicMock(return_value=cm)
    mgr = _make_manager_with_mock_pool(pool=pool)
    mgr.ensure_connected = AsyncMock()

    rows = [(f"r{i}", i) for i in range(5)]
    result = asyncio.run(mgr.bulk_insert("t", ["a", "b"], rows, page_size=2))
    assert result == "INSERT 0 5"
    assert cm.execute.await_count == 3
    # Each page gets the right number of params
    page_sizes = [len(call_args[0][1:]) // 2 for call_args in cm.execute.call_args_list]
    assert page_sizes == [2, 2, 1]


def test_bulk_insert_total_handles_unparseable_result():
    """If the execute result is something we can't parse, return a
    best-effort total of 0 without raising."""
    pool = MagicMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=cm)
    cm.__aexit__ = AsyncMock(return_value=None)
    cm.execute = AsyncMock(side_effect=["weird response from server"])
    pool.acquire = MagicMock(return_value=cm)
    mgr = _make_manager_with_mock_pool(pool=pool)
    mgr.ensure_connected = AsyncMock()

    result = asyncio.run(mgr.bulk_insert("t", ["a"], [("x",)]))
    # Best-effort: still returns "INSERT 0 0"
    assert result == "INSERT 0 0"


def test_bulk_insert_sync_bridges():
    """bulk_insert_sync is the sync wrapper around bulk_insert."""
    pool, cm = _fake_pool_with_execute(return_value="INSERT 0 2")
    mgr = _make_manager_with_mock_pool(pool=pool)
    mgr.ensure_connected = AsyncMock()

    result = mgr.bulk_insert_sync("t", ["a", "b"], [("x", 1), ("y", 2)])
    assert result == "INSERT 0 2"
    cm.execute.assert_awaited_once()


# ---------------------------------------------------------------------------
# Module-level sync helpers
# ---------------------------------------------------------------------------


def test_init_db_sync_returns_singleton():
    """init_db_sync returns the singleton, calling connect via _run_sync."""
    from common.db import postgres as pg_module

    with patch.object(pg_module, "_db_manager", None):
        mgr = DatabaseManager.__new__(DatabaseManager)
        mgr.enabled = True
        mgr.pool = MagicMock()
        mgr._connection_state = "connected"
        mgr._connection_error = None
        mgr.host = "fake"
        mgr.port = 5432
        mgr.database = "fake"
        mgr.user = "fake"
        mgr.password = "fake"
        mgr._application_name = "test"
        mgr._search_path = "public"
        mgr._run_sync = MagicMock(return_value=None)

        with patch.object(pg_module, "DatabaseManager", return_value=mgr):
            result = pg_module.init_db_sync()
            assert result is mgr
            mgr._run_sync.assert_called_once()


def test_close_db_sync_skips_when_pool_none():
    """close_db_sync is a no-op when the pool was never initialized."""
    from common.db import postgres as pg_module

    with patch.object(pg_module, "_db_manager", None):
        mgr = DatabaseManager.__new__(DatabaseManager)
        mgr.enabled = True
        mgr.pool = None
        mgr._connection_state = "disconnected"
        mgr._connection_error = None
        mgr.host = "fake"
        mgr.port = 5432
        mgr.database = "fake"
        mgr.user = "fake"
        mgr.password = "fake"
        mgr._application_name = "test"
        mgr._search_path = "public"
        mgr._run_sync = MagicMock()

        with patch.object(pg_module, "DatabaseManager", return_value=mgr):
            pg_module.close_db_sync()
            mgr._run_sync.assert_not_called()


def test_close_db_sync_closes_when_pool_present():
    from common.db import postgres as pg_module

    mgr = DatabaseManager.__new__(DatabaseManager)
    mgr.enabled = True
    mgr.pool = MagicMock()
    mgr._connection_state = "connected"
    mgr._connection_error = None
    mgr.host = "fake"
    mgr.port = 5432
    mgr.database = "fake"
    mgr.user = "fake"
    mgr.password = "fake"
    mgr._application_name = "test"
    mgr._search_path = "public"
    mgr._run_sync = MagicMock()

    with patch.object(pg_module, "_db_manager", mgr):
        pg_module.close_db_sync()
        mgr._run_sync.assert_called_once()


# ---------------------------------------------------------------------------
# __init__ port handling — regression for 'int' object has no attribute 'strip'
# ---------------------------------------------------------------------------
# The constructor's type hint is `port: Optional[int]`, but the previous
# implementation did `port_str.strip()` which raised AttributeError when a
# caller passed an int (the natural Python type). This was first noticed
# when rag_research_tool's wiki server, after migrating to the sync facade,
# hit `Connection failed: 'int' object has no attribute 'strip'` in /health.


def test_init_accepts_int_port():
    """port=5432 (int) must not raise; the natural Python type for a port."""
    env = {"POSTGRES_HOST": "fake-host"}
    with patch.dict(os.environ, env, clear=False):
        mgr = DatabaseManager(host="fake-host", port=5432)
    assert mgr.port == 5432
    assert isinstance(mgr.port, int)


def test_init_accepts_str_port():
    """port='5432' (str, e.g. from os.environ) must be coerced to int."""
    env = {"POSTGRES_HOST": "fake-host"}
    with patch.dict(os.environ, env, clear=False):
        mgr = DatabaseManager(host="fake-host", port="5432")
    assert mgr.port == 5432
    assert isinstance(mgr.port, int)


def test_init_port_none_uses_env_var():
    """port=None falls back to POSTGRES_PORT env var."""
    env = {"POSTGRES_HOST": "fake-host", "POSTGRES_PORT": "6432"}
    with patch.dict(os.environ, env, clear=False):
        mgr = DatabaseManager(host="fake-host", port=None)
    assert mgr.port == 6432


def test_init_port_none_default_when_env_unset():
    """port=None with POSTGRES_PORT unset uses the 5432 default."""
    env = {"POSTGRES_HOST": "fake-host"}
    with patch.dict(os.environ, env, clear=False):
        os.environ.pop("POSTGRES_PORT", None)
        mgr = DatabaseManager(host="fake-host", port=None)
    assert mgr.port == 5432


def test_init_default_use_postgresql_is_true():
    """The facade exists to provide PG access — defaulting to enabled.

    Regression: the previous default of 'false' silently disabled every
    downstream that didn't set USE_POSTGRESQL=true explicitly. First caught
    when rag_research_tool's wiki /db-status returned 'PostgreSQL is disabled'
    even though POSTGRES_HOST was set, because no caller set the flag.
    """
    env = {"POSTGRES_HOST": "fake-host"}
    with patch.dict(os.environ, env, clear=False):
        os.environ.pop("USE_POSTGRESQL", None)
        mgr = DatabaseManager(host="fake-host", port=5432)
    assert mgr.enabled is True
    assert mgr.port == 5432


def test_init_use_postgresql_false_explicit_opt_out():
    """Setting USE_POSTGRESQL=false still disables the client."""
    env = {"POSTGRES_HOST": "fake-host", "USE_POSTGRESQL": "false"}
    with patch.dict(os.environ, env, clear=False):
        mgr = DatabaseManager(host="fake-host", port=5432)
    assert mgr.enabled is False
    assert mgr.port == 5432


def test_init_min_pool_size_defaults_to_zero_outside_prod():
    """min_size defaults to 0 in non-prod envs (lazy connect).

    A previous version tried min_size=2 to reduce cold-start races, but
    that amplified a concurrency bug: the DatabaseManager's disconnect()/
    connect() path is NOT safe for concurrent callers, and eager init
    increased the chance of two coroutines racing for the pool. Reverted.
    """
    env = {"POSTGRES_HOST": "fake-host", "ENVIRONMENT": "dev"}
    with patch.dict(os.environ, env, clear=False):
        mgr = DatabaseManager(host="fake-host", port=5432)
    assert mgr.min_size == 0


def test_init_min_pool_size_two_in_prod():
    """In prod env, min_size=2 is the original default."""
    env = {"POSTGRES_HOST": "fake-host", "ENVIRONMENT": "prod"}
    with patch.dict(os.environ, env, clear=False):
        mgr = DatabaseManager(host="fake-host", port=5432)
    assert mgr.min_size == 2


def test_init_min_pool_size_explicit_zero_still_honored():
    """Callers who genuinely want lazy connect can pass min_size=0."""
    env = {"POSTGRES_HOST": "fake-host", "ENVIRONMENT": "prod"}
    with patch.dict(os.environ, env, clear=False):
        mgr = DatabaseManager(host="fake-host", port=5432, min_size=0)
    assert mgr.min_size == 0


@pytest.mark.asyncio
async def test_ensure_connected_does_no_liveness_probe():
    """ensure_connected must not acquire a connection just to probe.

    A previous version ran a `SELECT 1` liveness probe and called
    disconnect()+connect() on failure. That pattern is NOT safe for
    concurrent callers (segfaults when one coroutine is in disconnect()
    while another tries to acquire from the closing pool).

    The natural asyncpg pattern is to skip the probe entirely; let
    `_with_retry` handle transient errors by retrying on a fresh
    connection from the pool.
    """
    mgr = _make_manager_with_mock_pool()
    # pool.acquire should NOT be called at all
    mgr.pool = MagicMock()
    mgr.pool.acquire = MagicMock()
    mgr.disconnect = AsyncMock()
    mgr.connect = AsyncMock()

    await mgr.ensure_connected()

    mgr.pool.acquire.assert_not_called()
    mgr.disconnect.assert_not_awaited()
    mgr.connect.assert_not_awaited()


@pytest.mark.asyncio
async def test_with_retry_does_not_disconnect_on_transient_error():
    """_with_retry must NOT call disconnect() on transient errors.

    Regression: rag_research_tool's wiki revision -00078-bsc segfaulted
    because _with_retry called disconnect() while another concurrent
    request was using the pool. Two SIGSEGVs in two minutes.

    Fix: _with_retry just sleeps and retries. asyncpg's pool naturally
    returns a different connection on the next acquire.
    """
    mgr = _make_manager_with_mock_pool()
    mgr.disconnect = AsyncMock()
    mgr.connect = AsyncMock()

    call_count = [0]

    async def _flaky():
        call_count[0] += 1
        if call_count[0] == 1:
            raise asyncpg.exceptions.ConnectionDoesNotExistError("simulated stale")
        return 42

    result = await mgr._with_retry(_flaky)
    assert result == 42
    assert call_count[0] == 2
    # CRITICAL: disconnect and connect must NOT have been called
    mgr.disconnect.assert_not_awaited()



# ---------------------------------------------------------------------------
# Issue #7: cancellation safety
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_with_retry_marks_pool_for_reset_on_cancellation():
    """When the in-flight coro is cancelled, _with_retry sets
    _needs_pool_reset = True and re-raises CancelledError unchanged.

    This is the load-bearing assertion for issue #7: the flag is the
    handoff that lets the next non-cancelled ensure_connected() rebuild
    the pool cleanly, instead of us disconnecting mid-cancellation (which
    is the original segfault pattern).
    """
    mgr = _make_manager_with_mock_pool()
    assert mgr._needs_pool_reset is False

    async def _cancelled_coro():
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await mgr._with_retry(_cancelled_coro)

    assert mgr._needs_pool_reset is True, (
        "CancelledError must flag the pool for deferred reset "
        "(see issue #7 / production SIGSEGV root cause)"
    )


@pytest.mark.asyncio
async def test_ensure_connected_resets_pool_when_flag_set():
    """If _needs_pool_reset is True and pool is alive, ensure_connected
    disconnects cleanly and reconnects, then clears the flag.

    The disconnect happens in a non-racing context (no CancelledError
    in flight), which is what makes this pattern safe.
    """
    mgr = _make_manager_with_mock_pool()
    mgr._needs_pool_reset = True
    old_pool = MagicMock(name="old_pool")
    mgr.pool = old_pool
    mgr.disconnect = AsyncMock()
    mgr.connect = AsyncMock()

    await mgr.ensure_connected()

    mgr.disconnect.assert_awaited_once()
    mgr.connect.assert_awaited_once()
    assert mgr._needs_pool_reset is False, "reset flag must be consumed"


@pytest.mark.asyncio
async def test_ensure_connected_no_reset_when_flag_unset():
    """Regression check: when _needs_pool_reset is False (the normal
    case), ensure_connected does NOT call disconnect. We rely on
    asyncpg's natural pool-dead-connection handling; calling disconnect
    on every call would be expensive and would re-introduce the
    segfault pattern (issue #7).
    """
    mgr = _make_manager_with_mock_pool()
    assert mgr._needs_pool_reset is False
    # pool is non-None so ensure_connected takes the "already connected" path
    mgr.pool = MagicMock(name="healthy_pool")
    mgr.disconnect = AsyncMock()
    mgr.connect = AsyncMock()

    await mgr.ensure_connected()

    mgr.disconnect.assert_not_awaited()
    mgr.connect.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_connected_resets_flag_even_if_disconnect_fails():
    """If disconnect() raises during the reset path, ensure_connected
    must still clear the flag and proceed (set pool to None, fall
    through to reconnect). Otherwise the flag stays set forever and
    every subsequent call tries (and fails) to reset."""
    mgr = _make_manager_with_mock_pool()
    mgr._needs_pool_reset = True
    mgr.pool = MagicMock(name="old_pool")
    mgr.disconnect = AsyncMock(side_effect=RuntimeError("disconnect failed"))
    mgr.connect = AsyncMock()

    await mgr.ensure_connected()

    assert mgr._needs_pool_reset is False
    mgr.connect.assert_awaited_once()


# ---------------------------------------------------------------------------
# Issues #7/#9: dedicated event loop in _run_sync
# ---------------------------------------------------------------------------


def test_run_sync_uses_dedicated_loop_not_per_call_asyncio_run():
    """Two _run_sync calls in a row must share the same loop. The whole
    point of the refactor is to bind the asyncpg pool to one stable loop
    for the lifetime of the DatabaseManager; a fresh loop per call would
    re-introduce the segfault (issue #7/#9)."""
    mgr = _make_manager_with_mock_pool()

    async def _coro():
        return 1

    mgr._run_sync(_coro())
    first_loop = mgr._loop
    first_thread = mgr._loop_thread
    assert first_loop is not None
    assert first_thread is not None
    assert first_thread.daemon is True, "loop thread must be daemon"
    assert first_thread.is_alive()

    mgr._run_sync(_coro())
    assert mgr._loop is first_loop, "second call must reuse the same loop"
    assert mgr._loop_thread is first_thread, "second call must reuse the same thread"


def test_ensure_loop_returns_same_loop_for_concurrent_threads():
    """Thread-safety: two threads racing on _ensure_loop() must both
    get the same loop (not start two threads). The lock serializes
    the start sequence."""
    mgr = _make_manager_with_mock_pool()
    loops: list = []

    def _worker():
        loop = mgr._ensure_loop()
        loops.append(loop)

    t1 = threading.Thread(target=_worker)
    t2 = threading.Thread(target=_worker)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert len(loops) == 2
    assert loops[0] is loops[1], "both threads must observe the same loop"
    assert mgr._loop is not None
    assert mgr._loop_thread is not None


def test_run_sync_propagates_cancelled_error_from_coro():
    """If the coroutine itself raises CancelledError, _run_sync re-raises
    it as-is. This is the cancellation contract: the caller knows the
    op was cancelled and can react. Combined with _with_retry's flag-set
    behavior, this is what makes the whole pipeline work."""
    mgr = _make_manager_with_mock_pool()

    async def _cancelled():
        raise asyncio.CancelledError()

    # concurrent.futures.Future.result() re-raises asyncio.CancelledError
    # as concurrent.futures.CancelledError (different class, NOT a subclass
    # of asyncio.CancelledError as of Python 3.10). The cancellation
    # contract from the caller's perspective is "a CancelledError-family
    # exception was raised" — accept either class.
    import concurrent.futures
    with pytest.raises((asyncio.CancelledError, concurrent.futures.CancelledError)):
        mgr._run_sync(_cancelled())

    assert mgr._loop is not None, "loop must be initialized even on cancellation"
