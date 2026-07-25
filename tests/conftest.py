"""
Shared fixtures and module mocks for tests that depend on optional SDKs.

CI does not install `openai` or `anthropic`.  We inject mock modules into
sys.modules so that lazy imports inside client constructors succeed.  Tests
that use ``@patch("openai.OpenAI")`` etc. will then correctly replace the
mock class during each test.
"""

import sys
import types
from unittest.mock import MagicMock


def _ensure_mock_module(name: str) -> types.ModuleType:
    """Insert a MagicMock-backed module into sys.modules if absent."""
    if name not in sys.modules:
        mod = types.ModuleType(name)
        # Make attribute access return MagicMocks automatically.
        mod.__getattr__ = lambda self_name: MagicMock()  # noqa: ARG005
        sys.modules[name] = mod
    return sys.modules[name]


# --- openai ----------------------------------------------------------------

_openai = _ensure_mock_module("openai")
_openai.OpenAI = MagicMock  # type: ignore[attr-defined]
_openai.AzureOpenAI = MagicMock  # type: ignore[attr-defined]

# --- anthropic -------------------------------------------------------------

_anthropic = _ensure_mock_module("anthropic")
_anthropic.Anthropic = MagicMock  # type: ignore[attr-defined]
