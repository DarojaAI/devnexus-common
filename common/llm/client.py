"""
LLM Client Factory

Provides a unified interface for multiple LLM providers:
- Anthropic (Claude)
- OpenRouter (Claude, Minimax, GPT-4, etc.)

This abstraction allows multiple projects to support multiple LLM providers
while maintaining a consistent interface across all consumers.

Usage:
    from common.llm import get_llm_client_from_config, LLMClient

    client = get_llm_client_from_config(config)
    response = client.create_message(
        model="claude-3-5-sonnet-20241022",
        messages=[{"role": "user", "content": "Hello"}],
    )
    print(response.content)
"""

import logging
import os
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class Message:
    """Message wrapper for compatibility across providers"""

    role: str  # "user", "assistant"
    content: str


@dataclass
class LLMResponse:
    """Response wrapper for compatibility across providers"""

    content: str
    model: str
    stop_reason: Optional[str] = None
    usage: Optional[Dict[str, int]] = None


class LLMClient(ABC):
    """Abstract LLM client interface"""

    @abstractmethod
    def create_message(
        self,
        model: str,
        messages: List[Dict[str, str]],
        max_tokens: int = 4096,
        temperature: float = 0.7,
        **kwargs,
    ) -> LLMResponse:
        """Create a message using the LLM"""
        pass

    @abstractmethod
    def get_provider_name(self) -> str:
        """Get the provider name (e.g., 'anthropic', 'openrouter')"""
        pass


class AnthropicClient(LLMClient):
    """Anthropic Claude client wrapper"""

    def __init__(self, api_key: str):
        """Initialize Anthropic client"""
        try:
            from anthropic import Anthropic
        except ImportError:
            raise ImportError("anthropic package required: pip install anthropic")

        if not api_key:
            raise ValueError("Anthropic API key is required")

        self.client = Anthropic(api_key=api_key)
        self.api_key = api_key
        logger.info("✓ Anthropic LLM client initialized")

    def create_message(
        self,
        model: str,
        messages: List[Dict[str, str]],
        max_tokens: int = 4096,
        temperature: float = 0.7,
        **kwargs,
    ) -> LLMResponse:
        """Create a message via Anthropic API"""
        try:
            response = self.client.messages.create(
                model=model or "claude-3-5-sonnet-20241022",
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                **kwargs,
            )

            # Extract content from response
            content = ""
            if response.content:
                content = (
                    response.content[0].text
                    if hasattr(response.content[0], "text")
                    else str(response.content[0])
                )

            return LLMResponse(
                content=content,
                model=response.model,
                stop_reason=getattr(response, "stop_reason", None),
                usage={
                    "input_tokens": response.usage.input_tokens,
                    "output_tokens": response.usage.output_tokens,
                }
                if response.usage
                else None,
            )
        except Exception as e:
            logger.error(f"Anthropic API error: {e}")
            raise

    def get_provider_name(self) -> str:
        return "anthropic"


class OpenRouterClient(LLMClient):
    """OpenRouter client wrapper (supports Claude, GPT-4, Minimax, etc.)"""

    def __init__(self, api_key: str):
        """Initialize OpenRouter client"""
        try:
            import requests  # noqa: F401
        except ImportError:
            raise ImportError("requests package required: pip install requests")

        if not api_key:
            raise ValueError("OpenRouter API key is required")

        self.api_key = api_key
        self.base_url = "https://openrouter.ai/api/v1"
        logger.info("✓ OpenRouter LLM client initialized")

    def create_message(
        self,
        model: str,
        messages: List[Dict[str, str]],
        max_tokens: int = 4096,
        temperature: float = 0.7,
        **kwargs,
    ) -> LLMResponse:
        """Create a message via OpenRouter API"""
        import requests

        if not model:
            raise ValueError("Model is required for OpenRouter")

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/DarojaAI/dev-nexus",
            "X-Title": "dev-nexus",
        }

        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            **kwargs,
        }

        try:
            response = requests.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=payload,
                timeout=60,
            )
            response.raise_for_status()

            data = response.json()

            # Extract content from OpenRouter response
            content = ""
            if data.get("choices") and len(data["choices"]) > 0:
                content = data["choices"][0].get("message", {}).get("content", "")

            return LLMResponse(
                content=content,
                model=data.get("model", model),
                stop_reason=data["choices"][0].get("finish_reason")
                if data.get("choices")
                else None,
                usage={
                    "input_tokens": data.get("usage", {}).get("prompt_tokens", 0),
                    "output_tokens": data.get("usage", {}).get("completion_tokens", 0),
                }
                if data.get("usage")
                else None,
            )
        except requests.exceptions.RequestException as e:
            logger.error(f"OpenRouter API error: {e}")
            if hasattr(e, "response") and e.response is not None:
                logger.error(f"Response: {e.response.text}")
            raise

    def get_provider_name(self) -> str:
        return "openrouter"


def get_llm_client(
    provider: str,
    api_key: str,
    model: Optional[str] = None,
) -> LLMClient:
    """
    Factory function to create the appropriate LLM client

    Args:
        provider: "anthropic" or "openrouter"
        api_key: API key for the provider
        model: Optional default model override

    Returns:
        LLMClient instance (AnthropicClient or OpenRouterClient)

    Raises:
        ValueError: If provider is invalid or api_key is missing
    """
    provider = provider.lower().strip()

    if provider == "anthropic":
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY environment variable is required")
        return AnthropicClient(api_key)

    elif provider == "openrouter":
        if not api_key:
            raise ValueError("OPENROUTER_API_KEY environment variable is required")
        return OpenRouterClient(api_key)

    else:
        raise ValueError(
            f"Unknown LLM provider: {provider}. "
            f"Supported: 'anthropic', 'openrouter'"
        )


def get_llm_client_from_config(config: Any) -> LLMClient:
    """
    Create LLM client from config object

    Args:
        config: Config object with llm_provider and API keys

    Returns:
        LLMClient instance
    """
    provider = getattr(config, "llm_provider", "anthropic").lower()

    if provider == "anthropic":
        api_key = getattr(config, "anthropic_api_key", "")
        if not api_key:
            api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        return get_llm_client("anthropic", api_key)

    elif provider == "openrouter":
        api_key = getattr(config, "openrouter_api_key", "")
        if not api_key:
            api_key = os.environ.get("OPENROUTER_API_KEY", "")
        return get_llm_client("openrouter", api_key)

    else:
        raise ValueError(f"Unknown LLM provider: {provider}")
