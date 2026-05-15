"""Database Connection Module for PostgreSQL with pgvector support.

VPC-agnostic: accepts any host via POSTGRES_HOST (or constructor argument).
No VPC connector logic — just standard TCP to the provided host.
"""

import os
import asyncpg
import logging
import asyncio
import ssl
from typing import Optional, List, Dict, Any
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)


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
        port_str = (port or os.getenv("POSTGRES_PORT") or "5432")
        self.port = int(port_str.strip()) if port_str and port_str.strip() else 5432
        self.database = (database or os.getenv("POSTGRES_DB", "devnexus")).strip()
        self.user = (user or os.getenv("POSTGRES_USER", "devnexus")).strip()
        self.password = password or os.getenv("POSTGRES_PASSWORD", "")

        # Validate host - reject values with invalid characters
        if "\n" in self.host or "\r" in self.host:
            raise ValueError(f"POSTGRES_HOST contains invalid characters (newline): {repr(self.host)}")
        if not self.host or self.host == "localhost":
            raise ValueError(f"POSTGRES_HOST is not configured: {self.host}")

        # Production-aware pool sizing
        env = os.getenv("ENVIRONMENT", "dev").lower()
        self.min_size = min_size or (2 if env == "prod" else 0)
        self.max_size = max_size or (10 if env == "prod" else 5)
        self.pool: Optional[asyncpg.Pool] = None

        # Connection state tracking: "disconnected" | "initializing" | "connected" | "failed"
        self._connection_state = "disconnected"
        self._connection_error: Optional[str] = None

        # Check if PostgreSQL should be used
        self.enabled = os.getenv("USE_POSTGRESQL", "false").lower() == "true"

        self._application_name = (application_name or os.getenv("POSTGRES_APP_NAME", "devnexus-common")).strip()
        self._search_path = (search_path or os.getenv("POSTGRES_SEARCH_PATH", "public")).strip()

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
        ssl_no_verify = os.getenv("POSTGRES_SSL_NO_VERIFY", "false").lower() in ("1", "true", "yes")
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
                            command_timeout=30,
                            max_inactive_connection_lifetime=300,
                            max_queries=1000,
                            server_settings={
                                "application_name": self._application_name,
                                "search_path": self._search_path,
                                "statement_timeout": "30000",
                            },
                        ),
                        timeout=10,
                    )
                except Exception as e:
                    err_text = str(e).lower()
                    logger.error(f"asyncpg.create_pool failed (ssl={ssl_arg!r}): {e}")
                    if ssl_arg not in (False,) and (
                        "ssl" in err_text or "certificate" in err_text or "tls" in err_text
                    ):
                        logger.warning("Detected SSL-related failure, retrying once with ssl=False")
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
                                    command_timeout=30,
                                    max_inactive_connection_lifetime=300,
                                    max_queries=1000,
                                    server_settings={
                                        "application_name": self._application_name,
                                        "search_path": self._search_path,
                                        "statement_timeout": "30000",
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
                        logger.info(f"pgvector extension detected: v{result['extversion']}")
                    else:
                        logger.warning("pgvector extension not found")

                logger.info("Database connection pool established")
                self._connection_state = "connected"
                self._connection_error = None
                return

            except Exception as e:
                logger.error(f"Failed to connect to PostgreSQL (attempt {attempt}): {e}")
                try:
                    if self.pool is not None:
                        await self.pool.close()
                        self.pool = None
                except Exception:
                    pass

                if attempt == max_attempts:
                    logger.error("Exceeded max connection attempts to PostgreSQL — giving up")
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
        """Ensure database is connected, connecting if needed."""
        if not self.enabled:
            raise RuntimeError("PostgreSQL is disabled")

        if self.pool is None:
            logger.debug("Database pool not initialized, connecting now...")
            await self.connect()
            return

        # Liveness probe: detect stale pool where all connections are dead
        try:
            async with self.pool.acquire(timeout=2) as conn:
                await conn.execute("SELECT 1")
        except (asyncpg.ConnectionDoesNotExistError, asyncpg.InterfaceError, asyncio.TimeoutError) as e:
            logger.warning(f"DB pool stale — forcing reconnect: {e}")
            await self.disconnect()
            await self.connect()

    async def _with_retry(self, coro_factory, max_retries: int = 2):
        """Execute coroutine with transient error retry."""
        for attempt in range(max_retries + 1):
            try:
                return await coro_factory()
            except (asyncpg.ConnectionDoesNotExistError, asyncpg.InterfaceError) as e:
                if attempt == max_retries:
                    raise
                logger.warning(f"DB transient error, retrying ({attempt + 1}/{max_retries}): {e}")
                await self.disconnect()
                await asyncio.sleep(0.5 * (2 ** attempt))
                try:
                    await self.connect()
                except Exception as connect_err:
                    logger.error(f"Reconnect failed during retry attempt {attempt + 1}: {connect_err}")
                    raise

    @asynccontextmanager
    async def acquire(self):
        """Context manager for acquiring a connection from pool."""
        if not self.enabled or self.pool is None:
            raise RuntimeError("Database not connected")

        async with self.pool.acquire() as connection:
            yield connection

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
                return {
                    "status": "healthy",
                    "version": version.split(",")[0],
                    "pgvector_version": pgvector["extversion"] if pgvector else None,
                    "pool": pool_stats,
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
        return await self.fetchval(query, repo_id, name, description, context, embedding)

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

    async def update_pattern_embedding(self, pattern_id: int, embedding: List[float]) -> None:
        """Update embedding for existing pattern."""
        query = "UPDATE patterns SET embedding = $1::vector WHERE id = $2"
        await self.execute(query, embedding, pattern_id)

    async def get_patterns_without_embeddings(self, limit: int = 100) -> List[Dict[str, Any]]:
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
