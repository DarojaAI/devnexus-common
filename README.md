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
- `USE_POSTGRESQL` — must be `true` to enable the client

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
