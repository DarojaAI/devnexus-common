# Compliance Module

Shared standards compliance checking for DarojaAI projects.

## What

- `__init__.py` re-exports the public API: `load_standards_config`, `get_readiness_stages`, `get_readiness_checks`
- `standards_loader.py` — load `standards-config.yaml` from disk (env var `STANDARDS_CONFIG_PATH` overrides default path)
- `standards-config.yaml` — the actual data: which standards apply to which repo types, readiness stages, etc.
- `audit.py` — standalone CLI to audit one or more repositories against the standards

## Usage

```python
from common.compliance import (
    load_standards_config,
    get_readiness_stages,
    get_readiness_checks,
)

config = load_standards_config()
stages = get_readiness_stages(config)
checks = get_readiness_checks(config, repo_type="backend")
```

## CLI

```bash
# Audit current directory
python common/compliance/audit.py

# Audit a different repo
python common/compliance/audit.py --repo ../my-project

# Audit multiple repos at once
python common/compliance/audit.py --repo ../backend --repo ../frontend

# Generate a JSON report
python common/compliance/audit.py --all --report report.json

# Watch mode
python common/compliance/audit.py --watch
```

## Standards config location

`standards-config.yaml` is loaded from, in priority order:
1. `$STANDARDS_CONFIG_PATH` environment variable (if set)
2. `<package_install>/compliance/standards-config.yaml` (the bundled default)
3. `compliance/standards-config.yaml` relative to the current working directory

## Migration from dev-nexus

This module is the shared version of what used to be `dev-nexus/compliance/`
at the repo root. The original was project-specific. The move:

- `dev-nexus/compliance/standards_loader.py` → `common/compliance/standards_loader.py`
- `dev-nexus/compliance/standards-config.yaml` → `common/compliance/standards-config.yaml`
- `dev-nexus/compliance/audit.py` → `common/compliance/audit.py` (docstring examples generalized)

Consumers in dev-nexus (currently `src/a2a/skills/concept_readiness.py` line 684):
- Before: `from compliance.standards_loader import load_standards_config`
- After: `from common.compliance import load_standards_config`
