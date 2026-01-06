"""
Mode-based tweet decision logic.

This module provides the core decision function that determines whether
a tweet should be shown for a given mode, using prefilters and extractors.
"""

from pathlib import Path
from typing import Any

from database import (
    DEFAULT_DB_PATH,
    get_mode,
    get_prompt_response,
    get_prompt_responses_batch,
    get_tweet,
    get_tweets_batch,
    list_modes,
)
from extractors import get_extractor
from prefilters import get_prefilter


class NeedsClassification(Exception):
    """Raised when a tweet needs LLM classification before a decision can be made."""

    def __init__(self, tweet_id: str, prompt_id: str):
        self.tweet_id = tweet_id
        self.prompt_id = prompt_id
        super().__init__(f"Tweet {tweet_id} needs classification with prompt {prompt_id}")


def decide_tweet(
    tweet: dict,
    mode_id: str,
    model: str | None = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> bool:
    """
    Decide whether a tweet should be shown for a given mode.

    Args:
        tweet: Tweet dict with all metadata
        mode_id: Mode ID to use for decision
        model: Optional specific model to use for LLM responses
        db_path: Database path

    Returns:
        True if tweet should be shown, False otherwise

    Raises:
        NeedsClassification: If tweet needs LLM classification first
        KeyError: If mode not found
    """
    mode = get_mode(mode_id, db_path)
    if not mode:
        raise KeyError(f"Mode not found: {mode_id}")

    # 1. Try prefilter first (fast, no LLM needed)
    if mode["prefilter"]:
        prefilter_fn = get_prefilter(mode["prefilter"])
        if prefilter_fn:
            result = prefilter_fn(tweet)
            if result is not None:
                return result

    # 2. Need LLM response - check if we have one cached
    response_record = get_prompt_response(
        tweet["id"], mode["prompt_id"], model, db_path
    )

    if not response_record:
        raise NeedsClassification(tweet["id"], mode["prompt_id"])

    response = response_record["response"]

    # Check for error responses
    if isinstance(response, dict) and response.get("_error"):
        # Error response - treat as needs classification
        raise NeedsClassification(tweet["id"], mode["prompt_id"])

    # 3. Apply extractor to get decision
    extractor_fn = get_extractor(mode["extractor"])
    return extractor_fn(response)


def decide_tweets_batch(
    tweet_ids: list[str],
    mode_id: str,
    model: str | None = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> dict[str, str]:
    """
    Decide status for multiple tweets.

    Returns dict mapping tweet_id -> status:
    - 'approved': Show this tweet
    - 'filtered': Hide this tweet
    - 'pending': Needs LLM classification
    - 'unknown': Tweet not in database
    """
    if not tweet_ids:
        return {}

    mode = get_mode(mode_id, db_path)
    if not mode:
        raise KeyError(f"Mode not found: {mode_id}")

    # Get tweets
    tweets = get_tweets_batch(tweet_ids, db_path)

    # Get prefilter function
    prefilter_fn = None
    if mode["prefilter"]:
        prefilter_fn = get_prefilter(mode["prefilter"])

    # Get extractor function
    extractor_fn = get_extractor(mode["extractor"])

    # Get all cached responses
    responses = get_prompt_responses_batch(tweet_ids, mode["prompt_id"], model, db_path)

    results = {}

    for tweet_id in tweet_ids:
        # Check if tweet exists
        if tweet_id not in tweets:
            results[tweet_id] = "unknown"
            continue

        tweet = tweets[tweet_id]

        # Try prefilter
        if prefilter_fn:
            prefilter_result = prefilter_fn(tweet)
            if prefilter_result is not None:
                results[tweet_id] = "approved" if prefilter_result else "filtered"
                continue

        # Check for cached response
        if tweet_id not in responses:
            results[tweet_id] = "pending"
            continue

        response_record = responses[tweet_id]
        response = response_record["response"]

        # Check for error responses
        if isinstance(response, dict) and response.get("_error"):
            results[tweet_id] = "pending"
            continue

        # Apply extractor
        try:
            approved = extractor_fn(response)
            results[tweet_id] = "approved" if approved else "filtered"
        except Exception:
            # Extractor failed - treat as pending
            results[tweet_id] = "pending"

    return results


def get_mode_status_for_all_tweets(
    mode_id: str,
    model: str | None = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> dict[str, str]:
    """
    Get status for all classified tweets in a mode.
    Returns dict mapping tweet_id -> 'approved' | 'filtered'.
    Only includes tweets that have been classified (not pending).
    """
    from database import transaction

    mode = get_mode(mode_id, db_path)
    if not mode:
        raise KeyError(f"Mode not found: {mode_id}")

    extractor_fn = get_extractor(mode["extractor"])
    prefilter_fn = get_prefilter(mode["prefilter"]) if mode["prefilter"] else None

    # Get all tweets with responses for this prompt
    with transaction(db_path) as conn:
        if model:
            query = """
                SELECT t.*, pr.response_json
                FROM tweets t
                JOIN prompt_responses pr ON t.id = pr.tweet_id
                WHERE pr.prompt_id = ? AND pr.model = ?
            """
            rows = conn.execute(query, (mode["prompt_id"], model)).fetchall()
        else:
            query = """
                SELECT t.*, pr.response_json
                FROM tweets t
                JOIN prompt_responses pr ON t.id = pr.tweet_id
                WHERE pr.prompt_id = ?
            """
            rows = conn.execute(query, (mode["prompt_id"],)).fetchall()

    import json

    results = {}
    for row in rows:
        tweet = dict(row)
        tweet_id = tweet["id"]

        # Try prefilter
        if prefilter_fn:
            prefilter_result = prefilter_fn(tweet)
            if prefilter_result is not None:
                results[tweet_id] = "approved" if prefilter_result else "filtered"
                continue

        # Parse response
        try:
            response = json.loads(tweet["response_json"])
            if isinstance(response, dict) and response.get("_error"):
                continue  # Skip error responses
            approved = extractor_fn(response)
            results[tweet_id] = "approved" if approved else "filtered"
        except Exception:
            continue  # Skip on error

    return results


def get_available_modes(db_path: Path = DEFAULT_DB_PATH) -> list[dict]:
    """Get all available modes with their configurations."""
    return list_modes(db_path)
