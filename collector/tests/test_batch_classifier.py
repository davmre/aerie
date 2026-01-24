"""Tests for the batch classifier (chain-aware version)."""

from unittest.mock import Mock, patch

from batch_classifier import (
    build_batch_prompt,
    classify_chains_batch,
    format_chain_for_batch,
    format_chains_batch,
)
from providers import get_provider


class TestChainFormatting:
    """Tests for chain formatting functions."""

    def test_format_chain_single_tweet(self):
        """Single-tweet chain uses simpler format."""
        chain = {
            "tweets": [
                {
                    "id": "123456",
                    "text": "Hello world!",
                    "author_username": "testuser",
                    "author_display_name": "Test User",
                }
            ],
            "unclassified_ids": {"123456"},
        }

        result = format_chain_for_batch(chain, 1)

        assert "[Chain 1]" in result
        assert "@testuser (Test User)" in result
        assert "Hello world!" in result
        # Full tweet ID should NOT be in the output
        assert "123456" not in result

    def test_format_chain_with_verified(self):
        """Verified authors get a [verified] tag."""
        chain = {
            "tweets": [
                {
                    "id": "123",
                    "text": "Tweet",
                    "author_username": "verified_user",
                    "author_display_name": "Verified User",
                    "author_verified": True,
                }
            ],
            "unclassified_ids": {"123"},
        }

        result = format_chain_for_batch(chain, 1)
        assert "[verified]" in result

    def test_format_chain_conversation_thread(self):
        """Multi-tweet chains are formatted as conversation threads."""
        chain = {
            "tweets": [
                {
                    "id": "1",
                    "text": "Original post",
                    "author_username": "alice",
                    "author_display_name": "Alice",
                },
                {
                    "id": "2",
                    "text": "Reply to original",
                    "author_username": "bob",
                    "author_display_name": "Bob",
                    "reply_to_username": "alice",
                },
            ],
            "unclassified_ids": {"1", "2"},
        }

        result = format_chain_for_batch(chain, 1)

        assert "[Chain 1]" in result
        assert "CONVERSATION THREAD:" in result
        assert "Tweet 1" in result
        assert "Tweet 2" in result
        assert "@alice" in result
        assert "@bob" in result
        assert "Original post" in result
        assert "Reply to original" in result

    def test_format_chains_batch(self):
        """format_chains_batch combines multiple chains with separators."""
        chains = [
            {
                "tweets": [
                    {"id": "1", "text": "First", "author_username": "a", "author_display_name": "A"}
                ],
                "unclassified_ids": {"1"},
            },
            {
                "tweets": [
                    {"id": "2", "text": "Second", "author_username": "b", "author_display_name": "B"}
                ],
                "unclassified_ids": {"2"},
            },
        ]

        result = format_chains_batch(chains)

        assert "[Chain 1]" in result
        assert "[Chain 2]" in result
        assert "First" in result
        assert "Second" in result
        # Chains should be separated by dividers
        assert "---" in result


class TestBatchPrompt:
    """Tests for batch prompt building."""

    def test_build_batch_prompt_includes_base(self):
        """Batch prompt includes the base prompt text."""
        base = "You are a tweet classifier. Decide if each chain is good."
        result = build_batch_prompt(base)

        assert base in result
        assert "JSON array" in result
        # Should now use "chain" field instead of "id"
        assert '"chain"' in result
        assert '"approved"' in result

    def test_build_batch_prompt_mentions_chains(self):
        """Batch prompt refers to 'chains' not 'tweets'."""
        base = "Classify content"
        result = build_batch_prompt(base)

        assert "chain" in result.lower()
        assert "conversation" in result.lower()

    def test_build_batch_prompt_with_context(self):
        """Batch prompt includes context when provided."""
        base = "Classify content"
        context = "Current event: Major tech conference happening"
        result = build_batch_prompt(base, context_text=context)

        assert "Major tech conference" in result
        assert "SITUATIONAL CONTEXT" in result


class TestResponseParsing:
    """Tests for parsing LLM responses with chain field.

    Note: For chains, the LLM returns chain indices (1, 2, 3...) which are
    used directly as keys in the result dict.
    """

    def test_parse_valid_chain_response(self):
        """Parse a well-formed JSON array response with chain field."""
        response = """[
            {"chain": 1, "approved": true, "reason": "Good content"},
            {"chain": 2, "approved": false, "reason": "Spam thread"}
        ]"""

        # Use provider's parse_batch_response with id_field="chain"
        provider = get_provider("anthropic")
        result = provider.parse_batch_response(response, [1, 2], id_field="chain")

        assert result[1]["approved"] is True
        assert result[1]["reason"] == "Good content"
        assert result[2]["approved"] is False
        assert result[2]["reason"] == "Spam thread"

    def test_parse_chain_with_surrounding_text(self):
        """Parse JSON array even with surrounding text."""
        response = """Here are my classifications:

        [
            {"chain": 1, "approved": true, "reason": "Informative thread"}
        ]

        Let me know if you need anything else."""

        provider = get_provider("anthropic")
        result = provider.parse_batch_response(response, [1], id_field="chain")

        assert result[1]["approved"] is True

    def test_parse_marks_missing_chains_as_error(self):
        """Missing chains are marked with error."""
        # Only chain 1 returned, chain 2 is missing
        response = '[{"chain": 1, "approved": true, "reason": "Good"}]'

        provider = get_provider("anthropic")
        result = provider.parse_batch_response(response, [1, 2], id_field="chain")

        assert result[1]["approved"] is True
        assert "_error" in result[2]

    def test_parse_handles_malformed_json(self):
        """Malformed JSON returns errors for all expected chains."""
        response = "This is not JSON at all"

        provider = get_provider("anthropic")
        result = provider.parse_batch_response(response, [1, 2], id_field="chain")

        assert "_error" in result[1]
        assert "_error" in result[2]

    def test_parse_extracts_individual_chain_objects(self):
        """Can extract individual JSON objects when array parsing fails."""
        response = """Chain 1: {"chain": 1, "approved": true, "reason": "Good"}
        Chain 2: {"chain": 2, "approved": false, "reason": "Bad"}"""

        provider = get_provider("anthropic")
        result = provider.parse_batch_response(response, [1, 2], id_field="chain")

        assert result[1]["approved"] is True
        assert result[2]["approved"] is False

    def test_parse_coerces_types(self):
        """Values are coerced to expected types."""
        # Chain index can be int or string "1"
        response = '[{"chain": "1", "approved": 1, "reason": 42}]'

        provider = get_provider("anthropic")
        result = provider.parse_batch_response(response, [1], id_field="chain")

        # approved should be bool, reason should be string
        assert result[1]["approved"] is True
        assert result[1]["reason"] == "42"

    def test_parse_filters_invalid_indices(self):
        """Invalid indices from LLM are filtered out."""
        # LLM returns chain 99 which is out of range (only 2 chains)
        response = """[
            {"chain": 1, "approved": true, "reason": "Good"},
            {"chain": 99, "approved": true, "reason": "Invalid index"},
            {"chain": 2, "approved": false, "reason": "Bad"}
        ]"""

        provider = get_provider("anthropic")
        result = provider.parse_batch_response(response, [1, 2], id_field="chain")

        # Valid indices should be present
        assert result[1]["approved"] is True
        assert result[2]["approved"] is False
        # Invalid index should not create an extra entry (99 maps to error placeholder)
        assert len(result) == 2

    def test_parse_with_legacy_id_field(self):
        """Legacy id_field="id" still works for backwards compatibility."""
        response = """[
            {"id": 1, "approved": true, "reason": "Good"},
            {"id": 2, "approved": false, "reason": "Bad"}
        ]"""

        provider = get_provider("anthropic")
        # With id_field="id" (default), indices map to expected_ids
        result = provider.parse_batch_response(response, ["tweet_123", "tweet_456"])

        assert result["tweet_123"]["approved"] is True
        assert result["tweet_456"]["approved"] is False


class TestClassifyChainsBatch:
    """Tests for the main chain classification function."""

    def test_classify_empty_list_returns_empty(self):
        """Empty chain list returns empty results."""
        result = classify_chains_batch([], "prompt1")
        assert result == {}

    @patch("batch_classifier.get_prompt")
    def test_classify_missing_prompt_returns_error(self, mock_get_prompt):
        """Missing prompt returns error for all chains."""
        mock_get_prompt.return_value = None

        chains = [
            {
                "tweets": [{"id": "123", "text": "Test"}],
                "unclassified_ids": {"123"},
            }
        ]
        result = classify_chains_batch(chains, "nonexistent_prompt")

        # Result is keyed by chain index (1-based)
        assert result[1]["_error"] == "prompt_not_found"

    @patch("batch_classifier.get_provider")
    @patch("batch_classifier.get_prompt")
    def test_classify_success(self, mock_get_prompt, mock_get_provider):
        """Successful classification returns parsed results."""
        mock_get_prompt.return_value = {
            "id": "test_prompt",
            "prompt_text": "Classify chains",
        }

        # Mock the provider
        mock_provider = Mock()
        mock_provider.get_api_key.return_value = "test-api-key"
        mock_provider.classify_batch.return_value = {
            1: {"approved": True, "reason": "Good thread"}
        }
        mock_get_provider.return_value = mock_provider

        chains = [
            {
                "tweets": [
                    {"id": "123", "text": "Test tweet", "author_username": "user"}
                ],
                "unclassified_ids": {"123"},
            }
        ]
        result = classify_chains_batch(chains, "test_prompt")

        # Result is keyed by chain index
        assert result[1]["approved"] is True
        assert result[1]["reason"] == "Good thread"

    @patch("batch_classifier.get_provider")
    @patch("batch_classifier.get_prompt")
    def test_classify_api_error(self, mock_get_prompt, mock_get_provider):
        """API errors are captured in results."""
        mock_get_prompt.return_value = {
            "id": "test_prompt",
            "prompt_text": "Classify chains",
        }

        # Mock the provider to return an error result
        mock_provider = Mock()
        mock_provider.get_api_key.return_value = "test-api-key"
        mock_provider.classify_batch.return_value = {
            1: {"_error": "api_error", "_message": "Server error"}
        }
        mock_get_provider.return_value = mock_provider

        chains = [
            {
                "tweets": [{"id": "123", "text": "Test"}],
                "unclassified_ids": {"123"},
            }
        ]
        result = classify_chains_batch(chains, "test_prompt")

        assert result[1]["_error"] == "api_error"

    @patch("batch_classifier.get_provider")
    @patch("batch_classifier.get_prompt")
    def test_classify_no_api_key_returns_error(self, mock_get_prompt, mock_get_provider):
        """Missing API key returns error for all chains."""
        mock_get_prompt.return_value = {
            "id": "test_prompt",
            "prompt_text": "Classify chains",
        }

        # Mock the provider with no API key
        mock_provider = Mock()
        mock_provider.get_api_key.return_value = None
        mock_provider.config.api_key_env_var = "ANTHROPIC_API_KEY"
        mock_get_provider.return_value = mock_provider

        chains = [
            {
                "tweets": [{"id": "123", "text": "Test"}],
                "unclassified_ids": {"123"},
            }
        ]
        result = classify_chains_batch(chains, "test_prompt")

        assert result[1]["_error"] == "no_api_key"

    @patch("batch_classifier.get_provider")
    @patch("batch_classifier.get_prompt")
    def test_classify_uses_chain_id_field(self, mock_get_prompt, mock_get_provider):
        """Verify classify_batch is called with id_field='chain'."""
        mock_get_prompt.return_value = {
            "id": "test_prompt",
            "prompt_text": "Classify chains",
        }

        mock_provider = Mock()
        mock_provider.get_api_key.return_value = "test-api-key"
        mock_provider.classify_batch.return_value = {1: {"approved": True, "reason": "Good"}}
        mock_get_provider.return_value = mock_provider

        chains = [
            {
                "tweets": [{"id": "123", "text": "Test"}],
                "unclassified_ids": {"123"},
            }
        ]
        classify_chains_batch(chains, "test_prompt")

        # Check that classify_batch was called with id_field="chain"
        call_kwargs = mock_provider.classify_batch.call_args
        assert call_kwargs.kwargs.get("id_field") == "chain" or call_kwargs[1].get("id_field") == "chain"
