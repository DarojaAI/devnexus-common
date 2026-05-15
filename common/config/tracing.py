"""Unified Tracing Wrapper for LLM Observability

Supports Langfuse and LangSmith. Initialize once per process:

    from common.config.tracing import initialize_tracing, log_llm_call
    initialize_tracing()
    log_llm_call(model="gpt-4", prompt="Hello", response="Hi!")

All backends are optional — the module degrades gracefully when dependencies
or environment variables are missing.
"""

import os
import logging
from typing import Optional, Dict, Any
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("common.config.tracing")

# Global state
_langfuse_client = None
_langsmith_enabled = False


def _is_langfuse_available() -> bool:
    """Check if the langfuse module is importable."""
    try:
        import langfuse  # noqa: F401
        return True
    except ImportError:
        return False


def _is_langfuse_enabled() -> bool:
    """Check if Langfuse is properly configured."""
    enabled = os.getenv("LANGFUSE_ENABLED", "false").lower() in ("1", "true", "yes")
    public_key = os.getenv("LANGFUSE_PUBLIC_KEY", "")
    secret_key = os.getenv("LANGFUSE_SECRET_KEY", "")
    return enabled and public_key and secret_key


def _configure_langfuse():
    """Initialize and return a Langfuse client if available."""
    if not _is_langfuse_available():
        logger.debug("[LANGFUSE] Module not installed")
        return None
    if not _is_langfuse_enabled():
        logger.debug("[LANGFUSE] Disabled or missing keys")
        return None

    try:
        from langfuse import Langfuse

        client = Langfuse(
            public_key=os.getenv("LANGFUSE_PUBLIC_KEY"),
            secret_key=os.getenv("LANGFUSE_SECRET_KEY"),
            host=os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com"),
        )
        logger.info("[LANGFUSE] Initialized")
        return client
    except Exception as e:
        logger.warning(f"[LANGFUSE] Initialization failed: {e}")
        return None


def _is_langsmith_enabled() -> bool:
    """Check if LangSmith tracing is enabled."""
    tracing = os.getenv("LANGCHAIN_TRACING_V2", "false").lower() in ("1", "true", "yes")
    api_key = os.getenv("LANGCHAIN_API_KEY", "")
    return tracing and bool(api_key)


def _configure_langsmith() -> None:
    """Configure LangSmith environment if enabled."""
    if not _is_langsmith_enabled():
        logger.debug("[LANGSMITH] Disabled or missing API key")
        return

    os.environ.setdefault("LANGCHAIN_PROJECT", os.getenv("PROJECT_NAME", "devnexus"))
    os.environ.setdefault("LANGCHAIN_ENDPOINT", "https://api.smith.langchain.com")
    logger.info("[LANGSMITH] Configured")


def initialize_tracing():
    """Initialize both LangSmith and Langfuse clients."""
    global _langfuse_client, _langsmith_enabled
    _langsmith_enabled = _is_langsmith_enabled()
    if _langsmith_enabled:
        _configure_langsmith()
    _langfuse_client = _configure_langfuse()


def is_tracing_enabled() -> bool:
    """Check if any tracing platform is enabled."""
    global _langfuse_client, _langsmith_enabled
    return _langsmith_enabled or _langfuse_client is not None


def is_langfuse_available() -> bool:
    """Check if Langfuse module is available."""
    return _is_langfuse_available()


def log_llm_call(
    model: str,
    prompt: str,
    response: str,
    metadata: Optional[Dict[str, Any]] = None,
    session_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> None:
    """
    Log an LLM call to all enabled tracing platforms.

    Args:
        model: Model identifier (e.g. "gpt-4", "claude-3-sonnet").
        prompt: The input prompt sent to the model.
        response: The model's response text.
        metadata: Optional extra key/values (temperature, tokens, cost, etc.).
        session_id: Optional session grouping ID.
        user_id: Optional user ID.
    """
    global _langfuse_client, _langsmith_enabled

    if not is_tracing_enabled():
        return

    if _langfuse_client is not None:
        try:
            trace = _langfuse_client.trace(
                name="llm_call",
                session_id=session_id,
                user_id=user_id,
                metadata=metadata or {},
            )
            trace.generation(
                name=model,
                model=model,
                input=prompt,
                output=response,
            )
        except Exception as e:
            logger.warning(f"[LANGFUSE] Failed to log LLM call: {e}")

    if _langsmith_enabled:
        try:
            import langsmith
            from langsmith.run_trees import RunTree

            run_tree = RunTree(
                name="llm_call",
                run_type="llm",
                inputs={"prompt": prompt},
                outputs={"response": response},
                extra=metadata or {},
            )
            run_tree.post()
        except Exception as e:
            logger.warning(f"[LANGSMITH] Failed to log LLM call: {e}")


def get_tracing_status() -> dict:
    """Get status of both tracing platforms."""
    global _langfuse_client, _langsmith_enabled
    return {
        "langfuse": {
            "available": _is_langfuse_available(),
            "enabled": _langfuse_client is not None,
            "configured": _is_langfuse_enabled(),
        },
        "langsmith": {
            "enabled": _langsmith_enabled,
            "configured": _is_langsmith_enabled(),
        },
        "any_active": is_tracing_enabled(),
    }
