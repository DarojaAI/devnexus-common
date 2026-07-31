"""
LLM Client Factory

Provides a unified interface for multiple LLM providers:
- Anthropic (Claude)
- OpenRouter (Claude, Minimax, GPT-4, etc.)
- OpenAI (GPT-4, GPT-4o, etc.)
- Azure OpenAI (enterprise deployments)
- OpenAI-compatible endpoints (Ollama, vLLM, LiteLLM, etc.)

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

import json
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
class EmbeddingResponse:
    """Response wrapper for embedding API calls"""

    embeddings: List[List[float]]
    model: str
    usage: Optional[Dict[str, int]] = None


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
    def generate_json(
        self,
        model: str,
        prompt: str,
        response_schema: Dict[str, Any],
        max_tokens: int = 4096,
        temperature: float = 0.0,
        **kwargs,
    ) -> Dict[str, Any]:
        """
        Generate a structured JSON response conforming to response_schema.

        Args:
            model: Model identifier (e.g. "claude-3-5-sonnet-20241022")
            prompt: User prompt text
            response_schema: JSON Schema dict describing the expected response shape.
                            e.g. {"type": "object", "properties": {"name": {"type": "string"}}}
            max_tokens: Maximum output tokens
            temperature: Sampling temperature (0.0 = deterministic for structured output)

        Returns:
            Parsed JSON dict matching response_schema

        Raises:
            ValueError: If the model cannot produce valid JSON matching the schema
        """
        pass

    @abstractmethod
    def get_provider_name(self) -> str:
        """Get the provider name (e.g. 'anthropic', 'openrouter')"""
        pass

    def embed(
        self,
        model: str,
        input: List[str],
        **kwargs,
    ) -> "EmbeddingResponse":
        """Create embeddings for the given input texts.

        Args:
            model: Embedding model identifier (e.g. "text-embedding-3-small").
            input: List of strings to embed.

        Returns:
            EmbeddingResponse with embeddings, model, and usage.

        Raises:
            NotImplementedError: If the client does not support embeddings.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support embeddings"
        )

    def embed_normalized(
        self,
        model: str,
        input: List[str],
        dim: Optional[int] = None,
        normalize: bool = True,
        batch_size: int = 50,
        **kwargs,
    ) -> List[List[float]]:
        """Embed texts with optional L2-normalization and dimension padding.

        Convenience wrapper around :meth:`embed` that adds batching,
        L2-normalization, and zero-padding / truncation to a target
        dimension.  This is the recommended entry point for embedding
        call sites that store vectors in pgvector or other vector stores.

        Args:
            model: Embedding model identifier.
            input: List of strings to embed.
            dim: Target embedding dimension.  ``None`` skips pad/truncate.
            normalize: L2-normalize each vector to unit length (default ``True``).
            batch_size: Number of texts per API call (default 50).
            **kwargs: Forwarded to :meth:`embed`.

        Returns:
            List of embedding vectors, one per input text.

        Raises:
            NotImplementedError: If the client does not support embeddings.
        """
        results: List[List[float]] = []
        for i in range(0, len(input), batch_size):
            batch = input[i : i + batch_size]
            resp = self.embed(model=model, input=batch, **kwargs)
            for vector in resp.embeddings:
                if normalize:
                    vector = normalize_and_pad(vector, dim=dim)
                elif dim is not None:
                    vector = _pad_or_truncate(vector, dim)
                results.append(vector)
        return results


def normalize_and_pad(vector: List[float], dim: Optional[int] = None) -> List[float]:
    """L2-normalize *vector* to unit length, then optionally pad/truncate to *dim*.

    Args:
        vector: Raw embedding from the API.
        dim: Target dimension.  When provided the vector is truncated (if
            longer) or zero-padded (if shorter).  ``None`` skips the
            pad/truncate step and only normalizes.

    Returns:
        Normalized (and optionally padded/truncated) vector.
    """
    norm = sum(x * x for x in vector) ** 0.5
    if norm > 0:
        vector = [x / norm for x in vector]
    if dim is not None:
        vector = _pad_or_truncate(vector, dim)
    return vector


def _pad_or_truncate(vector: List[float], dim: int) -> List[float]:
    """Pad or truncate *vector* to *dim* dimensions."""
    if len(vector) >= dim:
        return vector[:dim]
    return vector + [0.0] * (dim - len(vector))


def batch_embed(
    client: "LLMClient",
    texts: List[str],
    model: str,
    dim: Optional[int] = None,
    normalize: bool = True,
    batch_size: int = 50,
) -> List[List[float]]:
    """Standalone helper: embed *texts* with batching, normalization, and dim-pad.

    This is a convenience wrapper that delegates to
    ``client.embed_normalized()``.  Use this when you have a client
    instance but prefer a functional API.

    Args:
        client: An ``LLMClient`` instance.
        texts: Input strings to embed.
        model: Embedding model identifier.
        dim: Target embedding dimension (``None`` = no pad/truncate).
        normalize: L2-normalize each vector (default ``True``).
        batch_size: Texts per API call (default 50).

    Returns:
        List of embedding vectors.
    """
    return client.embed_normalized(
        model=model,
        input=texts,
        dim=dim,
        normalize=normalize,
        batch_size=batch_size,
    )


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

    def generate_json(
        self,
        model: str,
        prompt: str,
        response_schema: Dict[str, Any],
        max_tokens: int = 4096,
        temperature: float = 0.0,
        **kwargs,
    ) -> Dict[str, Any]:
        """
        Generate a structured JSON response via Anthropic with json_schema.
        Uses response_format for guaranteed schema adherence.
        """
        messages = [{"role": "user", "content": prompt}]
        try:
            response = self.client.messages.create(
                model=model or "claude-3-5-sonnet-20241022",
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "response",
                        "schema": response_schema,
                    },
                },
                **kwargs,
            )
            content = ""
            if response.content:
                content = (
                    response.content[0].text
                    if hasattr(response.content[0], "text")
                    else str(response.content[0])
                )
            return json.loads(content)
        except json.JSONDecodeError as e:
            logger.error(f"Anthropic generate_json: invalid JSON from model: {e}")
            raise ValueError(f"Model did not return valid JSON: {e}") from e
        except Exception as e:
            logger.error(f"Anthropic generate_json error: {e}")
            raise

    def get_provider_name(self) -> str:
        return "anthropic"


class OpenAICompatibleClient(LLMClient):
    """Generic OpenAI-compatible client wrapper.

    Works with any endpoint that implements the ``/v1/chat/completions``
    API: OpenRouter, Ollama, vLLM, LiteLLM, LM Studio, text-generation-
    webui, and other OpenAI-compatible servers.

    Args:
        api_key: API key for the endpoint.  For local servers that don't
            require auth (e.g. Ollama), pass an empty string.
        base_url: Base URL of the ``/v1`` endpoint.  Defaults to
            ``https://openrouter.ai/api/v1`` (preserves OpenRouter behaviour
            for callers that don't pass an explicit URL).
        http_referer: ``HTTP-Referer`` header sent to OpenRouter.  Ignored
            for non-OpenRouter endpoints.  Defaults to
            ``https://github.com/DarojaAI/devnexus-common``.
        x_title: ``X-Title`` header sent to OpenRouter.  Ignored for
            non-OpenRouter endpoints.  Defaults to ``devnexus-common``.
        extra_headers: Additional headers to include in every request.
            ``Authorization`` and ``Content-Type`` are always set by the
            client and should NOT appear here.
    """

    DEFAULT_OPENROUTER_URL = "https://openrouter.ai/api/v1"

    def __init__(
        self,
        api_key: str = "",
        base_url: Optional[str] = None,
        http_referer: Optional[str] = None,
        x_title: Optional[str] = None,
        extra_headers: Optional[Dict[str, str]] = None,
    ):
        try:
            import requests  # noqa: F401
        except ImportError:
            raise ImportError("requests package required: pip install requests")

        self.api_key = api_key
        self.base_url = (
            base_url.rstrip("/") if base_url else self.DEFAULT_OPENROUTER_URL
        )
        self.extra_headers = extra_headers or {}

        # OpenRouter-specific headers (only sent when base_url is OpenRouter)
        is_openrouter = "openrouter.ai" in self.base_url
        self._openrouter_headers: Dict[str, str] = {}
        if is_openrouter and api_key:
            self._openrouter_headers = {
                "HTTP-Referer": http_referer
                or "https://github.com/DarojaAI/devnexus-common",
                "X-Title": x_title or "devnexus-common",
            }

        logger.info(
            "✓ OpenAI-compatible LLM client initialized (base_url=%s)",
            self.base_url,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        headers.update(self._openrouter_headers)
        headers.update(self.extra_headers)
        return headers

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def create_message(
        self,
        model: str,
        messages: List[Dict[str, str]],
        max_tokens: int = 4096,
        temperature: float = 0.7,
        **kwargs,
    ) -> LLMResponse:
        """Create a message via an OpenAI-compatible API."""
        import requests

        if not model:
            raise ValueError("Model is required")

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
                headers=self._build_headers(),
                json=payload,
                timeout=60,
            )
            response.raise_for_status()
            data = response.json()

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
            logger.error(f"OpenAI-compatible API error ({self.base_url}): {e}")
            if hasattr(e, "response") and e.response is not None:
                logger.error(f"Response: {e.response.text}")
            raise

    def generate_json(
        self,
        model: str,
        prompt: str,
        response_schema: Dict[str, Any],
        max_tokens: int = 4096,
        temperature: float = 0.0,
        **kwargs,
    ) -> Dict[str, Any]:
        """Generate a structured JSON response via an OpenAI-compatible API."""
        import requests

        if not model:
            raise ValueError("Model is required")

        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "response",
                    "schema": response_schema,
                },
            },
            **kwargs,
        }

        try:
            response = requests.post(
                f"{self.base_url}/chat/completions",
                headers=self._build_headers(),
                json=payload,
                timeout=60,
            )
            response.raise_for_status()
            data = response.json()

            content = ""
            if data.get("choices") and len(data["choices"]) > 0:
                content = data["choices"][0].get("message", {}).get("content", "")

            return json.loads(content)
        except json.JSONDecodeError as e:
            logger.error(f"OpenAI-compatible generate_json: invalid JSON: {e}")
            raise ValueError(f"Model did not return valid JSON: {e}") from e
        except requests.exceptions.RequestException as e:
            logger.error(
                f"OpenAI-compatible generate_json error ({self.base_url}): {e}"
            )
            raise

    def embed(
        self,
        model: str,
        input: List[str],
        **kwargs,
    ) -> "EmbeddingResponse":
        """Create embeddings via an OpenAI-compatible /v1/embeddings endpoint."""
        import requests as _requests

        if not model:
            raise ValueError("Model is required for embeddings")
        if not input:
            raise ValueError("Input texts are required for embeddings")

        payload = {
            "model": model,
            "input": input,
            **kwargs,
        }

        try:
            response = _requests.post(
                f"{self.base_url}/embeddings",
                headers=self._build_headers(),
                json=payload,
                timeout=60,
            )
            response.raise_for_status()
            data = response.json()

            embeddings = [item["embedding"] for item in data.get("data", [])]
            return EmbeddingResponse(
                embeddings=embeddings,
                model=data.get("model", model),
                usage=data.get("usage"),
            )
        except _requests.exceptions.RequestException as e:
            logger.error(f"OpenAI-compatible embed error ({self.base_url}): {e}")
            if hasattr(e, "response") and e.response is not None:
                logger.error(f"Response: {e.response.text}")
            raise

    def get_provider_name(self) -> str:
        if "openrouter.ai" in self.base_url:
            return "openrouter"
        return "openai-compatible"


# Backward-compatible alias
OpenRouterClient = OpenAICompatibleClient


class OpenAIClient(LLMClient):
    """Native OpenAI SDK client wrapper.

    Uses the ``openai`` Python package for built-in retries, streaming,
    and structured outputs.  Connects to ``api.openai.com`` by default
    or a custom ``base_url`` for proxies.

    Args:
        api_key: OpenAI API key (or env ``OPENAI_API_KEY``).
        base_url: Optional custom base URL (for proxies / compatible APIs).
        organization: Optional OpenAI organization ID.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        organization: Optional[str] = None,
    ):
        try:
            import openai  # noqa: F401
        except ImportError:
            raise ImportError(
                "openai package required: pip install devnexus-common[openai]"
            )

        api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            raise ValueError(
                "OpenAI API key is required. Pass api_key or set OPENAI_API_KEY."
            )

        kwargs: Dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        if organization:
            kwargs["organization"] = organization

        self.client = openai.OpenAI(**kwargs)
        self.api_key = api_key
        logger.info("✓ OpenAI SDK client initialized")

    def create_message(
        self,
        model: str,
        messages: List[Dict[str, str]],
        max_tokens: int = 4096,
        temperature: float = 0.7,
        **kwargs,
    ) -> LLMResponse:
        """Create a message via the OpenAI SDK."""
        try:
            response = self.client.chat.completions.create(
                model=model or "gpt-4o",
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                **kwargs,
            )

            choice = response.choices[0] if response.choices else None
            content = choice.message.content if choice and choice.message else ""
            stop_reason = choice.finish_reason if choice else None

            usage = None
            if response.usage:
                usage = {
                    "input_tokens": response.usage.prompt_tokens,
                    "output_tokens": response.usage.completion_tokens,
                }

            return LLMResponse(
                content=content or "",
                model=response.model or model,
                stop_reason=stop_reason,
                usage=usage,
            )
        except Exception as e:
            logger.error(f"OpenAI API error: {e}")
            raise

    def generate_json(
        self,
        model: str,
        prompt: str,
        response_schema: Dict[str, Any],
        max_tokens: int = 4096,
        temperature: float = 0.0,
        **kwargs,
    ) -> Dict[str, Any]:
        """Generate structured JSON via the OpenAI SDK."""
        messages = [{"role": "user", "content": prompt}]
        try:
            response = self.client.chat.completions.create(
                model=model or "gpt-4o",
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "response",
                        "schema": response_schema,
                    },
                },
                **kwargs,
            )
            content = ""
            if response.choices:
                choice = response.choices[0]
                if choice.message and choice.message.content:
                    content = choice.message.content
            return json.loads(content)
        except json.JSONDecodeError as e:
            logger.error(f"OpenAI generate_json: invalid JSON from model: {e}")
            raise ValueError(f"Model did not return valid JSON: {e}") from e
        except Exception as e:
            logger.error(f"OpenAI generate_json error: {e}")
            raise

    def get_provider_name(self) -> str:
        return "openai"


class AzureOpenAIClient(LLMClient):
    """Azure OpenAI client wrapper.

    Uses the ``openai`` Python SDK with Azure-specific authentication.
    Requires an Azure OpenAI endpoint and API key (or managed identity).

    Args:
        api_key: Azure OpenAI API key (or env ``AZURE_OPENAI_KEY``).
        azure_endpoint: Azure OpenAI resource endpoint URL (or env
            ``AZURE_OPENAI_ENDPOINT``).  Example:
            ``https://my-resource.openai.azure.com/``
        api_version: Azure API version (or env ``OPENAI_API_VERSION``).
            Defaults to ``2024-12-01-preview``.
        deployment_name: Optional default deployment name.  If provided,
            this is used as the model in ``create_message`` when no model
            is explicitly passed.
    """

    DEFAULT_API_VERSION = "2024-12-01-preview"

    def __init__(
        self,
        api_key: Optional[str] = None,
        azure_endpoint: Optional[str] = None,
        api_version: Optional[str] = None,
        deployment_name: Optional[str] = None,
    ):
        try:
            import openai  # noqa: F401
        except ImportError:
            raise ImportError(
                "openai package required: pip install devnexus-common[openai]"
            )

        api_key = api_key or os.environ.get("AZURE_OPENAI_KEY", "")
        if not api_key:
            raise ValueError(
                "Azure OpenAI API key is required. "
                "Pass api_key or set AZURE_OPENAI_KEY."
            )

        azure_endpoint = azure_endpoint or os.environ.get("AZURE_OPENAI_ENDPOINT", "")
        if not azure_endpoint:
            raise ValueError(
                "Azure endpoint is required. "
                "Pass azure_endpoint or set AZURE_OPENAI_ENDPOINT."
            )

        api_version = api_version or os.environ.get(
            "OPENAI_API_VERSION", self.DEFAULT_API_VERSION
        )

        self.client = openai.AzureOpenAI(
            api_key=api_key,
            azure_endpoint=azure_endpoint,
            api_version=api_version,
        )
        self.api_key = api_key
        self.azure_endpoint = azure_endpoint
        self.api_version = api_version
        self.deployment_name = deployment_name
        logger.info("✓ Azure OpenAI client initialized (endpoint=%s)", azure_endpoint)

    def _resolve_model(self, model: str) -> str:
        """Resolve model: explicit > deployment_name > fallback."""
        return model or self.deployment_name or "gpt-4o"

    def create_message(
        self,
        model: str,
        messages: List[Dict[str, str]],
        max_tokens: int = 4096,
        temperature: float = 0.7,
        **kwargs,
    ) -> LLMResponse:
        """Create a message via Azure OpenAI."""
        try:
            response = self.client.chat.completions.create(
                model=self._resolve_model(model),
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                **kwargs,
            )

            choice = response.choices[0] if response.choices else None
            content = choice.message.content if choice and choice.message else ""
            stop_reason = choice.finish_reason if choice else None

            usage = None
            if response.usage:
                usage = {
                    "input_tokens": response.usage.prompt_tokens,
                    "output_tokens": response.usage.completion_tokens,
                }

            return LLMResponse(
                content=content or "",
                model=response.model or model,
                stop_reason=stop_reason,
                usage=usage,
            )
        except Exception as e:
            logger.error(f"Azure OpenAI API error: {e}")
            raise

    def generate_json(
        self,
        model: str,
        prompt: str,
        response_schema: Dict[str, Any],
        max_tokens: int = 4096,
        temperature: float = 0.0,
        **kwargs,
    ) -> Dict[str, Any]:
        """Generate structured JSON via Azure OpenAI."""
        messages = [{"role": "user", "content": prompt}]
        try:
            response = self.client.chat.completions.create(
                model=self._resolve_model(model),
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "response",
                        "schema": response_schema,
                    },
                },
                **kwargs,
            )
            content = ""
            if response.choices:
                choice = response.choices[0]
                if choice.message and choice.message.content:
                    content = choice.message.content
            return json.loads(content)
        except json.JSONDecodeError as e:
            logger.error(f"Azure OpenAI generate_json: invalid JSON from model: {e}")
            raise ValueError(f"Model did not return valid JSON: {e}") from e
        except Exception as e:
            logger.error(f"Azure OpenAI generate_json error: {e}")
            raise

    def get_provider_name(self) -> str:
        return "azure-openai"


# ======================================================================
# Factory functions
# ======================================================================

# Supported provider aliases
_PROVIDER_ALIASES: Dict[str, str] = {
    "anthropic": "anthropic",
    "openrouter": "openrouter",
    "openai": "openai",
    "azure-openai": "azure-openai",
    "azure_openai": "azure-openai",
    "openai-compatible": "openai-compatible",
    "openai_compatible": "openai-compatible",
    "ollama": "ollama",
}


def get_llm_client(
    provider: str,
    api_key: str = "",
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    http_referer: Optional[str] = None,
    x_title: Optional[str] = None,
    extra_headers: Optional[Dict[str, str]] = None,
    # Azure-specific
    azure_endpoint: Optional[str] = None,
    api_version: Optional[str] = None,
    deployment_name: Optional[str] = None,
    # OpenAI-specific
    organization: Optional[str] = None,
) -> LLMClient:
    """
    Factory function to create the appropriate LLM client.

    Args:
        provider: One of: ``anthropic``, ``openrouter``, ``openai``,
            ``azure-openai``, ``openai-compatible``, ``ollama``.
            Aliases ``openai_compatible`` and ``azure_openai`` are also accepted.
        api_key: API key for the provider.  For Anthropic the env fallback
            is ``ANTHROPIC_API_KEY``; for OpenAI, ``OPENAI_API_KEY``; for
            Azure, ``AZURE_OPENAI_KEY``.
        model: Default model hint (not used by the factory itself, but
            stored for callers that prefer a default).
        base_url: Base URL for ``openai-compatible`` / ``ollama`` providers.
            For ``openrouter`` defaults to ``https://openrouter.ai/api/v1``.
            For ``ollama`` defaults to ``http://localhost:11434/v1``.
        http_referer: ``HTTP-Referer`` header (OpenRouter only).
        x_title: ``X-Title`` header (OpenRouter only).
        extra_headers: Additional HTTP headers for OpenAI-compatible requests.
        azure_endpoint: Azure OpenAI endpoint URL (required for ``azure-openai``).
        api_version: Azure API version (default ``2024-12-01-preview``).
        deployment_name: Default Azure deployment name.
        organization: OpenAI organization ID.

    Returns:
        LLMClient instance.

    Raises:
        ValueError: If provider is unknown or required credentials are missing.
    """
    provider_key = provider.lower().strip()
    resolved = _PROVIDER_ALIASES.get(provider_key, provider_key)

    if resolved == "anthropic":
        if not api_key:
            api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise ValueError(
                "Anthropic API key is required. Pass api_key or set ANTHROPIC_API_KEY."
            )
        return AnthropicClient(api_key=api_key)

    elif resolved == "openrouter":
        if not api_key:
            api_key = os.environ.get("OPENROUTER_API_KEY", "")
        if not api_key:
            raise ValueError(
                "OpenRouter API key is required. "
                "Pass api_key or set OPENROUTER_API_KEY."
            )
        return OpenAICompatibleClient(
            api_key=api_key,
            base_url=base_url or OpenAICompatibleClient.DEFAULT_OPENROUTER_URL,
            http_referer=http_referer,
            x_title=x_title,
            extra_headers=extra_headers,
        )

    elif resolved == "openai":
        return OpenAIClient(
            api_key=api_key or None,
            base_url=base_url,
            organization=organization,
        )

    elif resolved == "azure-openai":
        return AzureOpenAIClient(
            api_key=api_key or None,
            azure_endpoint=azure_endpoint,
            api_version=api_version,
            deployment_name=deployment_name,
        )

    elif resolved == "ollama":
        return OpenAICompatibleClient(
            api_key=api_key or "",
            base_url=base_url or "http://localhost:11434/v1",
            extra_headers=extra_headers,
        )

    elif resolved == "openai-compatible":
        if not base_url:
            raise ValueError("base_url is required for openai-compatible provider")
        return OpenAICompatibleClient(
            api_key=api_key,
            base_url=base_url,
            extra_headers=extra_headers,
        )

    else:
        supported = ", ".join(sorted(set(_PROVIDER_ALIASES.values())))
        raise ValueError(f"Unknown LLM provider: {provider!r}. Supported: {supported}")


def get_llm_client_from_config(config: Any) -> LLMClient:
    """
    Create LLM client from config object.

    Reads the following attributes from *config* (falling back to
    environment variables when the attribute is missing or empty):

    - ``llm_provider`` / ``LLM_PROVIDER`` — provider name (default ``anthropic``)
    - ``llm_base_url`` / ``LLM_BASE_URL`` — base URL (for compatible endpoints)
    - ``llm_model`` / ``LLM_MODEL`` — default model (informational; not used by factory)
    - ``llm_api_key`` / provider-specific key — API key

    Provider-specific config attributes:

    - ``http_referer``, ``x_title`` — OpenRouter headers
    - ``azure_endpoint`` / ``AZURE_OPENAI_ENDPOINT`` — Azure endpoint
    - ``api_version`` / ``OPENAI_API_VERSION`` — Azure API version
    - ``deployment_name`` — Azure deployment name
    - ``organization`` — OpenAI org ID

    Args:
        config: Config object with ``llm_provider`` and API keys.

    Returns:
        LLMClient instance.
    """
    provider = (
        getattr(config, "llm_provider", None)
        or os.environ.get("LLM_PROVIDER", "anthropic")
    ).lower()

    base_url = getattr(config, "llm_base_url", None) or os.environ.get("LLM_BASE_URL")

    # Provider-specific API key resolution
    api_key = ""
    if provider in ("anthropic",):
        api_key = getattr(config, "anthropic_api_key", "") or os.environ.get(
            "ANTHROPIC_API_KEY", ""
        )
    elif provider in ("openrouter",):
        api_key = getattr(config, "openrouter_api_key", "") or os.environ.get(
            "OPENROUTER_API_KEY", ""
        )
    elif provider in ("openai",):
        api_key = getattr(config, "openai_api_key", "") or os.environ.get(
            "OPENAI_API_KEY", ""
        )
    elif provider in ("azure-openai", "azure_openai"):
        api_key = getattr(config, "azure_openai_key", "") or os.environ.get(
            "AZURE_OPENAI_KEY", ""
        )
    else:
        # generic — try common key attributes
        api_key = (
            getattr(config, "llm_api_key", "")
            or getattr(config, "api_key", "")
            or os.environ.get("LLM_API_KEY", "")
        )

    return get_llm_client(
        provider=provider,
        api_key=api_key,
        base_url=base_url,
        http_referer=getattr(config, "http_referer", None),
        x_title=getattr(config, "x_title", None),
        extra_headers=getattr(config, "extra_headers", None),
        azure_endpoint=getattr(config, "azure_endpoint", None),
        api_version=getattr(config, "api_version", None),
        deployment_name=getattr(config, "deployment_name", None),
        organization=getattr(config, "organization", None),
    )
