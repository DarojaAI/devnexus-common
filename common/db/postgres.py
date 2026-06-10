"""Backward-compat shim. Use ``common.db.postgres`` (or ``common.db``).

The real implementation lives in :mod:`common.db.manager`. This module
remains so existing imports like
``from common.db.postgres import DatabaseManager`` keep working
unchanged in every downstream repo.

.. note::

   This file also re-exports ``asyncpg`` at the module level
   (without using it directly) so that the existing test suite's
   ``patch("common.db.postgres.asyncpg.create_pool", ...)`` keeps
   targeting a real module attribute. The backend in
   :mod:`common.db.backends.asyncpg` imports ``asyncpg`` itself;
   because Python's import system gives every importer the same
   module object, the patch is observed by both this shim's
   attribute lookup and the backend's pool-creation call.
"""

from common.db.manager import (  # noqa: F401
    DatabaseManager,
    _db_manager,
    _PoolStats,
    close_db,
    close_db_sync,
    get_db,
    init_db,
    init_db_sync,
)
import asyncpg  # noqa: F401  -- re-exported for test compatibility

__all__ = [
    "DatabaseManager",
    "_PoolStats",
    "_db_manager",
    "get_db",
    "init_db",
    "close_db",
    "init_db_sync",
    "close_db_sync",
]
