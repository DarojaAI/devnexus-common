"""
Tests for common.llm.client — OpenRouter client per-caller headers.

These tests mock the HTTP layer so they don't require an actual OpenRouter API
key or network access.  They verify:
1. The default headers (devnexus-common) are used when no caller info is provided.
2. Custom headers (per-caller) are passed through on every call.
3. The factory functions forward headers correctly.
"""

from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_response(status_code=200, body=None):
    """Build a mock requests.Response for OpenRouter /chat/completions."""
    if body is None:
        body = {
            "choices": [
                {"message": {"content": "hello"}, "finish_reason": "stop"}
            ],
            "model": "test-model",
            "usage": {"prompt_tokens": 10, "completion_tokens": 20},
        }
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = body
    resp.raise_for_status.return_value = None
    return resp


# ---------------------------------------------------------------------------
# OpenRouterClient — per-caller headers
# ---------------------------------------------------------------------------

class TestOpenRouterClientHeaders:
    """Verify that HTTP-Referer and X-Title are per-caller, not hardcoded."""

    @patch("requests.post")
    def test_default_headers(self, mock_post):
        """Without explicit http_referer/x_title, the default is devnexus-common."""
        from common.llm import OpenRouterClient

        mock_post.return_value = _make_mock_response()

        client = OpenRouterClient(api_key="test-key")
        client.create_message(
            model="test-model",
            messages=[{"role": "user", "content": "hi"}],
        )
        _, kwargs = mock_post.call_args
        assert kwargs["headers"]["HTTP-Referer"] == "https://github.com/DarojaAI/devnexus-common"
        assert kwargs["headers"]["X-Title"] == "devnexus-common"

    @patch("requests.post")
    def test_custom_headers(self, mock_post):
        """When http_referer/x_title are provided, they override defaults."""
        from common.llm import OpenRouterClient

        mock_post.return_value = _make_mock_response()

        client = OpenRouterClient(
            api_key="test-key",
            http_referer="https://github.com/DarojaAI/rag_research_tool",
            x_title="rag-research-tool",
        )
        client.create_message(
            model="test-model",
            messages=[{"role": "user", "content": "hi"}],
        )
        _, kwargs = mock_post.call_args
        assert kwargs["headers"]["HTTP-Referer"] == "https://github.com/DarojaAI/rag_research_tool"
        assert kwargs["headers"]["X-Title"] == "rag-research-tool"

    @patch("requests.post")
    def test_custom_headers_on_generate_json(self, mock_post):
        """Per-caller headers are also used in generate_json."""
        from common.llm import OpenRouterClient

        mock_post.return_value = _make_mock_response(
            body={
                "choices": [{"message": {"content": '{"key": "value"}'}, "finish_reason": "stop"}],
                "model": "test-model",
                "usage": {"prompt_tokens": 5, "completion_tokens": 10},
            }
        )

        client = OpenRouterClient(
            api_key="test-key",
            http_referer="https://github.com/DarojaAI/dev-nexus",
            x_title="dev-nexus",
        )
        client.generate_json(
            model="test-model",
            prompt="extract json",
            response_schema={"type": "object", "properties": {"key": {"type": "string"}}},
        )
        _, kwargs = mock_post.call_args
        assert kwargs["headers"]["HTTP-Referer"] == "https://github.com/DarojaAI/dev-nexus"
        assert kwargs["headers"]["X-Title"] == "dev-nexus"


# ---------------------------------------------------------------------------
# get_llm_client — factory forwards headers
# ---------------------------------------------------------------------------

class TestGetLlmClientHeaders:
    """Verify that the factory function forwards http_referer/x_title."""

    @patch("requests.post")
    def test_factory_default_headers(self, mock_post):
        from common.llm import get_llm_client

        mock_post.return_value = _make_mock_response()

        client = get_llm_client("openrouter", "test-key")
        client.create_message(
            model="test-model",
            messages=[{"role": "user", "content": "hi"}],
        )
        _, kwargs = mock_post.call_args
        assert kwargs["headers"]["HTTP-Referer"] == "https://github.com/DarojaAI/devnexus-common"
        assert kwargs["headers"]["X-Title"] == "devnexus-common"

    @patch("requests.post")
    def test_factory_custom_headers(self, mock_post):
        from common.llm import get_llm_client

        mock_post.return_value = _make_mock_response()

        client = get_llm_client(
            "openrouter",
            "test-key",
            http_referer="https://github.com/DarojaAI/rag_research_tool",
            x_title="rag-research-tool",
        )
        client.create_message(
            model="test-model",
            messages=[{"role": "user", "content": "hi"}],
        )
        _, kwargs = mock_post.call_args
        assert kwargs["headers"]["HTTP-Referer"] == "https://github.com/DarojaAI/rag_research_tool"
        assert kwargs["headers"]["X-Title"] == "rag-research-tool"


# ---------------------------------------------------------------------------
# get_llm_client_from_config — config reads headers
# ---------------------------------------------------------------------------

class TestGetLlmClientFromConfigHeaders:
    """Verify that the config-based factory reads http_referer/x_title from config."""

    @patch("requests.post")
    def test_config_with_headers(self, mock_post):
        from common.llm import get_llm_client_from_config

        mock_post.return_value = _make_mock_response()

        @dataclass
        class FakeConfig:
            llm_provider: str = "openrouter"
            openrouter_api_key: str = "test-key"
            http_referer: str = "https://github.com/DarojaAI/rag_research_tool"
            x_title: str = "rag-research-tool"

        client = get_llm_client_from_config(FakeConfig())
        client.create_message(
            model="test-model",
            messages=[{"role": "user", "content": "hi"}],
        )
        _, kwargs = mock_post.call_args
        assert kwargs["headers"]["HTTP-Referer"] == "https://github.com/DarojaAI/rag_research_tool"
        assert kwargs["headers"]["X-Title"] == "rag-research-tool"

    @patch("requests.post")
    def test_config_without_headers(self, mock_post):
        from common.llm import get_llm_client_from_config

        mock_post.return_value = _make_mock_response()

        @dataclass
        class FakeConfig:
            llm_provider: str = "openrouter"
            openrouter_api_key: str = "test-key"

        client = get_llm_client_from_config(FakeConfig())
        client.create_message(
            model="test-model",
            messages=[{"role": "user", "content": "hi"}],
        )
        _, kwargs = mock_post.call_args
        assert kwargs["headers"]["HTTP-Referer"] == "https://github.com/DarojaAI/devnexus-common"
        assert kwargs["headers"]["X-Title"] == "devnexus-common"


# ---------------------------------------------------------------------------
# Anthropic client — headers are unaffected
# ---------------------------------------------------------------------------

class TestAnthropicClientHeadersUnchanged:
    """Verify that the Anthropic client is completely unaffected by this change."""

    def test_anthropic_client_init(self):
        """AnthropicClient.__init__ signature is unchanged."""
        from common.llm import AnthropicClient

        with patch("common.llm.client.AnthropicClient.__init__", return_value=None):
            _ = AnthropicClient.__new__(AnthropicClient)

    def test_factory_anthropic_no_referer(self):
        """get_llm_client for Anthropic does not accept http_referer/x_title."""
        from common.llm import get_llm_client

        # Anthropic factory should raise ValueError if api_key is missing,
        # regardless of http_referer/x_title
        with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
            get_llm_client("anthropic", "", http_referer="https://example.com")
