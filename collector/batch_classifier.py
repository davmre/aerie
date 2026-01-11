"""
Batch tweet classification using multi-tweet LLM requests.

This module provides efficient classification by sending multiple tweets
in a single LLM request, reducing costs by ~3.5x compared to single-tweet
requests.
"""

import json
import os
import re
from pathlib import Path

import anthropic
from anthropic.types import TextBlock

from database import DEFAULT_DB_PATH, get_prompt, store_prompt_response

# Default model for classification
DEFAULT_MODEL = "claude-sonnet-4-20250514"


def format_tweet_for_batch(tweet: dict, index: int) -> str:
    """Format a single tweet for inclusion in a batch request."""
    parts = []

    # Header with index and ID
    tweet_id = tweet.get("id", "unknown")
    parts.append(f"[{index}] ID: {tweet_id}")

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
- "id": the tweet ID (from the ID: line)
- "approved": boolean (true to show, false to hide)
- "reason": brief explanation (1 sentence)

Example response format:
[
  {{"id": "123456", "approved": true, "reason": "Informative tech discussion"}},
  {{"id": "789012", "approved": false, "reason": "Engagement bait"}}
]"""


def parse_batch_response(
    response_text: str,
    expected_ids: list[str],
) -> dict[str, dict]:
    """
    Parse the LLM's batch response into per-tweet results.

    Returns a dict mapping tweet_id -> response dict.
    Handles partial failures gracefully.
    """
    results = {}

    def extract_from_list(parsed: list) -> None:
        """Extract results from a parsed JSON list."""
        for item in parsed:
            if isinstance(item, dict) and "id" in item:
                tweet_id = str(item["id"])
                results[tweet_id] = {
                    "approved": bool(item.get("approved", False)),
                    "reason": str(item.get("reason", "")),
                }

    # Try to parse as JSON array
    try:
        parsed = json.loads(response_text.strip())
        if isinstance(parsed, list):
            extract_from_list(parsed)
    except json.JSONDecodeError:
        pass

    # If direct parse didn't work, try to find JSON array in the response
    if not results:
        array_match = re.search(r"\[[\s\S]*\]", response_text)
        if array_match:
            try:
                parsed = json.loads(array_match.group())
                if isinstance(parsed, list):
                    extract_from_list(parsed)
            except json.JSONDecodeError:
                pass

    # If still no results, try to extract individual JSON objects
    if not results:
        for obj_match in re.finditer(r'\{[^{}]*"id"\s*:\s*"?(\d+)"?[^{}]*\}', response_text):
            try:
                obj = json.loads(obj_match.group())
                if "id" in obj:
                    tweet_id = str(obj["id"])
                    results[tweet_id] = {
                        "approved": bool(obj.get("approved", False)),
                        "reason": str(obj.get("reason", "")),
                    }
            except json.JSONDecodeError:
                continue

    # Mark any missing tweets as errors
    for tweet_id in expected_ids:
        if tweet_id not in results:
            results[tweet_id] = {
                "_error": "parse_failed",
                "_raw": response_text[:200] if not results else "missing from response",
            }

    return results


def classify_tweets_batch(
    tweets: list[dict],
    prompt_id: str,
    model: str = DEFAULT_MODEL,
    client: anthropic.Anthropic | None = None,
) -> dict[str, dict]:
    """
    Classify multiple tweets in a single LLM request.

    Args:
        tweets: List of tweet dicts with id, text, author_username, etc.
        prompt_id: ID of the prompt to use from the prompts table.
        model: Model to use for classification.
        client: Optional Anthropic client (creates one if not provided).

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

    # Create client if needed
    if client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            return {t["id"]: {"_error": "no_api_key"} for t in tweets}
        client = anthropic.Anthropic(api_key=api_key)

    # Build the batch prompt and format tweets
    system_prompt = build_batch_prompt(prompt["prompt_text"])
    tweets_text = format_tweets_batch(tweets)
    expected_ids = [t["id"] for t in tweets]

    try:
        response = client.messages.create(
            model=model,
            max_tokens=100 * len(tweets),  # ~100 tokens per tweet response
            system=system_prompt,
            messages=[{"role": "user", "content": f"Classify these tweets:\n\n{tweets_text}"}],
        )

        first_block = response.content[0]
        if not isinstance(first_block, TextBlock):
            return {
                t["id"]: {"_error": "unexpected_response", "_message": "No text content"}
                for t in tweets
            }
        content = first_block.text.strip()
        return parse_batch_response(content, expected_ids)

    except anthropic.RateLimitError as e:
        return {t["id"]: {"_error": "rate_limit", "_message": str(e)[:100]} for t in tweets}
    except anthropic.APIError as e:
        return {t["id"]: {"_error": "api_error", "_message": str(e)[:200]} for t in tweets}
    except Exception as e:
        return {t["id"]: {"_error": "unexpected_error", "_message": str(e)[:200]} for t in tweets}


def classify_and_store_batch(
    tweets: list[dict],
    prompt_id: str,
    model: str = DEFAULT_MODEL,
    client: anthropic.Anthropic | None = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> dict[str, dict]:
    """
    Classify tweets and store the results in prompt_responses.

    This is a convenience wrapper around classify_tweets_batch that
    also persists the results to the database.

    Returns the classification results.
    """
    results = classify_tweets_batch(tweets, prompt_id, model, client)

    # Store each result
    for tweet_id, response in results.items():
        store_prompt_response(tweet_id, prompt_id, model, response, db_path)

    return results
