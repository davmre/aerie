"""Tests for the batch classifier."""

from unittest.mock import Mock, patch

from batch_classifier import (
    build_batch_prompt,
    classify_tweets_batch,
    format_tweet_for_batch,
    format_tweets_batch,
    parse_batch_response,
)


class TestTweetFormatting:
    """Tests for tweet formatting functions."""

    def test_format_tweet_basic(self):
        """Basic tweet formatting includes ID, author, and text."""
        tweet = {
            "id": "123456",
            "text": "Hello world!",
            "author_username": "testuser",
            "author_display_name": "Test User",
        }

        result = format_tweet_for_batch(tweet, 1)

        assert "[1] ID: 123456" in result
        assert "@testuser (Test User)" in result
        assert "Hello world!" in result

    def test_format_tweet_with_verified(self):
        """Verified authors get a [verified] tag."""
        tweet = {
            "id": "123",
            "text": "Tweet",
            "author_username": "verified_user",
            "author_display_name": "Verified User",
            "author_verified": True,
        }

        result = format_tweet_for_batch(tweet, 1)
        assert "[verified]" in result

    def test_format_tweet_with_context(self):
        """Tweet context (retweet, quote, reply) is included."""
        tweet = {
            "id": "123",
            "text": "My reply",
            "author_username": "user",
            "author_display_name": "User",
            "is_retweet": False,
            "is_quote": True,
            "reply_to_username": "other_user",
        }

        result = format_tweet_for_batch(tweet, 1)
        assert "quote tweet" in result
        assert "reply to @other_user" in result

    def test_format_tweet_with_metrics(self):
        """Engagement metrics are included when present."""
        tweet = {
            "id": "123",
            "text": "Popular tweet",
            "author_username": "user",
            "author_display_name": "User",
            "like_count": 100,
            "retweet_count": 50,
            "reply_count": 25,
        }

        result = format_tweet_for_batch(tweet, 1)
        assert "100 likes" in result
        assert "50 retweets" in result
        assert "25 replies" in result

    def test_format_tweet_no_zero_metrics(self):
        """Zero-value metrics are not included."""
        tweet = {
            "id": "123",
            "text": "Unpopular tweet",
            "author_username": "user",
            "author_display_name": "User",
            "like_count": 0,
            "retweet_count": 0,
        }

        result = format_tweet_for_batch(tweet, 1)
        assert "likes" not in result
        assert "retweets" not in result

    def test_format_tweets_batch(self):
        """format_tweets_batch combines multiple tweets."""
        tweets = [
            {"id": "1", "text": "First", "author_username": "a", "author_display_name": "A"},
            {"id": "2", "text": "Second", "author_username": "b", "author_display_name": "B"},
        ]

        result = format_tweets_batch(tweets)

        assert "[1] ID: 1" in result
        assert "[2] ID: 2" in result
        assert "First" in result
        assert "Second" in result


class TestBatchPrompt:
    """Tests for batch prompt building."""

    def test_build_batch_prompt_includes_base(self):
        """Batch prompt includes the base prompt text."""
        base = "You are a tweet classifier. Decide if each tweet is good."
        result = build_batch_prompt(base)

        assert base in result
        assert "JSON array" in result
        assert '"id"' in result
        assert '"approved"' in result


class TestResponseParsing:
    """Tests for parsing LLM responses."""

    def test_parse_valid_json_array(self):
        """Parse a well-formed JSON array response."""
        response = """[
            {"id": "123", "approved": true, "reason": "Good content"},
            {"id": "456", "approved": false, "reason": "Spam"}
        ]"""

        result = parse_batch_response(response, ["123", "456"])

        assert result["123"]["approved"] is True
        assert result["123"]["reason"] == "Good content"
        assert result["456"]["approved"] is False
        assert result["456"]["reason"] == "Spam"

    def test_parse_json_with_surrounding_text(self):
        """Parse JSON array even with surrounding text."""
        response = """Here are my classifications:

        [
            {"id": "123", "approved": true, "reason": "Informative"}
        ]

        Let me know if you need anything else."""

        result = parse_batch_response(response, ["123"])

        assert result["123"]["approved"] is True

    def test_parse_marks_missing_tweets_as_error(self):
        """Missing tweets are marked with error."""
        response = '[{"id": "123", "approved": true, "reason": "Good"}]'

        result = parse_batch_response(response, ["123", "456"])

        assert result["123"]["approved"] is True
        assert "_error" in result["456"]

    def test_parse_handles_malformed_json(self):
        """Malformed JSON returns errors for all expected tweets."""
        response = "This is not JSON at all"

        result = parse_batch_response(response, ["123", "456"])

        assert "_error" in result["123"]
        assert "_error" in result["456"]

    def test_parse_extracts_individual_objects(self):
        """Can extract individual JSON objects when array parsing fails."""
        response = """Tweet 1: {"id": "123", "approved": true, "reason": "Good"}
        Tweet 2: {"id": "456", "approved": false, "reason": "Bad"}"""

        result = parse_batch_response(response, ["123", "456"])

        assert result["123"]["approved"] is True
        assert result["456"]["approved"] is False

    def test_parse_coerces_types(self):
        """Values are coerced to expected types."""
        response = '[{"id": 123, "approved": 1, "reason": 42}]'

        result = parse_batch_response(response, ["123"])

        # id should be string, approved should be bool, reason should be string
        assert result["123"]["approved"] is True
        assert result["123"]["reason"] == "42"


class TestClassifyTweetsBatch:
    """Tests for the main classification function."""

    def test_classify_empty_list_returns_empty(self):
        """Empty tweet list returns empty results."""
        result = classify_tweets_batch([], "prompt1")
        assert result == {}

    @patch("batch_classifier.get_prompt")
    def test_classify_missing_prompt_returns_error(self, mock_get_prompt):
        """Missing prompt returns error for all tweets."""
        mock_get_prompt.return_value = None

        tweets = [{"id": "123", "text": "Test"}]
        result = classify_tweets_batch(tweets, "nonexistent_prompt")

        assert result["123"]["_error"] == "prompt_not_found"

    @patch("batch_classifier.get_provider")
    @patch("batch_classifier.get_prompt")
    def test_classify_success(self, mock_get_prompt, mock_get_provider):
        """Successful classification returns parsed results."""
        mock_get_prompt.return_value = {
            "id": "test_prompt",
            "prompt_text": "Classify tweets",
        }

        # Mock the provider
        mock_provider = Mock()
        mock_provider.get_api_key.return_value = "test-api-key"
        mock_provider.classify_batch.return_value = {
            "123": {"approved": True, "reason": "Good"}
        }
        mock_get_provider.return_value = mock_provider

        tweets = [{"id": "123", "text": "Test tweet", "author_username": "user"}]
        result = classify_tweets_batch(tweets, "test_prompt")

        assert result["123"]["approved"] is True
        assert result["123"]["reason"] == "Good"

    @patch("batch_classifier.get_provider")
    @patch("batch_classifier.get_prompt")
    def test_classify_api_error(self, mock_get_prompt, mock_get_provider):
        """API errors are captured in results."""
        mock_get_prompt.return_value = {
            "id": "test_prompt",
            "prompt_text": "Classify tweets",
        }

        # Mock the provider to return an error result
        mock_provider = Mock()
        mock_provider.get_api_key.return_value = "test-api-key"
        mock_provider.classify_batch.return_value = {
            "123": {"_error": "api_error", "_message": "Server error"}
        }
        mock_get_provider.return_value = mock_provider

        tweets = [{"id": "123", "text": "Test"}]
        result = classify_tweets_batch(tweets, "test_prompt")

        assert result["123"]["_error"] == "api_error"

    @patch("batch_classifier.get_provider")
    @patch("batch_classifier.get_prompt")
    def test_classify_no_api_key_returns_error(self, mock_get_prompt, mock_get_provider):
        """Missing API key returns error for all tweets."""
        mock_get_prompt.return_value = {
            "id": "test_prompt",
            "prompt_text": "Classify tweets",
        }

        # Mock the provider with no API key
        mock_provider = Mock()
        mock_provider.get_api_key.return_value = None
        mock_provider.config.api_key_env_var = "ANTHROPIC_API_KEY"
        mock_get_provider.return_value = mock_provider

        tweets = [{"id": "123", "text": "Test"}]
        result = classify_tweets_batch(tweets, "test_prompt")

        assert result["123"]["_error"] == "no_api_key"
