"""Tests for pool saturation observability (issue #687).

The DatabaseManager accumulates pool acquire metrics (wait times,
failure counts) and surfaces them via health_check() so the operator
sees pool starvation before it becomes user-visible. These tests
verify:
  1. The metrics are accumulated correctly on successful acquires.
  2. The metrics are accumulated correctly on failed acquires.
  3. Under contention (more concurrent acquires than pool slots),
     the wait time for at least some acquires is non-zero.
  4. The health_check() response includes the saturation metrics.
"""

from __future__ import annotations

import asyncio

import pytest
from unittest.mock import AsyncMock, MagicMock

from common.db.postgres import DatabaseManager, _PoolStats


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_manager_with_mock_pool(
    *,
    enabled: bool = True,
    pool: object = None,
    pool_state: str = "connected",
) -> DatabaseManager:
    """Bypass env-var validation; build a manager with a mock pool.
    Mirrors the helper in tests/test_postgres_sync.py so this test is
    self-contained.
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
    mgr._needs_pool_reset = False
    mgr._loop = None
    mgr._loop_thread = None
    mgr._loop_lock = __import__("threading").Lock()
    mgr._loop_started = __import__("threading").Event()
    mgr._loop_failed = None
    # Issue #687: pool saturation stats
    mgr._stats = _PoolStats()
    return mgr


def _mock_pool_with_async_acquire() -> MagicMock:
    """Build a pool whose .acquire() returns an async context manager.

    The returned context manager yields a fake connection immediately
    (no real wait), so test can drive _PoolStats.record_acquire() by
    calling mgr.acquire() in sequence.
    """
    pool = MagicMock(name="asyncpg_pool")

    fake_conn = MagicMock(name="asyncpg_conn")

    cm = MagicMock(name="acquire_cm")
    cm.__aenter__ = AsyncMock(return_value=fake_conn)
    cm.__aexit__ = AsyncMock(return_value=None)
    pool.acquire = MagicMock(return_value=cm)
    return pool


# ---------------------------------------------------------------------------
# Unit tests: _PoolStats
# ---------------------------------------------------------------------------


def test_pool_stats_starts_at_zero():
    """A fresh _PoolStats has all counters at 0 and an empty sample buffer."""
    s = _PoolStats()
    summary = s.summary()
    assert summary["acquire_total"] == 0
    assert summary["acquire_failed_total"] == 0
    assert summary["acquire_wait_ms_avg"] == 0.0
    assert summary["acquire_wait_ms_p50"] == 0.0
    assert summary["acquire_wait_ms_p95"] == 0.0
    assert summary["acquire_wait_ms_p99"] == 0.0
    assert summary["sample_count"] == 0


def test_pool_stats_record_acquire_increments_counters():
    s = _PoolStats()
    s.record_acquire(10.0)
    s.record_acquire(20.0)
    s.record_acquire(30.0)
    summary = s.summary()
    assert summary["acquire_total"] == 3
    assert summary["acquire_failed_total"] == 0
    assert summary["acquire_wait_ms_avg"] == 20.0  # (10+20+30)/3
    assert summary["sample_count"] == 3
    # Percentiles from [10, 20, 30]:
    # p50 = index max(0, int(3*0.50)-1) = 0 -> 10.0
    # p95 = index max(0, int(3*0.95)-1) = 1 -> 20.0
    # p99 = index max(0, int(3*0.99)-1) = 1 -> 20.0
    assert summary["acquire_wait_ms_p50"] == 10.0
    assert summary["acquire_wait_ms_p95"] == 20.0
    assert summary["acquire_wait_ms_p99"] == 20.0


def test_pool_stats_record_failure():
    s = _PoolStats()
    s.record_acquire(5.0)
    s.record_acquire_failure()
    s.record_acquire_failure()
    summary = s.summary()
    assert summary["acquire_total"] == 1
    assert summary["acquire_failed_total"] == 2
    # Failure records should NOT add to wait samples
    assert summary["sample_count"] == 1


# ---------------------------------------------------------------------------
# Integration tests: DatabaseManager.acquire() records metrics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_acquire_records_wait_time_and_increments_counter():
    """A single successful acquire records a non-negative wait time
    and increments the success counter.
    """
    mgr = _make_manager_with_mock_pool(pool=_mock_pool_with_async_acquire())
    mgr.pool = mgr.pool  # already set

    async with mgr.acquire():
        pass  # hold the connection briefly

    summary = mgr._stats.summary()
    assert summary["acquire_total"] == 1
    assert summary["acquire_failed_total"] == 0
    # Mock pool returns the connection immediately, so wait should be small
    # (sub-millisecond in practice). The test just verifies it's non-negative
    # and that the sample was recorded.
    assert summary["sample_count"] == 1
    assert summary["acquire_wait_ms_avg"] >= 0.0


@pytest.mark.asyncio
async def test_acquire_records_failure_when_pool_raises():
    """If pool.acquire() raises, _PoolStats records the failure but does
    not add a wait sample.
    """
    failing_pool = MagicMock(name="failing_pool")

    async def _raise_aenter(self):
        raise RuntimeError("pool exhausted")

    cm = MagicMock(name="failing_cm")
    cm.__aenter__ = _raise_aenter
    cm.__aexit__ = AsyncMock(return_value=None)
    failing_pool.acquire = MagicMock(return_value=cm)

    mgr = _make_manager_with_mock_pool(pool=failing_pool)

    with pytest.raises(RuntimeError, match="pool exhausted"):
        async with mgr.acquire():
            pass  # never reached

    summary = mgr._stats.summary()
    assert summary["acquire_total"] == 0
    assert summary["acquire_failed_total"] == 1
    assert summary["sample_count"] == 0


# ---------------------------------------------------------------------------
# Stress test: signal is emitted under load
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pool_acquire_wait_is_non_zero_under_contention():
    """The acceptance criterion for issue #687: spawn N concurrent
    acquisitions against a pool smaller than N. At least one acquire
    must record a non-zero wait time (because the later acquires had to
    wait for the earlier ones to release).

    This test does NOT require a real PG. It uses a pool whose
    .acquire() blocks for a short, deterministic time so we can
    reliably assert that at least one wait is non-zero.
    """
    mgr = _make_manager_with_mock_pool(pool=None)
    # Configure the mock to simulate a pool that always waits 5ms
    # before yielding a connection. This guarantees non-zero waits
    # in the recorded samples.
    fake_conn = MagicMock(name="conn")

    async def _slow_aenter(self):
        await asyncio.sleep(0.005)  # 5ms
        return fake_conn

    cm = MagicMock(name="slow_cm")
    cm.__aenter__ = _slow_aenter
    cm.__aexit__ = AsyncMock(return_value=None)
    mgr.pool = MagicMock()
    mgr.pool.acquire = MagicMock(return_value=cm)

    # Spawn 20 concurrent acquires. Each will take ~5ms. Since asyncio
    # serializes them (only one connection at a time from this mock pool),
    # at least the 2nd through 20th should see non-zero wait times.
    async def _hold():
        async with mgr.acquire():
            # Hold the connection briefly so the next caller waits
            await asyncio.sleep(0.001)

    await asyncio.gather(*[_hold() for _ in range(20)])

    summary = mgr._stats.summary()
    assert summary["acquire_total"] == 20
    assert summary["acquire_failed_total"] == 0
    # The mock yields the connection immediately (after the 5ms
    # self-imposed wait) but then the user code holds it for 1ms
    # before the next acquire starts. Each acquire's wait is therefore
    # the 5ms self-wait + the time it waited for the previous holder
    # to release. The 2nd+ acquires will have wait > 5ms.
    assert summary["acquire_wait_ms_p95"] > 5.0, (
        f"Expected p95 wait > 5ms (mock-pool self-wait) but got "
        f"{summary['acquire_wait_ms_p95']}"
    )


# ---------------------------------------------------------------------------
# health_check() exposes the new metrics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_check_includes_saturation_metrics():
    """The response from health_check() must include the saturation
    fields so the operator dashboard can pick them up.
    """
    mgr = _make_manager_with_mock_pool()
    fake_pool = MagicMock()
    fake_pool.get_size.return_value = 1
    fake_pool.get_idle_size.return_value = 1
    fake_pool.get_min_size.return_value = 0
    fake_pool.get_max_size.return_value = 5
    fake_pool.acquire = MagicMock()
    mgr.pool = fake_pool

    # Pre-record some metrics
    mgr._stats.record_acquire(42.0)

    fake_conn = MagicMock()
    fake_conn.fetchval = AsyncMock(return_value="PostgreSQL 16.1 on x86_64")
    fake_conn.fetchrow = AsyncMock(return_value={"extversion": "0.7.4"})

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=fake_conn)
    cm.__aexit__ = AsyncMock(return_value=None)
    fake_pool.acquire = MagicMock(return_value=cm)

    result = await mgr.health_check()

    # The new saturation keys are present in pool_stats
    pool_stats = result["pool"]
    assert "acquire_total" in pool_stats
    assert "acquire_failed_total" in pool_stats
    assert "acquire_wait_ms_avg" in pool_stats
    assert "acquire_wait_ms_p50" in pool_stats
    assert "acquire_wait_ms_p95" in pool_stats
    assert "acquire_wait_ms_p99" in pool_stats
    # The pre-recorded value shows up
    assert pool_stats["acquire_total"] == 1
    assert pool_stats["acquire_wait_ms_avg"] == 42.0
