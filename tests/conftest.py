"""Pytest configuration and shared fixtures for the devnexus-common test suite."""

import sys
from pathlib import Path

import pytest

repo_root = Path(__file__).parent.parent.resolve()
root_str = str(repo_root)
if root_str not in sys.path:
    sys.path.insert(0, root_str)


@pytest.fixture(params=["asyncpg", "psycopg3"])
def backend(request):
    """Parametrized fixture: each test runs against both backends.

    Use this fixture to ensure a test exercises BOTH the asyncpg
    and psycopg 3 dispatch paths introduced in issue #28. The
    fixture value is a string: either ``"asyncpg"`` or
    ``"psycopg3"``.

    For backend-specific tests, gate execution with
    ``pytest.skip(...)`` so the test only runs against the relevant
    backend (otherwise you'd be asserting on the wrong path).

    See ``tests/test_postgres_sync.py::TestBackendDispatch`` for the
    canonical usage pattern.
    """
    return request.param
