# devnexus-common

Shared Python utilities for DarojaAI projects.

## Installation

```bash
pip install -e .
```

With tracing extras:
```bash
pip install -e ".[tracing]"
```

## Modules

### `common.llm` — LLM client

Unified LLM client supporting Anthropic Claude and OpenRouter (with easy extensibility for other providers).

```python
from common.llm import get_llm_client_from_config, LLMClient

# From config object (with llm_provider, anthropic_api_key, etc.)
client = get_llm_client_from_config(config)

# Or directly
from common.llm import get_llm_client
client = get_llm_client("anthropic", api_key="sk-ant-...")

response = client.create_message(
    model="claude-3-5-sonnet-20241022",
    messages=[{"role": "user", "content": "Hello"}],
)
print(response.content)
print(response.usage)  # {"input_tokens": 10, "output_tokens": 50}
```

**Environment variables:**
- `ANTHROPIC_API_KEY` — for Anthropic provider
- `OPENROUTER_API_KEY` — for OpenRouter provider

### `common.db.postgres` — PostgreSQL client

VPC-agnostic async PostgreSQL client with pgvector support. Connects via standard TCP using `POSTGRES_HOST` (or any host passed to the constructor). No VPC connector logic.

```python
from common.db import DatabaseManager, init_db, close_db

# Using env vars: POSTGRES_HOST, POSTGRES_PORT, POSTGRES_DB, POSTGRES_USER, POSTGRES_PASSWORD
db = DatabaseManager()
await db.connect()

# Or explicitly
db = DatabaseManager(host="10.0.0.3", database="mydb")
await db.connect()

# Query
rows = await db.fetch("SELECT id, name FROM users WHERE active = $1", True)
```

**Environment variables:**
- `POSTGRES_HOST` — required
- `POSTGRES_PORT` — default `5432`
- `POSTGRES_DB` — default `devnexus`
- `POSTGRES_USER` — default `devnexus`
- `POSTGRES_PASSWORD` — default `""`
- `POSTGRES_SSLMODE` — `disable` | `require`
- `POSTGRES_SSL_NO_VERIFY` — `true` to skip cert verification
- `POSTGRES_APP_NAME` — default `devnexus-common`
- `POSTGRES_SEARCH_PATH` — default `public`
- `USE_POSTGRESQL` — default `true`; set to `false` to disable the client (skip connection on `connect()` and raise on `ensure_connected()`)
- `POSTGRES_STATEMENT_TIMEOUT_MS` — default `30000` (30s). Applied centrally as a `statement_timeout` server setting on every acquired session so a single runaway query cannot starve the shared pool (issue #686).

**Session-level settings (applied on every `pool.acquire()`):**
- `statement_timeout` = 30s by default. Configurable via the env var above. asyncpg's `command_timeout` is also set to 30s. The server-side timeout is the primary defense; asyncpg's client-side is a backup.

**Pool saturation observability (issue #687):**
`DatabaseManager` accumulates in-memory metrics on every acquire:
- `acquire_total` (counter)
- `acquire_failed_total` (counter)
- `acquire_wait_ms_avg` / `_p50` / `_p95` / `_p99` (over a bounded ring buffer of the last 1024 wait times)

Exposed via `health_check()` under the `pool` sub-dict, alongside the existing live `size`/`free`/`min`/`max` stats. The `health_check_sync()` wrapper is available for sync callers. Example:

```python
db = DatabaseManager()
await db.connect()
result = await db.health_check()
# result["pool"] == {
#   "size": 1, "free": 1, "min": 0, "max": 5,
#   "acquire_total": 142, "acquire_failed_total": 0,
#   "acquire_wait_ms_avg": 1.2, "acquire_wait_ms_p50": 0.4,
#   "acquire_wait_ms_p95": 3.7, "acquire_wait_ms_p99": 12.1,
#   "sample_count": 142,
# }
```

Wire the `_p99` field to your alerting (recommended threshold: > 1s for 5m
sustained). Run the `test_pool_saturation.py` stress test before rolling
out a new downstream consumer to make sure your workload doesn't trip
the alert under normal traffic.

### `common.a2a.client` — A2A HTTP client

Synchronous HTTP client for A2A-protocol agents with retry, backoff, and workflow polling.

```python
from common.a2a import A2AClient

with A2AClient("https://agent.example.com", auth_token="ghp_...") as client:
    client.discover()
    result = client.execute("my_skill", {"key": "value"})
    final = client.execute_and_poll("long_skill", {"key": "value"})
```

**Environment variables:**
- `A2A_TOKEN` — default auth token

### `common.config.tracing` — Unified tracing

Initializes Langfuse and/or LangSmith if configured, then provides a single `log_llm_call()` entrypoint.

```python
from common.config.tracing import initialize_tracing, log_llm_call

initialize_tracing()
log_llm_call(model="gpt-4", prompt="Hello", response="Hi!")
```

**Environment variables:**
- `LANGFUSE_ENABLED`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST`
- `LANGCHAIN_TRACING_V2`, `LANGCHAIN_API_KEY`, `LANGCHAIN_PROJECT`

## License

MIT
