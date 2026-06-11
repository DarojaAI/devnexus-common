# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [1.7.0] - 2026-06-11

### Added

- **psycopg 3 backend** (`DatabaseManager(backend="psycopg3", ...)`). Pure-Python wrapper around libpq. Opt-in via `pip install "devnexus-common[psycopg3]"`. Implements the same `DatabaseBackend` Protocol as the asyncpg backend, shares the same public API, same dedicated event loop, same cancellation-safety machinery. Issue #28, #29.
- **`psycopg3` and `pgvector` optional-dependency groups** in `pyproject.toml`. Install the psycopg 3 backend with `pip install "devnexus-common[psycopg3]"`; full pgvector support with `pip install "devnexus-common[psycopg3,pgvector]"`.
- **Real-PG stress test parametrized against both backends.** The concurrent-cancellation stress test now runs against both `asyncpg` and `psycopg3` via two jobs in `.github/workflows/devnexus-common-stress.yml`. Issue #29.

### Test coverage

- 6 existing basic-query-helper tests parametrized against both backends (issue #29)
- 9 new dispatcher-specific tests in `TestBackendDispatch` (issue #29)
- 2 new tests verifying the translator is applied for psycopg 3 only (issue #29)
- 1 new stress test parametrized against both backends (issue #29)
- 1 new CI workflow job for the psycopg 3 stress test (issue #29)

### Total tests

- 128 (pre-parametrization) → 165+ (post-parametrization, since basic-query tests run 2x)

### Notes

- **asyncpg remains the default.** Existing deployments are unaffected.
- The psycopg 3 backend is **opt-in** for v1.7.0. It will become the default in v2.0 after 1 quarter of production soak.
- Both backends can be installed side-by-side if needed: `pip install "devnexus-common[psycopg3]"` adds psycopg 3 to an asyncpg-using environment.
