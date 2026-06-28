"""Stress-test-local pytest configuration.

Overrides the ``backend`` fixture from the parent ``tests/conftest.py``
with a module-scoped version. The stress test's
``tests/stress/test_concurrent_cancellation.py::db_manager`` fixture is
``scope="module"`` and depends on ``backend``; pytest refuses to satisfy
a module-scoped fixture with a function-scoped dependency, raising:

    ScopeMismatch: You tried to access the function scoped fixture
    backend with a module scoped request object.

The parent conftest defines ``backend`` as a function-scoped fixture
(default scope) because non-stress tests run with the default
per-function isolation. We cannot change the parent fixture's scope
without affecting those tests; the cleanest fix is a stress-test-local
override.

Tests under ``tests/stress/`` will use this module-scoped ``backend``;
tests elsewhere will continue to use the function-scoped one from
``tests/conftest.py``.
"""

from __future__ import annotations

import pytest


@pytest.fixture(scope="module", params=["asyncpg", "psycopg3"])
def backend(request):
    """Module-scoped backend fixture (stress-test-local override).

    Mirrors the parent conftest's parametrization so the test class's
    @pytest.mark.parametrize("backend", ["asyncpg", "psycopg3"]) is
    effectively shadowed — but the parametrize list is kept in sync
    here so the override is self-contained.

    For non-stress tests, the function-scoped fixture in
    tests/conftest.py still applies (pytest fixture lookup walks up
    the conftest chain; this file's overrides take precedence for
    tests under tests/stress/).
    """
    return request.param
