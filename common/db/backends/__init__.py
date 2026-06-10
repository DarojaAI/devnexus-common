"""Database backend implementations.

Each backend implements the ``DatabaseBackend`` Protocol defined in
``common.db.backends.base``. The current set is:

  - ``AsyncpgBackend`` (common.db.backends.asyncpg) — asyncpg driver
  - ``Psycopg3Backend`` (issue #28) — psycopg 3 driver

The ``DatabaseManager`` (common.db.manager) is a backend-agnostic facade
that owns the event loop, the retry/cancellation logic, and the pool
saturation metrics. It delegates driver-specific work to the configured
backend.
"""

from common.db.backends.base import BackendConfig, DatabaseBackend

__all__ = ["BackendConfig", "DatabaseBackend"]
