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
import logging
import os
import re
import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config import ClassificationConfig, LLMConfig
from logging_config import truncate_for_log

# Get logger for provider operations
logger = logging.getLogger("aerie.providers")


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
    except (sqlite3.Error, OSError) as e:
        # If database isn't available, return None
        logger.debug(f"Could not read API key from database: {e}")
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
    except (sqlite3.Error, OSError) as e:
        logger.warning(f"Failed to save API key to database: {e}")
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
    except (sqlite3.Error, OSError) as e:
        logger.warning(f"Failed to delete API key from database: {e}")
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
        content_text: str,
        expected_ids: list[str | int],
        system_prompt: str,
        model: str | None = None,
        id_field: str = "id",
    ) -> dict[str | int, dict[str, Any]]:
        """
        Classify multiple items (tweets or chains) in a single request.

        Args:
            content_text: Formatted batch of content to classify.
            expected_ids: List of IDs to expect in the response (tweet IDs or chain indices).
            system_prompt: The system prompt for batch classification.
            model: Model to use (defaults to provider's default_model).
            id_field: Field name for IDs in the response ("id" for tweets, "chain" for chains).

        Returns:
            Dict mapping id -> classification result.
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
        expected_ids: list[str | int],
        id_field: str = "id",
    ) -> dict[str | int, dict[str, Any]]:
        """
        Parse batch classification response into per-item results.

        For tweet batches (id_field="id"): LLM returns indices (1, 2, 3...) which
        we map back to actual tweet IDs from expected_ids.

        For chain batches (id_field="chain"): LLM returns chain indices (1, 2, 3...)
        which we return directly as keys.

        Args:
            content: Raw LLM response text
            expected_ids: List of expected IDs (tweet IDs for tweets, indices for chains)
            id_field: Field name for IDs in the response ("id" or "chain")

        Returns:
            Dict mapping id -> response dict
        """
        results: dict[str | int, dict[str, Any]] = {}

        # For chains, expected_ids are the indices themselves (1, 2, 3...)
        # For tweets, expected_ids are tweet IDs and we map from indices
        is_chain_mode = id_field == "chain"

        def index_to_result_id(index: int) -> str | int | None:
            """Convert LLM index to result key, or None if invalid."""
            if 1 <= index <= len(expected_ids):
                if is_chain_mode:
                    # For chains, the index IS the result key
                    return index
                else:
                    # For tweets, map index to tweet ID
                    return expected_ids[index - 1]
            return None

        def extract_from_list(parsed: list) -> None:
            """Extract results from a parsed JSON list."""
            for item in parsed:
                if isinstance(item, dict) and id_field in item:
                    try:
                        index = int(item[id_field])
                        result_id = index_to_result_id(index)
                        if result_id is not None:
                            results[result_id] = {
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
            # Build regex pattern dynamically based on id_field
            pattern = rf'\{{[^{{}}]*"{id_field}"\s*:\s*"?(\d+)"?[^{{}}]*\}}'
            for obj_match in re.finditer(pattern, content):
                try:
                    obj = json.loads(obj_match.group())
                    if id_field in obj:
                        index = int(obj[id_field])
                        result_id = index_to_result_id(index)
                        if result_id is not None:
                            results[result_id] = {
                                "approved": bool(obj.get("approved", False)),
                                "reason": str(obj.get("reason", "")),
                            }
                except (json.JSONDecodeError, ValueError, TypeError):
                    continue

        # Mark any missing items as errors
        for expected_id in expected_ids:
            if expected_id not in results:
                results[expected_id] = {
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
        default_model=LLMConfig.ANTHROPIC_DEFAULT_MODEL,
        available_models=LLMConfig.ANTHROPIC_MODELS,
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

        actual_model = self.get_model(model)
        user_message = f"Analyze this tweet:\n\n{tweet_text}"
        max_tokens = LLMConfig.SINGLE_REQUEST_MAX_TOKENS

        # Log request details
        logger.debug(
            "Anthropic API request\n"
            f"  model: {actual_model}\n"
            f"  system_prompt: {truncate_for_log(system_prompt, 200)}\n"
            f"  user_message: {truncate_for_log(user_message, 500)}\n"
            f"  max_tokens: {max_tokens}"
        )

        try:
            client = self._get_client()
            response = client.messages.create(
                model=actual_model,
                max_tokens=max_tokens,
                system=system_prompt,
                messages=[{"role": "user", "content": user_message}],
            )

            first_block = response.content[0]
            if not isinstance(first_block, TextBlock):
                logger.warning("Anthropic API returned unexpected response type (no text content)")
                return {"_error": "unexpected_response", "_message": "No text content"}

            # Log response
            response_text = first_block.text
            logger.debug(f"Anthropic API response: {truncate_for_log(response_text, 500)}")

            return self.parse_json_response(response_text)

        except anthropic.APIError as e:
            logger.warning(f"Anthropic API error: {e}")
            return {"_error": "api_error", "_message": str(e)[:200]}

    def classify_batch(
        self,
        content_text: str,
        expected_ids: list[str | int],
        system_prompt: str,
        model: str | None = None,
        id_field: str = "id",
    ) -> dict[str | int, dict[str, Any]]:
        import anthropic
        from anthropic.types import TextBlock

        actual_model = self.get_model(model)
        # Use appropriate message based on content type
        content_type = "chains" if id_field == "chain" else "tweets"
        user_message = f"Classify these {content_type}:\n\n{content_text}"
        max_tokens = LLMConfig.BATCH_TOKENS_PER_ITEM * len(expected_ids)

        # Log request details
        logger.debug(
            "Anthropic API batch request\n"
            f"  model: {actual_model}\n"
            f"  batch_size: {len(expected_ids)}\n"
            f"  system_prompt: {truncate_for_log(system_prompt, 200)}\n"
            f"  user_message: {truncate_for_log(user_message, 500)}\n"
            f"  max_tokens: {max_tokens}"
        )

        try:
            client = self._get_client()
            response = client.messages.create(
                model=actual_model,
                max_tokens=max_tokens,
                system=system_prompt,
                messages=[
                    {
                        "role": "user",
                        "content": user_message,
                    }
                ],
            )

            first_block = response.content[0]
            if not isinstance(first_block, TextBlock):
                logger.warning("Anthropic API batch returned unexpected response type (no text content)")
                return {
                    tid: {"_error": "unexpected_response", "_message": "No text content"}
                    for tid in expected_ids
                }

            # Log response
            response_text = first_block.text
            logger.debug(f"Anthropic API batch response: {truncate_for_log(response_text, 500)}")

            return self.parse_batch_response(response_text, expected_ids, id_field)

        except anthropic.RateLimitError as e:
            logger.warning(f"Anthropic API rate limit: {e}")
            return {tid: {"_error": "rate_limit", "_message": str(e)[:100]} for tid in expected_ids}
        except anthropic.APIError as e:
            logger.warning(f"Anthropic API error: {e}")
            return {tid: {"_error": "api_error", "_message": str(e)[:200]} for tid in expected_ids}


# =============================================================================
# Google Gemini Provider
# =============================================================================


class GeminiProvider(LLMProvider):
    """Google Gemini provider."""

    config = ProviderConfig(
        name="gemini",
        description="Google Gemini models (Flash, Pro)",
        default_model=LLMConfig.GEMINI_DEFAULT_MODEL,
        available_models=LLMConfig.GEMINI_MODELS,
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

        actual_model = self.get_model(model)
        user_message = f"Analyze this tweet:\n\n{tweet_text}"
        max_tokens = LLMConfig.SINGLE_REQUEST_MAX_TOKENS

        # Log request details
        logger.debug(
            "Gemini API request\n"
            f"  model: {actual_model}\n"
            f"  system_prompt: {truncate_for_log(system_prompt, 200)}\n"
            f"  user_message: {truncate_for_log(user_message, 500)}\n"
            f"  max_tokens: {max_tokens}"
        )

        try:
            client = self._get_client()
            response = client.models.generate_content(
                model=actual_model,
                contents=user_message,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    max_output_tokens=max_tokens,
                ),
            )

            if not response.text:
                logger.warning("Gemini API returned unexpected response (no text content)")
                return {"_error": "unexpected_response", "_message": "No text content"}

            # Log response
            response_text = response.text
            logger.debug(f"Gemini API response: {truncate_for_log(response_text, 500)}")

            return self.parse_json_response(response_text)

        except genai.errors.APIError as e:
            logger.warning(f"Gemini API error: {e}")
            return {"_error": "api_error", "_message": str(e)[:200]}
        except ImportError:
            return {
                "_error": "missing_dependency",
                "_message": "google-genai package not installed",
            }

    def classify_batch(
        self,
        content_text: str,
        expected_ids: list[str | int],
        system_prompt: str,
        model: str | None = None,
        id_field: str = "id",
    ) -> dict[str | int, dict[str, Any]]:
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

        actual_model = self.get_model(model)
        # Use appropriate message based on content type
        content_type = "chains" if id_field == "chain" else "tweets"
        user_message = f"Classify these {content_type}:\n\n{content_text}"
        max_tokens = LLMConfig.BATCH_TOKENS_PER_ITEM * len(expected_ids)

        # Log request details
        logger.debug(
            "Gemini API batch request\n"
            f"  model: {actual_model}\n"
            f"  batch_size: {len(expected_ids)}\n"
            f"  system_prompt: {truncate_for_log(system_prompt, 200)}\n"
            f"  user_message: {truncate_for_log(user_message, 500)}\n"
            f"  max_tokens: {max_tokens}"
        )

        try:
            client = self._get_client()
            response = client.models.generate_content(
                model=actual_model,
                contents=user_message,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    max_output_tokens=max_tokens,
                ),
            )

            if not response.text:
                logger.warning("Gemini API batch returned unexpected response (no text content)")
                return {
                    tid: {"_error": "unexpected_response", "_message": "No text content"}
                    for tid in expected_ids
                }

            # Log response
            response_text = response.text
            logger.debug(f"Gemini API batch response: {truncate_for_log(response_text, 500)}")

            return self.parse_batch_response(response_text, expected_ids, id_field)

        except genai.errors.APIError as e:
            logger.warning(f"Gemini API error: {e}")
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
    return ClassificationConfig.DEFAULT_PROVIDER
