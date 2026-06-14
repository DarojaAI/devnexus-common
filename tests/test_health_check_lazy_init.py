"""Tests for lazy-init fix in DatabaseManager.health_check() (issue #787).

The DatabaseManager uses a lazy-init pattern: ``self.pool`` is None
until the first query triggers ``ensure_connected()``. Services like
the wiki service construct a DatabaseManager at FastAPI startup but
never issue a query at startup, so a naive ``health_check()`` would
report ``disconnected`` even though the pool would connect fine on
first use. This file pins down the fix:

  - When ``self.pool is None``, ``health_check()`` calls
    ``ensure_connected()`` first.
  - On success, the result is ``healthy`` (or whatever the probe
    reports), not ``disconnected``.
  - On ``ensure_connected()`` failure, the result is
    ``unhealthy`` with an error message — i.e. the /health
    endpoint surfaces real connection failures instead of a
    confusing "no pool" misreport.
"""

from __future__ import annotations

import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from common.db.postgres import DatabaseManager, _PoolStats


# ---------------------------------------------------------------------------
# Helpers (mirrors _make_manager_with_mock_pool in test_pool_saturation.py)
# ---------------------------------------------------------------------------


def _make_manager_with_mock_pool(
    *,
    enabled: bool = True,
    pool: object = None,
) -> DatabaseManager:
    """Bypass env-var validation; build a manager with a mock pool."""
    mgr = DatabaseManager.__new__(DatabaseManager)
    mgr.enabled = enabled
    mgr.pool = pool
    mgr._connection_state = "connected" if pool is not None else "disconnected"
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
    mgr._needs_pool_reset = False
    mgr._loop = None
    mgr._loop_thread = None
    mgr._loop_lock = threading.Lock()
    mgr._loop_started = threading.Event()
    mgr._loop_failed = None
    mgr._stats = _PoolStats()
    return mgr


def _make_healthy_mock_pool() -> MagicMock:
    """Build a pool that returns a fake connection with version + ext info."""
    pool = MagicMock(name="asyncpg_pool")
    pool.get_size.return_value = 1
    pool.get_idle_size.return_value = 1
    pool.get_min_size.return_value = 0
    pool.get_max_size.return_value = 5

    fake_conn = MagicMock(name="asyncpg_conn")
    fake_conn.fetchval = AsyncMock(return_value="PostgreSQL 16.1 on x86_64")
    fake_conn.fetchrow = AsyncMock(return_value={"extversion": "0.7.4"})

    cm = MagicMock(name="acquire_cm")
    cm.__aenter__ = AsyncMock(return_value=fake_conn)
    cm.__aexit__ = AsyncMock(return_value=None)
    pool.acquire = MagicMock(return_value=cm)
    return pool


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_check_triggers_lazy_init_when_pool_is_none():
    """The /health signal must reflect real state, not lazy state.

    When a service constructs a DatabaseManager at startup but has
    not yet issued a query, ``self.pool`` is still None. A naive
    health check would report ``disconnected`` — which is
    misleading because the pool would connect fine on first use.
    The fix: health_check() calls ``ensure_connected()`` first, so
    the probe sees a real pool and reports ``healthy``.
    """
    mgr = _make_manager_with_mock_pool(pool=None)
    assert mgr.pool is None, "precondition: pool is None before any query"

    healthy_pool = _make_healthy_mock_pool()

    async def _fake_ensure_connected() -> None:
        # Simulate the real ensure_connected(): connect sets self.pool.
        mgr.pool = healthy_pool

    with patch.object(
        DatabaseManager,
        "ensure_connected",
        new=AsyncMock(side_effect=_fake_ensure_connected),
    ) as mock_ensure:
        result = await mgr.health_check()

    # The lazy-init path must have been taken.
    mock_ensure.assert_awaited_once()
    assert mgr.pool is healthy_pool, "ensure_connected() must populate the pool"

    # And the result is healthy — NOT the misleading "disconnected".
    assert result["status"] == "healthy"
    assert result["version"] == "PostgreSQL 16.1 on x86_64"
    assert result["pgvector"] == "0.7.4"


@pytest.mark.asyncio
async def test_health_check_reports_unhealthy_when_ensure_connected_raises():
    """A real network/auth failure during lazy init must surface as
    ``unhealthy`` (with an error), not ``disconnected``. The
    operator needs to know the difference: "pool would init fine
    on first use" vs. "pool is genuinely unreachable".
    """
    mgr = _make_manager_with_mock_pool(pool=None)
    assert mgr.pool is None

    with patch.object(
        DatabaseManager,
        "ensure_connected",
        new=AsyncMock(side_effect=RuntimeError("connection refused")),
    ):
        result = await mgr.health_check()

    assert result["status"] == "unhealthy"
    assert "connect failed" in result["error"]
    assert "connection refused" in result["error"]


@pytest.mark.asyncio
async def test_health_check_still_disconnected_when_ensure_connected_does_not_init_pool():
    """Defensive: if ``ensure_connected()`` returns without raising
    but the pool is still None (e.g. backend that defers pool
    creation, or a mocked test path), the second ``pool is None``
    check fires and we report ``disconnected`` — exactly as before.
    """
    mgr = _make_manager_with_mock_pool(pool=None)

    async def _noop_ensure() -> None:
        # Real ensure_connected was bypassed; pool stays None.
        return None

    with patch.object(
        DatabaseManager, "ensure_connected", new=AsyncMock(side_effect=_noop_ensure)
    ):
        result = await mgr.health_check()

    assert result["status"] == "disconnected"
    assert "No active connection pool" in result["message"]


@pytest.mark.asyncio
async def test_health_check_skips_ensure_connected_when_pool_already_set():
    """Regression guard: the lazy-init trigger is gated on
    ``self.pool is None``. Once the pool is up, we must NOT call
    ``ensure_connected()`` again on every health check (that would
    add latency, and could disturb a healthy running pool).
    """
    healthy_pool = _make_healthy_mock_pool()
    mgr = _make_manager_with_mock_pool(pool=healthy_pool)

    with patch.object(
        DatabaseManager, "ensure_connected", new=AsyncMock()
    ) as mock_ensure:
        result = await mgr.health_check()

    mock_ensure.assert_not_called()
    assert result["status"] == "healthy"
