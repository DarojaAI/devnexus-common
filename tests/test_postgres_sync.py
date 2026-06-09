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
