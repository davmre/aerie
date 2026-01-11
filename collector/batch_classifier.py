"""
Batch tweet classification using multi-tweet LLM requests.

This module provides efficient classification by sending multiple tweets
in a single LLM request, reducing costs by ~3.5x compared to single-tweet
requests.
"""

from pathlib import Path

from database import DEFAULT_DB_PATH, get_prompt, store_prompt_response
from providers import get_default_provider, get_provider


# Re-export parse_batch_response for backwards compatibility with existing code
def parse_batch_response(response_text: str, expected_ids: list[str]) -> dict[str, dict]:
    """
    Parse batch classification response into per-tweet results.

    This is a convenience wrapper around LLMProvider.parse_batch_response.
    """
    # Create a temporary instance just to access the parsing logic
    provider = get_provider(get_default_provider())
    return provider.parse_batch_response(response_text, expected_ids)


def format_tweet_for_batch(tweet: dict, index: int) -> str:
    """Format a single tweet for inclusion in a batch request."""
    parts = []

    # Header with just the index (no full tweet ID - simpler for LLM to echo back)
    parts.append(f"[Tweet {index}]")

    # Author info
    author = tweet.get("author_username") or "unknown"
    display_name = tweet.get("author_display_name") or author
    author_line = f"@{author} ({display_name})"
    if tweet.get("author_verified"):
        author_line += " [verified]"
    parts.append(author_line)

    # Tweet text
    parts.append(tweet.get("text", ""))

    # Context
    context_parts = []
    if tweet.get("is_retweet"):
        context_parts.append("retweet")
    if tweet.get("is_quote"):
        context_parts.append("quote tweet")
    if tweet.get("reply_to_username"):
        context_parts.append(f"reply to @{tweet['reply_to_username']}")
    if context_parts:
        parts.append(f"[{', '.join(context_parts)}]")

    # Engagement metrics
    metrics = []
    if tweet.get("like_count", 0) > 0:
        metrics.append(f"{tweet['like_count']} likes")
    if tweet.get("retweet_count", 0) > 0:
        metrics.append(f"{tweet['retweet_count']} retweets")
    if tweet.get("reply_count", 0) > 0:
        metrics.append(f"{tweet['reply_count']} replies")
    if metrics:
        parts.append(f"[{', '.join(metrics)}]")

    return "\n".join(parts)


def format_tweets_batch(tweets: list[dict]) -> str:
    """Format multiple tweets for a batch classification request."""
    formatted = []
    for i, tweet in enumerate(tweets, 1):
        formatted.append(format_tweet_for_batch(tweet, i))

    return "\n\n".join(formatted)


def build_batch_prompt(base_prompt: str) -> str:
    """
    Wrap the base prompt with batch classification instructions.

    The base prompt contains the classification criteria. We add instructions
    for handling multiple tweets and the expected response format.
    """
    return f"""{base_prompt}

---

You will be given multiple tweets to classify. For each tweet, provide your classification.

IMPORTANT: Respond with ONLY a JSON array. Each element must have:
- "id": the tweet number (1, 2, 3, etc.)
- "approved": boolean (true to show, false to hide)
- "reason": brief explanation (1 sentence)

Example response format:
[
  {{"id": 1, "approved": true, "reason": "Informative tech discussion"}},
  {{"id": 2, "approved": false, "reason": "Engagement bait"}}
]"""


def classify_tweets_batch(
    tweets: list[dict],
    prompt_id: str,
    provider_name: str | None = None,
    model: str | None = None,
) -> dict[str, dict]:
    """
    Classify multiple tweets in a single LLM request.

    Args:
        tweets: List of tweet dicts with id, text, author_username, etc.
        prompt_id: ID of the prompt to use from the prompts table.
        provider_name: LLM provider ("anthropic", "gemini"). Defaults to "anthropic".
        model: Model to use (defaults to provider's default model).

    Returns:
        Dict mapping tweet_id -> classification result dict.
        Each result has either {approved, reason} or {_error, ...}.
    """
    if not tweets:
        return {}

    # Get the prompt
    prompt = get_prompt(prompt_id)
    if not prompt:
        return {t["id"]: {"_error": "prompt_not_found", "_prompt_id": prompt_id} for t in tweets}

    # Get provider
    if provider_name is None:
        provider_name = get_default_provider()

    try:
        provider = get_provider(provider_name)
    except ValueError as e:
        return {t["id"]: {"_error": "provider_error", "_message": str(e)[:200]} for t in tweets}

    # Check API key
    if not provider.get_api_key():
        return {
            t["id"]: {"_error": "no_api_key", "_env_var": provider.config.api_key_env_var}
            for t in tweets
        }

    # Build the batch prompt and format tweets
    system_prompt = build_batch_prompt(prompt["prompt_text"])
    tweets_text = format_tweets_batch(tweets)
    expected_ids = [t["id"] for t in tweets]

    # Use provider's batch classification
    return provider.classify_batch(tweets_text, expected_ids, system_prompt, model)


def classify_and_store_batch(
    tweets: list[dict],
    prompt_id: str,
    provider_name: str | None = None,
    model: str | None = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> dict[str, dict]:
    """
    Classify tweets and store the results in prompt_responses.

    This is a convenience wrapper around classify_tweets_batch that
    also persists the results to the database.

    Args:
        tweets: List of tweet dicts with id, text, author_username, etc.
        prompt_id: ID of the prompt to use from the prompts table.
        provider_name: LLM provider ("anthropic", "gemini"). Defaults to "anthropic".
        model: Model to use (defaults to provider's default model).
        db_path: Path to the database.

    Returns the classification results.
    """
    # Get actual model name for storage
    if provider_name is None:
        provider_name = get_default_provider()
    provider = get_provider(provider_name)
    actual_model = provider.get_model(model)

    results = classify_tweets_batch(tweets, prompt_id, provider_name, model)

    # Store each result
    for tweet_id, response in results.items():
        store_prompt_response(tweet_id, prompt_id, actual_model, response, db_path)

    return results
