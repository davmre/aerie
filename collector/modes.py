"""
Mode-based tweet decision logic.

This module provides the core decision function that determines whether
a tweet should be shown for a given mode, using prefilters and extractors.
"""

import json
from pathlib import Path
from typing import Any

from database import (
    DEFAULT_DB_PATH,
    get_all_tweet_ids,
    get_cached_decision_stats,
    get_mode,
    get_mode_decisions_batch,
    get_prompt_response,
    get_prompt_responses_batch,
    get_tweet,
    get_tweets_batch,
    get_tweets_without_decision,
    list_modes,
    store_mode_decisions_batch,
)
from extractors import get_extractor_with_config
from prefilters import get_prefilter_with_config


def _parse_config(config_json: str | None) -> dict | None:
    """Parse JSON config, returning None if empty or invalid."""
    if not config_json:
        return None
    try:
        return json.loads(config_json)
    except (json.JSONDecodeError, TypeError):
        return None


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

    # Parse configs
    prefilter_config = _parse_config(mode.get("prefilter_config"))
    extractor_config = _parse_config(mode.get("extractor_config"))

    # 1. Try prefilter first (fast, no LLM needed)
    if mode["prefilter"]:
        prefilter_fn = get_prefilter_with_config(mode["prefilter"], prefilter_config)
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
    extractor_fn = get_extractor_with_config(mode["extractor"], extractor_config)
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

    # Parse configs
    prefilter_config = _parse_config(mode.get("prefilter_config"))
    extractor_config = _parse_config(mode.get("extractor_config"))

    # Get tweets
    tweets = get_tweets_batch(tweet_ids, db_path)

    # Get prefilter function
    prefilter_fn = None
    if mode["prefilter"]:
        prefilter_fn = get_prefilter_with_config(mode["prefilter"], prefilter_config)

    # Get extractor function
    extractor_fn = get_extractor_with_config(mode["extractor"], extractor_config)

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
    Get status for all tweets with cached decisions in a mode.
    Returns dict mapping tweet_id -> 'approved' | 'filtered'.
    Only includes tweets that have cached decisions (not pending).
    """
    from database import transaction

    mode = get_mode(mode_id, db_path)
    if not mode:
        raise KeyError(f"Mode not found: {mode_id}")

    with transaction(db_path) as conn:
        rows = conn.execute(
            "SELECT tweet_id, decision FROM mode_decisions WHERE mode_id = ?",
            (mode_id,),
        ).fetchall()

    return {row["tweet_id"]: row["decision"] for row in rows}


def compute_mode_decisions(
    mode_id: str,
    model: str | None = None,
    batch_size: int = 1000,
    db_path: Path = DEFAULT_DB_PATH,
) -> dict:
    """
    Compute and cache decisions for tweets that CAN be decided right now.

    Strategy:
    1. For prefilter-only modes: iterate all tweets, apply prefilter
    2. For extractor modes: only process tweets that already have prompt_responses
       (don't iterate tweets without responses - they're implicitly pending)

    This avoids expensive iteration over tweets that can't be decided yet.

    Returns:
        {"computed": N, "pending": N} - computed is tweets we made decisions for,
        pending is an estimate of tweets still needing LLM classification.
    """
    from database import transaction

    mode = get_mode(mode_id, db_path)
    if not mode:
        raise KeyError(f"Mode not found: {mode_id}")

    # Parse configs
    prefilter_config = _parse_config(mode.get("prefilter_config"))
    extractor_config = _parse_config(mode.get("extractor_config"))

    prefilter_fn = get_prefilter_with_config(mode["prefilter"], prefilter_config) if mode["prefilter"] else None
    extractor_fn = get_extractor_with_config(mode["extractor"], extractor_config)

    computed = 0

    # Check if prefilter is "total" (always returns True or False, never None)
    # For such prefilters, we need to iterate all tweets
    # For partial prefilters or no prefilter, we only process tweets with responses
    prefilter_is_total = False
    if prefilter_fn:
        # Test with empty tweet to see if prefilter always decides
        test_result = prefilter_fn({})
        prefilter_is_total = test_result is not None

    if prefilter_is_total:
        # Prefilter decides everything - iterate all tweets without decisions
        while True:
            tweets = get_tweets_without_decision(mode_id, limit=batch_size, db_path=db_path)
            if not tweets:
                break

            decisions_to_store = []
            for tweet in tweets:
                result = prefilter_fn(tweet)
                decisions_to_store.append({
                    "tweet_id": tweet["id"],
                    "mode_id": mode_id,
                    "decision": "approved" if result else "filtered",
                    "source": "prefilter",
                })

            if decisions_to_store:
                store_mode_decisions_batch(decisions_to_store, db_path)
                computed += len(decisions_to_store)

            if len(tweets) < batch_size:
                break

        return {"computed": computed, "pending": 0}

    # For partial prefilters or extractor-only modes:
    # Only process tweets that have prompt_responses (efficient!)
    with transaction(db_path) as conn:
        # Get tweets with responses that don't have cached decisions yet
        query = """
            SELECT t.*, pr.response_json
            FROM tweets t
            JOIN prompt_responses pr ON t.id = pr.tweet_id
            LEFT JOIN mode_decisions md ON t.id = md.tweet_id AND md.mode_id = ?
            WHERE pr.prompt_id = ? AND md.tweet_id IS NULL
        """
        if model:
            query += " AND pr.model = ?"
            rows = conn.execute(query, (mode_id, mode["prompt_id"], model)).fetchall()
        else:
            rows = conn.execute(query, (mode_id, mode["prompt_id"])).fetchall()

    decisions_to_store = []
    for row in rows:
        tweet = dict(row)
        tweet_id = tweet["id"]

        # Try prefilter first (for partial prefilters like author whitelist)
        if prefilter_fn:
            prefilter_result = prefilter_fn(tweet)
            if prefilter_result is not None:
                decisions_to_store.append({
                    "tweet_id": tweet_id,
                    "mode_id": mode_id,
                    "decision": "approved" if prefilter_result else "filtered",
                    "source": "prefilter",
                })
                continue

        # Apply extractor to prompt response
        try:
            response = json.loads(tweet["response_json"])
            if isinstance(response, dict) and response.get("_error"):
                continue  # Skip error responses, they stay pending

            approved = extractor_fn(response)
            decisions_to_store.append({
                "tweet_id": tweet_id,
                "mode_id": mode_id,
                "decision": "approved" if approved else "filtered",
                "source": "extractor",
            })
        except Exception:
            continue  # Skip on error

    if decisions_to_store:
        store_mode_decisions_batch(decisions_to_store, db_path)
        computed = len(decisions_to_store)

    # Estimate pending: total tweets - tweets with decisions
    with transaction(db_path) as conn:
        total = conn.execute("SELECT COUNT(*) FROM tweets").fetchone()[0]
        decided = conn.execute(
            "SELECT COUNT(*) FROM mode_decisions WHERE mode_id = ?",
            (mode_id,)
        ).fetchone()[0]
        pending = total - decided

    return {"computed": computed, "pending": pending}


def compute_all_mode_decisions(
    model: str | None = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> dict[str, dict]:
    """
    Compute decisions for all modes.
    Returns dict mapping mode_id -> {"computed": N, "pending": N}
    """
    modes = list_modes(db_path)
    results = {}
    for mode in modes:
        results[mode["id"]] = compute_mode_decisions(mode["id"], model, db_path=db_path)
    return results


def get_available_modes(db_path: Path = DEFAULT_DB_PATH) -> list[dict]:
    """Get all available modes with their configurations."""
    return list_modes(db_path)


def get_mode_stats(
    mode_id: str,
    model: str | None = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> dict:
    """
    Get tweet statistics for a specific mode from cached decisions.

    Returns:
        {"total": N, "approved": N, "filtered": N, "pending": N}
    """
    mode = get_mode(mode_id, db_path)
    if not mode:
        raise KeyError(f"Mode not found: {mode_id}")

    return get_cached_decision_stats(mode_id, db_path)
