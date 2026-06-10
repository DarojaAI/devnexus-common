"""Database backend Protocol and configuration dataclass.

This is the contract every concrete backend (asyncpg, psycopg 3) must
satisfy. The ``DatabaseManager`` (common.db.manager) holds a backend
instance and delegates driver-specific work to it. Manager-owned
concerns (event loop, retry, cancellation, pool stats) stay on the
manager — they are backend-agnostic and must not be reimplemented per
driver.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional, Protocol, runtime_checkable


@dataclass
class BackendConfig:
    """Driver-agnostic connection + tuning configuration.

    Constructed by the ``DatabaseManager`` from its ``__init__`` kwargs
    and the ``POSTGRES_*`` environment variables, then passed to
    ``backend.connect(config)``. The backend is free to ignore fields
    it doesn't understand (e.g. ``ssl_mode``/``ssl_no_verify`` are
    asyncpg-specific), but every backend should respect
    ``host``/``port``/``database``/``user``/``password``/``min_size``/
    ``max_size``/``application_name``/``search_path``/
    ``statement_timeout_ms`` — those are the cross-driver contract.
    """

    host: str
    port: int
    database: str
    user: str
    password: str
    min_size: int
    max_size: int
    application_name: str
    search_path: str
    statement_timeout_ms: int
    ssl_mode: str
    ssl_no_verify: bool


@runtime_checkable
class DatabaseBackend(Protocol):
    """Async database backend interface.

    Implemented by ``AsyncpgBackend`` (common.db.backends.asyncpg) and
    ``Psycopg3Backend`` (issue #28). The Protocol is ``runtime_checkable``
    so tests and tooling can do ``isinstance(obj, DatabaseBackend)``.

    Lifecycle:
      - ``connect(config)`` opens the pool. Idempotent: a second call
        while the pool is alive is a no-op (the manager also guards
        against double-connect).
      - ``disconnect()`` closes the pool. Safe to call when already
        disconnected.
      - All other methods assume the pool is alive. The manager's
        ``ensure_connected()`` is the canonical "make sure I'm up"
        entry point.

    The four "patterns" methods at the bottom are the only
    domain-specific methods on the Protocol. They exist because
    rag_research_tool uses them and we want the backend to own the
    driver-specific SQL (asyncpg's ``$1::vector`` cast vs psycopg 3's
    ``%s::vector``). For Phase 1 the manager delegates to them; for
    Phase 2 the manager can stop exposing them and let callers go
    through the backend directly if desired.
    """

    @property
    def name(self) -> str:
        """Short identifier for the backend: ``"asyncpg"`` or ``"psycopg3"``."""
        ...

    async def connect(self, config: "BackendConfig") -> None:
        """Open the connection pool. Backend-specific implementation."""
        ...

    async def disconnect(self) -> None:
        """Close the connection pool. Safe to call when already closed."""
        ...

    async def health_check(self) -> dict:
        """Return backend health information.

        Returned dict should at minimum include ``status`` (one of
        ``"healthy"``, ``"unhealthy"``, ``"disabled"``,
        ``"disconnected"``). The manager layers on its pool-stats
        metrics (``acquire_total``, ``acquire_wait_ms_*``) on top of
        whatever the backend returns.
        """
        ...

    def acquire(self):
        """Return an async context manager yielding a backend connection.

        The manager wraps this with pool-stats timing (issue #687).
        Backends should return a fresh async context manager per call
        (asyncpg's ``pool.acquire()`` does this naturally).
        """
        ...

    async def execute(self, query: str, *args: Any) -> str:
        """Execute a SQL command (INSERT/UPDATE/DELETE). Return the status string."""
        ...

    async def fetch(self, query: str, *args: Any) -> List[Any]:
        """Fetch multiple rows. Return a list of backend-specific row objects."""
        ...

    async def fetchrow(self, query: str, *args: Any) -> Optional[Any]:
        """Fetch a single row, or ``None`` if no rows match."""
        ...

    async def fetchval(self, query: str, *args: Any) -> Any:
        """Fetch a single scalar value, or ``None`` if no rows match."""
        ...

    async def executemany(self, query: str, args: List[Any]) -> None:
        """Execute ``query`` once per row in ``args`` (psycopg-style batch)."""
        ...

    # ------------------------------------------------------------------
    # Patterns helpers (rag_research_tool). Backend-specific SQL lives
    # here so the manager stays a thin delegator. Both backends must
    # implement the same four methods with the same semantics; only
    # the SQL syntax ($1 vs %s) and vector-cast syntax differ.
    # ------------------------------------------------------------------

    async def insert_pattern_with_embedding(self, **kwargs: Any) -> Any:
        """Insert a pattern with optional embedding. Return the new id."""
        ...

    async def find_similar_patterns(self, **kwargs: Any) -> List[dict]:
        """Find patterns whose embedding is similar to the given vector."""
        ...

    async def update_pattern_embedding(self, **kwargs: Any) -> Any:
        """Backfill the embedding for an existing pattern."""
        ...

    async def get_patterns_without_embeddings(self, **kwargs: Any) -> List[dict]:
        """Return patterns whose embedding column is NULL."""
        ...
