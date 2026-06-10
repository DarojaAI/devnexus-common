"""Database Connection Module for PostgreSQL with pgvector support.

VPC-agnostic: accepts any host via POSTGRES_HOST (or constructor argument).
No VPC connector logic — just standard TCP to the provided host.
"""

import os
import time
import asyncpg
import logging
import asyncio
import ssl
from collections import deque
from typing import Optional, List, Dict, Any, Tuple
import threading
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)


class _PoolStats:
    """In-memory pool saturation metrics for observability (issue #687).

    Tracks acquire counts, wait times, and failures so callers can
    detect pool starvation before it manifests as user-visible slowness.
    The samples ring buffer is bounded to keep memory predictable in
    long-running processes.
    """

    # Number of recent wait-time samples kept for percentile estimation.
    # 1024 is enough for stable p99 over 5-minute windows at typical
    # QPS without unbounded growth.
    _SAMPLE_CAP = 1024

    def __init__(self) -> None:
        self.acquire_total: int = 0
        self.acquire_failed_total: int = 0
        self.acquire_wait_ms_total: float = 0.0
        # Bounded ring buffer of recent wait times (ms) for percentile
        # estimation. deque(maxlen=...) is O(1) append and discards old
        # entries automatically.
        self.recent_wait_ms: deque = deque(maxlen=self._SAMPLE_CAP)

    def record_acquire(self, wait_ms: float) -> None:
        self.acquire_total += 1
        self.acquire_wait_ms_total += wait_ms
        self.recent_wait_ms.append(wait_ms)

    def record_acquire_failure(self) -> None:
        self.acquire_failed_total += 1

    def summary(self) -> Dict[str, Any]:
        """Return a JSON-serializable summary suitable for health-check output."""
        waits = sorted(self.recent_wait_ms) if self.recent_wait_ms else []
        n = len(waits)
        if n == 0:
            p50 = p95 = p99 = 0.0
        else:
            p50 = waits[max(0, int(n * 0.50) - 1)]
            p95 = waits[max(0, int(n * 0.95) - 1)]
            p99 = waits[max(0, int(n * 0.99) - 1)]
        return {
            "acquire_total": self.acquire_total,
            "acquire_failed_total": self.acquire_failed_total,
            "acquire_wait_ms_avg": (
                self.acquire_wait_ms_total / self.acquire_total
                if self.acquire_total
                else 0.0
            ),
            "acquire_wait_ms_p50": p50,
            "acquire_wait_ms_p95": p95,
            "acquire_wait_ms_p99": p99,
            "sample_count": n,
        }


class DatabaseManager:
    """Manages PostgreSQL database connections with pgvector support."""

    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        database: Optional[str] = None,
        user: Optional[str] = None,
        password: Optional[str] = None,
        min_size: Optional[int] = None,
        max_size: Optional[int] = None,
        application_name: Optional[str] = None,
        search_path: Optional[str] = None,
    ):
        """
        Initialize database manager.

        Args:
            host: PostgreSQL host (defaults to POSTGRES_HOST env var).
            port: PostgreSQL port (defaults to POSTGRES_PORT env var).
            database: Database name (defaults to POSTGRES_DB env var).
            user: Database user (defaults to POSTGRES_USER env var).
            password: Database password (defaults to POSTGRES_PASSWORD env var).
            min_size: Minimum pool size.
            max_size: Maximum pool size.
            application_name: Connection application_name (defaults to POSTGRES_APP_NAME env var).
            search_path: Schema search path (defaults to POSTGRES_SEARCH_PATH env var).
        """
        self.host = (host or os.getenv("POSTGRES_HOST", "localhost")).strip()
        # port is typed `Optional[int]`; coerce str→int so callers can pass either.
        # The previous `port_str.strip()` raised AttributeError on int input.
        if port is not None:
            self.port = int(port)
        else:
            self.port = int(os.getenv("POSTGRES_PORT", "5432"))
        self.database = (database or os.getenv("POSTGRES_DB", "devnexus")).strip()
        self.user = (user or os.getenv("POSTGRES_USER", "devnexus")).strip()
        self.password = password or os.getenv("POSTGRES_PASSWORD", "")

        # Validate host - reject values with invalid characters
        if "\n" in self.host or "\r" in self.host:
            raise ValueError(
                f"POSTGRES_HOST contains invalid characters (newline): {repr(self.host)}"
            )
        if not self.host or self.host == "localhost":
            raise ValueError(f"POSTGRES_HOST is not configured: {self.host}")

        # Pool sizing: keep the original 0/2-by-env default. The previous
        # min_size=2 default amplified a concurrency bug: the DatabaseManager's
        # disconnect()/connect() path is NOT safe for concurrent callers, and
        # eager connection init increased the chance of two coroutines racing
        # for the pool at startup. Reverted to the original sizing — the
        # natural-asyncpg pattern (lazy connect + let the pool recycle dead
        # connections) is the correct way to handle this.
        env = os.getenv("ENVIRONMENT", "dev").lower()
        # Use `is not None` (not `or`) so explicit min_size=0 is honored
        # as the lazy-connect opt-out.
        self.min_size = (
            min_size if min_size is not None else (2 if env == "prod" else 0)
        )
        self.max_size = max_size or (10 if env == "prod" else 5)
        self.pool: Optional[asyncpg.Pool] = None

        # Connection state tracking: "disconnected" | "initializing" | "connected" | "failed"
        self._connection_state = "disconnected"
        self._connection_error: Optional[str] = None

        # Pool saturation metrics (issue #687). Acquire wait times and
        # failure counts are accumulated here and exposed via
        # health_check() so the operator sees pool starvation before it
        # becomes user-visible. The data is per-process (not global);
        # multiple DatabaseManager instances have independent counters.
        self._stats = _PoolStats()

        # Check if PostgreSQL should be used
        # Default to enabled: the facade exists to provide PG access; callers
        # who need to opt out set USE_POSTGRESQL=false explicitly. The previous
        # default of "false" silently disabled every downstream that didn't
        # remember to set the flag — first caught when rag_research_tool's
        # wiki /db-status returned "PostgreSQL is disabled" after migrate.
        self.enabled = os.getenv("USE_POSTGRESQL", "true").lower() == "true"

        self._application_name = (
            application_name or os.getenv("POSTGRES_APP_NAME", "devnexus-common")
        ).strip()
        self._search_path = (
            search_path or os.getenv("POSTGRES_SEARCH_PATH", "public")
        ).strip()

        # Statement timeout (ms) — applied as a session setting (statement_timeout)
        # AND as asyncpg's per-operation command_timeout (seconds). Both
        # bound the runtime of any single query. The value is
        # POSTGRES_STATEMENT_TIMEOUT_MS (default 30000 = 30s), tunable
        # per-tool for batch jobs that legitimately need longer. Issue #686.
        # Stored as int ms internally; converted to float seconds for
        # command_timeout at call time.
        self.statement_timeout_ms = int(
            os.getenv("POSTGRES_STATEMENT_TIMEOUT_MS", "30000")
        )

        # Dedicated event loop for the sync facade (issues #7/#9).
        # asyncpg's pool is bound to whichever loop is running when the pool
        # is created. If we use asyncio.run(coro) on every _run_sync() call,
        # a new loop is created per call and asyncpg's internal asyncio
        # primitives become stale relative to the new loop — when
        # CancelledError fires, asyncpg's cleanup path interacts with
        # primitives bound to the OLD loop, leading to SIGSEGV in the C
        # protocol code. Production hit this on rag_research_tool's wiki
        # revisions -00078-bsc and -00079-j4m.
        #
        # Fix: all sync-facade calls run on a single dedicated event loop
        # in a daemon thread, owned by this DatabaseManager instance. The
        # asyncpg pool is then bound to one stable loop for its lifetime,
        # which is what asyncpg expects.
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None
        self._loop_lock = threading.Lock()
        self._loop_started = threading.Event()
        self._loop_failed: Optional[BaseException] = None

        # Cancellation safety (issue #7): when an in-flight asyncpg op is
        # cancelled, asyncpg's pool/connection can be left in a half-state.
        # We flag a deferred reset; the next non-cancelled ensure_connected
        # observes the flag and rebuilds the pool cleanly (deferred =
        # not racing with active acquirers).
        self._needs_pool_reset: bool = False

    async def connect(self) -> None:
        """Establish connection pool to PostgreSQL."""
        if not self.enabled:
            logger.info("PostgreSQL is disabled (USE_POSTGRESQL=false)")
            self._connection_state = "disconnected"
            return

        if self.pool is not None:
            logger.warning("Database pool already exists")
            return

        self._connection_state = "initializing"
        self._connection_error = None

        max_attempts = 6
        delay = 1

        # Configure SSL behaviour from environment
        ssl_mode = os.getenv("POSTGRES_SSLMODE", "disable").lower()
        ssl_no_verify = os.getenv("POSTGRES_SSL_NO_VERIFY", "false").lower() in (
            "1",
            "true",
            "yes",
        )
        ssl_arg = None
        if ssl_mode in ("disable", "false", "0"):
            ssl_arg = False
        elif ssl_mode in ("require", "true", "1"):
            if ssl_no_verify:
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                ssl_arg = ctx
            else:
                ssl_arg = ssl.create_default_context()
        else:
            ssl_arg = None

        for attempt in range(1, max_attempts + 1):
            try:
                logger.info(
                    f"Connecting to PostgreSQL at {self.host}:{self.port}/{self.database} "
                    f"(attempt {attempt}/{max_attempts})"
                )
                logger.info(f"asyncpg ssl argument: {ssl_arg!r}")

                try:
                    self.pool = await asyncio.wait_for(
                        asyncpg.create_pool(
                            host=self.host,
                            port=self.port,
                            database=self.database,
                            user=self.user,
                            password=self.password,
                            ssl=ssl_arg,
                            min_size=self.min_size,
                            max_size=self.max_size,
                            command_timeout=self.statement_timeout_ms / 1000.0,
                            max_inactive_connection_lifetime=300,
                            max_queries=1000,
                            server_settings={
                                "application_name": self._application_name,
                                "search_path": self._search_path,
                                "statement_timeout": str(self.statement_timeout_ms),
                            },
                        ),
                        timeout=10,
                    )
                except Exception as e:
                    err_text = str(e).lower()
                    logger.error(f"asyncpg.create_pool failed (ssl={ssl_arg!r}): {e}")
                    if ssl_arg not in (False,) and (
                        "ssl" in err_text
                        or "certificate" in err_text
                        or "tls" in err_text
                    ):
                        logger.warning(
                            "Detected SSL-related failure, retrying once with ssl=False"
                        )
                        try:
                            self.pool = await asyncio.wait_for(
                                asyncpg.create_pool(
                                    host=self.host,
                                    port=self.port,
                                    database=self.database,
                                    user=self.user,
                                    password=self.password,
                                    ssl=False,
                                    min_size=self.min_size,
                                    max_size=self.max_size,
                                    command_timeout=self.statement_timeout_ms / 1000.0,
                                    max_inactive_connection_lifetime=300,
                                    max_queries=1000,
                                    server_settings={
                                        "application_name": self._application_name,
                                        "search_path": self._search_path,
                                        "statement_timeout": str(
                                            self.statement_timeout_ms
                                        ),
                                    },
                                ),
                                timeout=10,
                            )
                        except Exception as e2:
                            logger.error(f"Fallback (ssl=False) also failed: {e2}")
                            raise
                    else:
                        raise

                # Verify pgvector extension is available
                async with self.pool.acquire() as conn:
                    result = await conn.fetchrow(
                        "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
                    )
                    if result:
                        logger.info(
                            f"pgvector extension detected: v{result['extversion']}"
                        )
                    else:
                        logger.warning("pgvector extension not found")

                logger.info("Database connection pool established")
                self._connection_state = "connected"
                self._connection_error = None
                return

            except Exception as e:
                logger.error(
                    f"Failed to connect to PostgreSQL (attempt {attempt}): {e}"
                )
                try:
                    if self.pool is not None:
                        await self.pool.close()
                        self.pool = None
                except Exception:
                    pass

                if attempt == max_attempts:
                    logger.error(
                        "Exceeded max connection attempts to PostgreSQL — giving up"
                    )
                    self._connection_state = "failed"
                    self._connection_error = str(e)
                    raise

                logger.info(f"Retrying in {delay}s...")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

    async def disconnect(self) -> None:
        """Close connection pool."""
        if self.pool is not None:
            await self.pool.close()
            self.pool = None
            logger.info("Database connection pool closed")

    async def ensure_connected(self) -> None:
        """Ensure database is connected, connecting if needed.

        Consumes the deferred-reset flag set by _with_retry on cancellation
        (issue #7). The flag is processed in a non-racing context — never
        inside the cancellation path itself, where it would race with
        active acquirers. See _with_retry for the full rationale.
        """
        if not self.enabled:
            raise RuntimeError("PostgreSQL is disabled")

        # Issue #7: if a previous call was cancelled mid-flight, the pool
        # may be in a half-state. We do NOT disconnect+reconnect inside
        # the cancellation path (that races with active acquirers — the
        # original segfault pattern). Instead, the next non-cancelled
        # ensure_connected() observes the flag, disconnects cleanly, and
        # reconnects.
        if self._needs_pool_reset and self.pool is not None:
            logger.warning("Pool marked for reset (post-cancellation); rebuilding")
            self._needs_pool_reset = False
            try:
                await self.disconnect()
            except Exception as e:
                logger.warning(f"Pool reset disconnect failed (continuing): {e}")
            # Defensive: always clear pool after a reset attempt. The real
            # disconnect() sets pool=None on success, but mocks in tests
            # don't, and a buggy disconnect could leave it set. Either
            # way, fall through to the reconnect path below.
            self.pool = None

        if self.pool is None:
            logger.debug("Database pool not initialized, connecting now...")
            await self.connect()
            return

    async def _with_retry(self, coro_factory, max_retries: int = 2):
        """Execute coroutine with transient error retry.

        IMPORTANT: do NOT call disconnect()/connect() inside this loop. A
        previous version did `await self.disconnect()` on transient error
        to force a fresh pool. That pattern is unsafe for concurrent
        callers: when request A is in the middle of disconnect() (closing
        the pool), request B's acquire() runs against a closing pool and
        asyncpg segfaults. Production hit this on rag_research_tool's wiki
        revision -00078-bsc.

        The natural asyncpg pattern is to retry the operation. When a
        connection dies, the pool marks it as broken and the next
        acquire() returns a different (or freshly created) connection.
        """
        for attempt in range(max_retries + 1):
            try:
                return await coro_factory()
            except (asyncpg.ConnectionDoesNotExistError, asyncpg.InterfaceError) as e:
                if attempt == max_retries:
                    raise
                logger.warning(
                    f"DB transient error, retrying ({attempt + 1}/{max_retries}): {e}"
                )
                await asyncio.sleep(0.5 * (2**attempt))
            except asyncio.CancelledError:
                # Issue #7: cancellation safety. When the in-flight op is
                # cancelled, asyncpg's pool/connection can be left in a
                # half-state. We do NOT try to disconnect/reconnect here
                # (that races with active acquirers — the original
                # segfault pattern). Instead, flag a deferred reset; the
                # next non-cancelled ensure_connected() will rebuild the
                # pool cleanly. We must re-raise CancelledError so the
                # caller's cancellation contract is preserved.
                self._needs_pool_reset = True
                logger.debug("DB op cancelled; pool marked for deferred reset")
                raise

    @asynccontextmanager
    async def acquire(self):
        """Context manager for acquiring a connection from pool.

        Times the pool acquire wait and records it (and any failure) on
        the _PoolStats for issue #687 observability. The timing covers
        ONLY the time spent waiting for a connection in the pool's
        internal queue -- not the time the user code holds the
        connection.
        """
        if not self.enabled or self.pool is None:
            raise RuntimeError("Database not connected")

        start = time.monotonic()
        failed = True
        try:
            pool_ctx = self.pool.acquire()
            connection = await pool_ctx.__aenter__()
            failed = False
        finally:
            wait_ms = (time.monotonic() - start) * 1000.0
            if failed:
                self._stats.record_acquire_failure()
            else:
                self._stats.record_acquire(wait_ms)

        try:
            yield connection
        finally:
            await pool_ctx.__aexit__(None, None, None)

    async def health_check(self) -> Dict[str, Any]:
        """Check database health."""
        if not self.enabled:
            return {"status": "disabled", "message": "PostgreSQL is not enabled"}

        if self.pool is None:
            return {"status": "disconnected", "message": "No active connection pool"}

        try:
            async with self.pool.acquire() as conn:
                version = await conn.fetchval("SELECT version()")
                pgvector = await conn.fetchrow(
                    "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
                )
                pool_stats = {
                    "size": self.pool.get_size(),
                    "free": self.pool.get_idle_size(),
                    "min": self.pool.get_min_size(),
                    "max": self.pool.get_max_size(),
                }
                # Merge the live pool stats (size/free/min/max from
                # asyncpg) with the accumulated saturation metrics from
                # _PoolStats (issue #687). The operator sees both at a
                # glance: live state + pressure over time.
                saturation = self._stats.summary()
                return {
                    "status": "healthy",
                    "version": version.split(",")[0],
                    "pgvector_version": pgvector["extversion"] if pgvector else None,
                    "pool": {**pool_stats, **saturation},
                    "host": self.host,
                    "database": self.database,
                }
        except Exception as e:
            logger.error(f"Health check failed: {e}")
            return {"status": "unhealthy", "error": str(e)}

    async def execute(self, query: str, *args) -> str:
        """Execute a SQL command (INSERT, UPDATE, DELETE)."""
        await self.ensure_connected()
        return await self._with_retry(lambda: self._execute_impl(query, *args))

    async def _execute_impl(self, query: str, *args) -> str:
        async with self.pool.acquire() as conn:
            return await conn.execute(query, *args)

    async def fetch(self, query: str, *args) -> List[asyncpg.Record]:
        """Fetch multiple rows."""
        await self.ensure_connected()
        return await self._with_retry(lambda: self._fetch_impl(query, *args))

    async def _fetch_impl(self, query: str, *args) -> List[asyncpg.Record]:
        async with self.pool.acquire() as conn:
            return await conn.fetch(query, *args)

    async def fetchrow(self, query: str, *args) -> Optional[asyncpg.Record]:
        """Fetch single row."""
        await self.ensure_connected()
        return await self._with_retry(lambda: self._fetchrow_impl(query, *args))

    async def _fetchrow_impl(self, query: str, *args) -> Optional[asyncpg.Record]:
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(query, *args)

    async def fetchval(self, query: str, *args) -> Any:
        """Fetch single value."""
        await self.ensure_connected()
        return await self._with_retry(lambda: self._fetchval_impl(query, *args))

    async def _fetchval_impl(self, query: str, *args) -> Any:
        async with self.pool.acquire() as conn:
            return await conn.fetchval(query, *args)

    # ============================================
    # Vector Operations (pgvector)
    # ============================================

    async def insert_pattern_with_embedding(
        self,
        repo_id: int,
        name: str,
        description: str,
        context: str,
        embedding: Optional[List[float]] = None,
    ) -> int:
        """Insert pattern with optional embedding."""
        await self.ensure_connected()
        query = """
            INSERT INTO patterns (repo_id, name, description, context, embedding)
            VALUES ($1, $2, $3, $4, $5::vector)
            ON CONFLICT (repo_id, name)
            DO UPDATE SET
                description = EXCLUDED.description,
                context = EXCLUDED.context,
                embedding = EXCLUDED.embedding
            RETURNING id
        """
        return await self.fetchval(
            query, repo_id, name, description, context, embedding
        )

    async def find_similar_patterns(
        self,
        embedding: List[float],
        limit: int = 10,
        threshold: float = 0.8,
        exclude_repo_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Find similar patterns using vector similarity."""
        query = """
            SELECT
                p.id,
                p.name,
                p.description,
                p.context,
                p.repo_id,
                r.name as repo_name,
                1 - (p.embedding <=> $1::vector) as similarity
            FROM patterns p
            JOIN repositories r ON p.repo_id = r.id
            WHERE p.embedding IS NOT NULL
                AND 1 - (p.embedding <=> $1::vector) >= $2
                AND ($3::integer IS NULL OR p.repo_id != $3)
            ORDER BY p.embedding <=> $1::vector
            LIMIT $4
        """
        rows = await self.fetch(query, embedding, threshold, exclude_repo_id, limit)
        return [
            {
                "id": row["id"],
                "name": row["name"],
                "description": row["description"],
                "context": row["context"],
                "repo_id": row["repo_id"],
                "repo_name": row["repo_name"],
                "similarity": float(row["similarity"]),
            }
            for row in rows
        ]

    async def update_pattern_embedding(
        self, pattern_id: int, embedding: List[float]
    ) -> None:
        """Update embedding for existing pattern."""
        query = "UPDATE patterns SET embedding = $1::vector WHERE id = $2"
        await self.execute(query, embedding, pattern_id)

    async def get_patterns_without_embeddings(
        self, limit: int = 100
    ) -> List[Dict[str, Any]]:
        """Get patterns that don't have embeddings yet."""
        query = """
            SELECT
                p.id,
                p.name,
                p.description,
                p.context,
                p.repo_id,
                r.name as repo_name
            FROM patterns p
            JOIN repositories r ON p.repo_id = r.id
            WHERE p.embedding IS NULL
            LIMIT $1
        """
        rows = await self.fetch(query, limit)
        return [
            {
                "id": row["id"],
                "name": row["name"],
                "description": row["description"],
                "context": row["context"],
                "repo_id": row["repo_id"],
                "repo_name": row["repo_name"],
            }
            for row in rows
        ]

    # ============================================
    # Sync facade
    # ============================================
    # Use these from SYNC contexts only (CLI scripts, sync request
    # handlers, threadpool workers). The _run_sync guard raises if
    # called from inside a running event loop — in async contexts,
    # Implementation note (issues #7/#9): we previously used
    # asyncio.run(coro) per call, creating a fresh event loop on every
    # sync facade invocation. That pattern is the root cause of the
    # production SIGSEGV: asyncpg's pool is bound to whichever loop
    # was running when the pool was created. A new loop on every call
    # meant the pool's internal asyncio primitives were stale relative
    # to the new loop, and CancelledError cleanup would deref primitives
    # bound to the OLD loop → SIGSEGV in the C protocol code.
    #
    # New pattern: all sync-facade calls run on a single dedicated event
    # loop in a daemon thread owned by this DatabaseManager. The asyncpg
    # pool is then bound to one stable loop for its lifetime, which is
    # what asyncpg expects. concurrent.futures.Future.cancel() correctly
    # propagates to the underlying asyncio task on the dedicated loop,
    # giving us proper cancellation semantics (the missing primitive
    # that asyncio.run()-per-call couldn't provide).

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        """Lazily start the dedicated event loop in a daemon thread.

        Idempotent: subsequent calls return the same loop. Thread-safe:
        the start sequence is serialized by ``_loop_lock``. The loop is
        bound for the lifetime of the DatabaseManager; the daemon
        thread is reaped when the process exits.

        Raises:
            RuntimeError: if the loop thread fails to start within 5s
                or fails to initialize (e.g. asyncio.new_event_loop
                itself raises).
        """
        if self._loop is not None:
            return self._loop
        with self._loop_lock:
            if self._loop is not None:
                return self._loop
            self._loop_started.clear()
            self._loop_failed = None

            def _run_loop() -> None:
                loop: Optional[asyncio.AbstractEventLoop] = None
                try:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    self._loop = loop
                    self._loop_started.set()
                    loop.run_forever()
                except BaseException as e:  # noqa: BLE001
                    self._loop_failed = e
                    self._loop_started.set()
                finally:
                    if loop is not None:
                        try:
                            loop.close()
                        except Exception:
                            pass
                    self._loop = None

            self._loop_thread = threading.Thread(
                target=_run_loop,
                daemon=True,
                name=f"db-loop-{id(self):x}",
            )
            self._loop_thread.start()
            if not self._loop_started.wait(timeout=5.0):
                raise RuntimeError(
                    "DatabaseManager loop thread did not signal ready within 5s"
                )
            if self._loop_failed is not None:
                raise RuntimeError(
                    f"DatabaseManager loop thread failed to initialize: {self._loop_failed}"
                )
            if self._loop is None:
                raise RuntimeError("DatabaseManager loop is None after start")
            return self._loop

    def _run_sync(self, coro, timeout: Optional[float] = None):
        """Run a coroutine to completion on the dedicated DB loop.

        Blocks the calling thread until the coroutine completes (or
        ``timeout`` elapses, in which case the underlying asyncio task
        is cancelled via ``concurrent.futures.Future.cancel()`` and
        ``concurrent.futures.TimeoutError`` is raised). The asyncpg pool
        is bound to the dedicated loop for the lifetime of this
        DatabaseManager, so all sync-facade calls share the same loop
        and asyncpg's internal primitives stay consistent.

        Raises:
            RuntimeError: if called from within a running event loop.
                Use the async methods (execute, fetch, ...) directly
                in async contexts.
            concurrent.futures.TimeoutError: if ``timeout`` is set and
                the coroutine did not complete in time.
            Anything the coroutine raises: re-raised here.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError as e:
            if "no running event loop" in str(e):
                loop = self._ensure_loop()
                future = asyncio.run_coroutine_threadsafe(coro, loop)
                return future.result(timeout=timeout)
            raise
        raise RuntimeError(
            "DatabaseManager._run_sync called from a running event "
            "loop. Use the async methods (execute, fetch, ...) "
            "directly in async contexts."
        )

    def execute_sync(self, query: str, *args) -> str:
        """Sync wrapper for execute(). See class docstring."""
        return self._run_sync(self.execute(query, *args))

    def fetch_sync(self, query: str, *args) -> "List[asyncpg.Record]":
        """Sync wrapper for fetch(). See class docstring."""
        return self._run_sync(self.fetch(query, *args))

    def fetchrow_sync(self, query: str, *args) -> "Optional[asyncpg.Record]":
        """Sync wrapper for fetchrow(). See class docstring."""
        return self._run_sync(self.fetchrow(query, *args))

    def fetchval_sync(self, query: str, *args) -> Any:
        """Sync wrapper for fetchval(). See class docstring."""
        return self._run_sync(self.fetchval(query, *args))

    def health_check_sync(self) -> Dict[str, Any]:
        """Sync wrapper for health_check(). See class docstring."""
        return self._run_sync(self.health_check())

    # ============================================
    # Bulk insert (pgvector-aware, async — bridges via bulk_insert_sync)
    # ============================================

    async def bulk_insert(
        self,
        table: str,
        columns: List[str],
        rows: List[Tuple],
        *,
        on_conflict: Optional[str] = None,
        page_size: int = 1000,
    ) -> str:
        """Insert many rows in batches of page_size. Equivalent to
        psycopg2.extras.execute_values.

        Args:
            table: Target table name. Not quoted; caller is responsible
                for ensuring the table name is safe (e.g., not from
                untrusted user input).
            columns: List of column names. Same safety caveat as table.
            rows: Tuples of values, one per row. Each tuple's length
                must equal len(columns).
            on_conflict: Optional ON CONFLICT clause appended verbatim,
                e.g. "ON CONFLICT (id) DO UPDATE SET x = EXCLUDED.x".
            page_size: Rows per INSERT statement. asyncpg handles
                1000s of params in one call; 1000 is a safe default.

        Returns:
            The total rows-inserted string, e.g. "INSERT 0 1042".
        """
        if not rows:
            return "INSERT 0"
        n = len(columns)
        if any(len(r) != n for r in rows):
            raise ValueError(
                f"bulk_insert: every row must have exactly {n} values; "
                f"got row lengths {[len(r) for r in rows]}"
            )
        cols = ", ".join(columns)
        placeholders = ", ".join(f"${i+1}" for i in range(n))
        sql = f"INSERT INTO {table} ({cols}) VALUES ({placeholders})"
        if on_conflict:
            sql += f" {on_conflict}"
        await self.ensure_connected()
        total = 0
        from itertools import chain

        for i in range(0, len(rows), page_size):
            page = rows[i : i + page_size]
            params = list(chain.from_iterable(page))
            result = await self._with_retry(lambda: self._execute_impl(sql, *params))
            # asyncpg returns "INSERT 0 42" for inserts; the trailing
            # number is rows-inserted. Parse defensively.
            try:
                total += int(result.rsplit(" ", 1)[-1])
            except (ValueError, IndexError):
                logger.debug(f"bulk_insert: could not parse row count from {result!r}")
        return f"INSERT 0 {total}"

    def bulk_insert_sync(
        self,
        table: str,
        columns: List[str],
        rows: List[Tuple],
        *,
        on_conflict: Optional[str] = None,
        page_size: int = 1000,
    ) -> str:
        """Sync wrapper for bulk_insert()."""
        return self._run_sync(
            self.bulk_insert(
                table, columns, rows, on_conflict=on_conflict, page_size=page_size
            )
        )


# Singleton instance
_db_manager: Optional[DatabaseManager] = None


def get_db() -> DatabaseManager:
    """Get database manager singleton."""
    global _db_manager
    if _db_manager is None:
        _db_manager = DatabaseManager()
    return _db_manager


async def init_db() -> DatabaseManager:
    """Initialize database connection."""
    db = get_db()
    await db.connect()
    return db


async def close_db() -> None:
    """Close database connection."""
    db = get_db()
    await db.disconnect()


def init_db_sync() -> DatabaseManager:
    """Sync wrapper for init_db().

    Use from sync contexts (CLI scripts, sync request handlers,
    threadpool workers). For async contexts, use init_db() directly.
    """
    db = get_db()
    db._run_sync(db.connect())
    return db


def close_db_sync() -> None:
    """Sync wrapper for close_db(). See init_db_sync for usage."""
    db = get_db()
    if db.pool is not None:
        db._run_sync(db.disconnect())
