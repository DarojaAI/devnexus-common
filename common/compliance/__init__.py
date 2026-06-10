"""
Compliance Module

Standards compliance checking shared across DarojaAI projects.
Provides readiness staging, standards loading, and audit capabilities.

This module is the shared version of the original dev-nexus/compliance/
top-level module. The original was project-specific; this version is
generic and can be consumed by any DarojaAI project.
"""

from common.compliance.standards_loader import (
    load_standards_config,
    get_readiness_stages,
    get_readiness_checks,
)

__all__ = [
    "load_standards_config",
    "get_readiness_stages",
    "get_readiness_checks",
]
