"""
LLM Provider abstraction for multi-provider classification support.

Supports multiple LLM providers (Anthropic, Google Gemini, etc.) through a
unified interface. Providers are selected via the `provider` field in modes.

Usage:
    provider = get_provider("anthropic")
    result = provider.classify(tweet_text, system_prompt, model="claude-haiku-3")

    # Or for batch classification:
    results = provider.classify_batch(tweets, system_prompt, model="gemini-2.0-flash")
"""

import json
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any


# =============================================================================
# API Key Storage Helpers
# =============================================================================

# Setting keys for API keys in the database
API_KEY_SETTINGS = {
    "anthropic": "anthropic_api_key",
    "gemini": "gemini_api_key",
}


def get_api_key_from_settings(provider_name: str, db_path: Path | None = None) -> str | None:
    """
    Get API key from database settings.

    Args:
        provider_name: Provider name ("anthropic", "gemini")
        db_path: Database path (uses DEFAULT_DB_PATH if None)

    Returns:
        API key string or None if not found
    """
    setting_key = API_KEY_SETTINGS.get(provider_name)
    if not setting_key:
        return None

    try:
        # Import here to avoid circular imports
        from database import DEFAULT_DB_PATH, get_setting

        path = db_path or DEFAULT_DB_PATH
        return get_setting(setting_key, path)
    except Exception:
        # If database isn't available, return None
        return None


def set_api_key_in_settings(
    provider_name: str, api_key: str, db_path: Path | None = None
) -> bool:
    """
    Store API key in database settings.

    Args:
        provider_name: Provider name ("anthropic", "gemini")
        api_key: API key to store
        db_path: Database path (uses DEFAULT_DB_PATH if None)

    Returns:
        True if successful, False otherwise
    """
    setting_key = API_KEY_SETTINGS.get(provider_name)
    if not setting_key:
        return False

    try:
        from database import DEFAULT_DB_PATH, set_setting

        path = db_path or DEFAULT_DB_PATH
        set_setting(setting_key, api_key, path)
        return True
    except Exception:
        return False


def delete_api_key_from_settings(provider_name: str, db_path: Path | None = None) -> bool:
    """
    Delete API key from database settings.

    Args:
        provider_name: Provider name ("anthropic", "gemini")
        db_path: Database path (uses DEFAULT_DB_PATH if None)

    Returns:
        True if successful, False otherwise
    """
    setting_key = API_KEY_SETTINGS.get(provider_name)
    if not setting_key:
        return False

    try:
        from database import DEFAULT_DB_PATH, delete_setting

        path = db_path or DEFAULT_DB_PATH
        delete_setting(setting_key, path)
        return True
    except Exception:
        return False


@dataclass
class ProviderConfig:
    """Configuration for a provider."""

    name: str
    description: str
    default_model: str
    available_models: list[str]
    api_key_env_var: str


class LLMProvider(ABC):
    """Abstract base class for LLM providers."""

    config: ProviderConfig

    @abstractmethod
    def classify(
        self,
        tweet_text: str,
        system_prompt: str,
        model: str | None = None,
    ) -> dict[str, Any]:
        """
        Classify a single tweet.

        Args:
            tweet_text: Formatted tweet text to classify.
            system_prompt: The system prompt for classification.
            model: Model to use (defaults to provider's default_model).

        Returns:
            Parsed JSON response dict, or error dict with "_error" key.
        """
        pass

    @abstractmethod
    def classify_batch(
        self,
        tweets_text: str,
        expected_ids: list[str],
        system_prompt: str,
        model: str | None = None,
    ) -> dict[str, dict[str, Any]]:
        """
        Classify multiple tweets in a single request.

        Args:
            tweets_text: Formatted batch of tweets to classify.
            expected_ids: List of tweet IDs to expect in the response.
            system_prompt: The system prompt for batch classification.
            model: Model to use (defaults to provider's default_model).

        Returns:
            Dict mapping tweet_id -> classification result.
        """
        pass

    def get_api_key(self, db_path: Path | None = None) -> str | None:
        """
        Get the API key for this provider.

        Checks in order:
        1. Environment variable (takes precedence for Docker/deployment flexibility)
        2. Database settings (for web UI configuration)
        """
        # Environment variable takes precedence
        env_key = os.environ.get(self.config.api_key_env_var)
        if env_key:
            return env_key

        # Fall back to database settings
        return get_api_key_from_settings(self.config.name, db_path)

    def get_model(self, model: str | None) -> str:
        """Get the model to use, falling back to default."""
        return model or self.config.default_model

    def parse_json_response(self, content: str) -> dict[str, Any]:
        """
        Parse JSON from LLM response text.

        Handles cases where JSON is wrapped in markdown code blocks or
        surrounded by extra text.
        """
        content = content.strip()

        # Try direct parse first
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            pass

        # Try to find JSON object in the response
        json_match = re.search(r"\{[^{}]*\}", content, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group())
            except json.JSONDecodeError:
                pass

        # Return error with raw content
        return {"_error": "parse_failed", "_raw": content[:500]}

    def parse_batch_response(
        self,
        content: str,
        expected_ids: list[str],
    ) -> dict[str, dict[str, Any]]:
        """
        Parse batch classification response into per-tweet results.

        The LLM returns indices (1, 2, 3...) which we map back to actual tweet IDs.
        Returns a dict mapping tweet_id -> response dict.
        """
        results: dict[str, dict[str, Any]] = {}

        def index_to_tweet_id(index: int) -> str | None:
            """Convert 1-based index to tweet ID, or None if invalid."""
            if 1 <= index <= len(expected_ids):
                return expected_ids[index - 1]
            return None

        def extract_from_list(parsed: list) -> None:
            """Extract results from a parsed JSON list."""
            for item in parsed:
                if isinstance(item, dict) and "id" in item:
                    try:
                        index = int(item["id"])
                        tweet_id = index_to_tweet_id(index)
                        if tweet_id:
                            results[tweet_id] = {
                                "approved": bool(item.get("approved", False)),
                                "reason": str(item.get("reason", "")),
                            }
                    except (ValueError, TypeError):
                        continue

        # Try to parse as JSON array
        try:
            parsed = json.loads(content.strip())
            if isinstance(parsed, list):
                extract_from_list(parsed)
        except json.JSONDecodeError:
            pass

        # If direct parse didn't work, try to find JSON array in the response
        if not results:
            array_match = re.search(r"\[[\s\S]*\]", content)
            if array_match:
                try:
                    parsed = json.loads(array_match.group())
                    if isinstance(parsed, list):
                        extract_from_list(parsed)
                except json.JSONDecodeError:
                    pass

        # If still no results, try to extract individual JSON objects
        if not results:
            for obj_match in re.finditer(r'\{[^{}]*"id"\s*:\s*"?(\d+)"?[^{}]*\}', content):
                try:
                    obj = json.loads(obj_match.group())
                    if "id" in obj:
                        index = int(obj["id"])
                        tweet_id = index_to_tweet_id(index)
                        if tweet_id:
                            results[tweet_id] = {
                                "approved": bool(obj.get("approved", False)),
                                "reason": str(obj.get("reason", "")),
                            }
                except (json.JSONDecodeError, ValueError, TypeError):
                    continue

        # Mark any missing tweets as errors
        for tweet_id in expected_ids:
            if tweet_id not in results:
                results[tweet_id] = {
                    "_error": "parse_failed",
                    "_raw": content[:200] if not results else "missing from response",
                }

        return results


# =============================================================================
# Anthropic Provider
# =============================================================================


class AnthropicProvider(LLMProvider):
    """Anthropic Claude provider."""

    config = ProviderConfig(
        name="anthropic",
        description="Anthropic Claude models (Haiku, Sonnet, Opus)",
        default_model="claude-sonnet-4-5",
        available_models=[
            "claude-haiku-4-5",
            "claude-sonnet-4-5",
            "claude-opus-4-5",
        ],
        api_key_env_var="ANTHROPIC_API_KEY",
    )

    def __init__(self):
        self._client = None

    def _get_client(self):
        """Lazy-load the Anthropic client."""
        if self._client is None:
            import anthropic

            api_key = self.get_api_key()
            if not api_key:
                raise ValueError(
                    f"API key not found. Set {self.config.api_key_env_var} environment variable."
                )
            self._client = anthropic.Anthropic(api_key=api_key)
        return self._client

    def classify(
        self,
        tweet_text: str,
        system_prompt: str,
        model: str | None = None,
    ) -> dict[str, Any]:
        import anthropic
        from anthropic.types import TextBlock

        try:
            client = self._get_client()
            response = client.messages.create(
                model=self.get_model(model),
                max_tokens=500,  # Increased for thread classification
                system=system_prompt,
                messages=[{"role": "user", "content": f"Analyze this tweet:\n\n{tweet_text}"}],
            )

            first_block = response.content[0]
            if not isinstance(first_block, TextBlock):
                return {"_error": "unexpected_response", "_message": "No text content"}

            return self.parse_json_response(first_block.text)

        except anthropic.APIError as e:
            return {"_error": "api_error", "_message": str(e)[:200]}

    def classify_batch(
        self,
        tweets_text: str,
        expected_ids: list[str],
        system_prompt: str,
        model: str | None = None,
    ) -> dict[str, dict[str, Any]]:
        import anthropic
        from anthropic.types import TextBlock

        try:
            client = self._get_client()
            response = client.messages.create(
                model=self.get_model(model),
                max_tokens=100 * len(expected_ids),
                system=system_prompt,
                messages=[
                    {
                        "role": "user",
                        "content": f"Classify these tweets:\n\n{tweets_text}",
                    }
                ],
            )

            first_block = response.content[0]
            if not isinstance(first_block, TextBlock):
                return {
                    tid: {"_error": "unexpected_response", "_message": "No text content"}
                    for tid in expected_ids
                }

            return self.parse_batch_response(first_block.text, expected_ids)

        except anthropic.RateLimitError as e:
            return {tid: {"_error": "rate_limit", "_message": str(e)[:100]} for tid in expected_ids}
        except anthropic.APIError as e:
            return {tid: {"_error": "api_error", "_message": str(e)[:200]} for tid in expected_ids}


# =============================================================================
# Google Gemini Provider
# =============================================================================


class GeminiProvider(LLMProvider):
    """Google Gemini provider."""

    config = ProviderConfig(
        name="gemini",
        description="Google Gemini models (Flash, Pro)",
        default_model="gemini-3-flash-preview",
        available_models=[
            "gemini-3-flash-preview",
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite",
        ],
        api_key_env_var="GEMINI_API_KEY",
    )

    def __init__(self):
        self._client: Any = None

    def _get_client(self) -> Any:
        """Lazy-load the Gemini client."""
        if self._client is None:
            try:
                from google import genai  # type: ignore[import-not-found]
            except ImportError:
                raise ImportError(
                    "google-genai package not installed. Install with: pip install google-genai"
                ) from None

            api_key = self.get_api_key()
            if not api_key:
                raise ValueError(
                    f"API key not found. Set {self.config.api_key_env_var} environment variable."
                )
            self._client = genai.Client(api_key=api_key)
        return self._client

    def classify(
        self,
        tweet_text: str,
        system_prompt: str,
        model: str | None = None,
    ) -> dict[str, Any]:
        try:
            from google import genai  # type: ignore[import-not-found]
            from google.genai import types  # type: ignore[import-not-found]
        except ImportError:
            return {
                "_error": "missing_dependency",
                "_message": "google-genai package not installed",
            }

        try:
            client = self._get_client()
            response = client.models.generate_content(
                model=self.get_model(model),
                contents=f"Analyze this tweet:\n\n{tweet_text}",
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    max_output_tokens=500,  # Increased for thread classification
                ),
            )

            if not response.text:
                return {"_error": "unexpected_response", "_message": "No text content"}

            return self.parse_json_response(response.text)

        except genai.errors.APIError as e:
            return {"_error": "api_error", "_message": str(e)[:200]}
        except ImportError:
            return {
                "_error": "missing_dependency",
                "_message": "google-genai package not installed",
            }

    def classify_batch(
        self,
        tweets_text: str,
        expected_ids: list[str],
        system_prompt: str,
        model: str | None = None,
    ) -> dict[str, dict[str, Any]]:
        try:
            from google import genai  # type: ignore[import-not-found]
            from google.genai import types  # type: ignore[import-not-found]
        except ImportError:
            return {
                tid: {
                    "_error": "missing_dependency",
                    "_message": "google-genai package not installed",
                }
                for tid in expected_ids
            }

        try:
            client = self._get_client()
            response = client.models.generate_content(
                model=self.get_model(model),
                contents=f"Classify these tweets:\n\n{tweets_text}",
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    max_output_tokens=100 * len(expected_ids),
                ),
            )

            if not response.text:
                return {
                    tid: {"_error": "unexpected_response", "_message": "No text content"}
                    for tid in expected_ids
                }

            return self.parse_batch_response(response.text, expected_ids)

        except genai.errors.APIError as e:
            return {tid: {"_error": "api_error", "_message": str(e)[:200]} for tid in expected_ids}


# =============================================================================
# Provider Registry
# =============================================================================

# Map of provider names to provider classes
PROVIDERS: dict[str, type[LLMProvider]] = {
    "anthropic": AnthropicProvider,
    "gemini": GeminiProvider,
}

# Cached provider instances
_provider_instances: dict[str, LLMProvider] = {}


def get_provider(provider_name: str) -> LLMProvider:
    """
    Get a provider instance by name.

    Providers are cached for reuse (to maintain client connections).

    Args:
        provider_name: Name of the provider ("anthropic", "gemini", etc.)

    Returns:
        LLMProvider instance.

    Raises:
        ValueError: If provider name is not recognized.
    """
    if provider_name not in PROVIDERS:
        available = ", ".join(PROVIDERS.keys())
        raise ValueError(f"Unknown provider: {provider_name}. Available providers: {available}")

    if provider_name not in _provider_instances:
        _provider_instances[provider_name] = PROVIDERS[provider_name]()

    return _provider_instances[provider_name]


def list_providers(db_path: Path | None = None) -> list[dict[str, Any]]:
    """
    List available providers with their configurations.

    Args:
        db_path: Database path for checking stored API keys

    Returns:
        List of provider info dicts with name, description, default_model, etc.
    """
    result = []
    for provider_class in PROVIDERS.values():
        config = provider_class.config
        # Check both env var and database settings
        env_key_set = bool(os.environ.get(config.api_key_env_var))
        db_key_set = bool(get_api_key_from_settings(config.name, db_path))
        result.append(
            {
                "name": config.name,
                "description": config.description,
                "default_model": config.default_model,
                "available_models": config.available_models,
                "api_key_env_var": config.api_key_env_var,
                "api_key_set": env_key_set or db_key_set,
                "api_key_source": "env" if env_key_set else ("database" if db_key_set else None),
            }
        )
    return result


def get_default_provider() -> str:
    """Get the default provider name."""
    return "anthropic"
