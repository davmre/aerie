"""
Mode-based tweet decision logic.

This module provides the core decision function that determines whether
a tweet should be shown for a given mode, using prefilters and extractors.
"""

import json
from pathlib import Path

from database import (
    DEFAULT_DB_PATH,
    get_cached_decision_stats,
    get_mode,
    get_prompt_response,
    get_prompt_responses_batch,
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
    # Use mode's configured model, falling back to passed model parameter
    effective_model = mode.get("model_name") or model
    response_record = get_prompt_response(tweet["id"], mode["prompt_id"], effective_model, db_path)

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

    # Get all cached responses - use mode's configured model
    effective_model = mode.get("model_name") or model
    responses = get_prompt_responses_batch(tweet_ids, mode["prompt_id"], effective_model, db_path)

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

    prefilter_fn = (
        get_prefilter_with_config(mode["prefilter"], prefilter_config)
        if mode["prefilter"]
        else None
    )
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
        assert prefilter_fn is not None  # prefilter_is_total implies prefilter_fn exists
        while True:
            tweets = get_tweets_without_decision(mode_id, limit=batch_size, db_path=db_path)
            if not tweets:
                break

            decisions_to_store = []
            for tweet in tweets:
                result = prefilter_fn(tweet)
                decisions_to_store.append(
                    {
                        "tweet_id": tweet["id"],
                        "mode_id": mode_id,
                        "decision": "approved" if result else "filtered",
                        "source": "prefilter",
                    }
                )

            if decisions_to_store:
                store_mode_decisions_batch(decisions_to_store, db_path)
                computed += len(decisions_to_store)

            if len(tweets) < batch_size:
                break

        return {"computed": computed, "pending": 0}

    # For partial prefilters or extractor-only modes:
    # Only process tweets that have prompt_responses (efficient!)
    # Use mode's configured model, falling back to passed model parameter
    effective_model = mode.get("model_name") or model
    with transaction(db_path) as conn:
        # Get tweets with responses that don't have cached decisions yet
        query = """
            SELECT t.*, pr.response_json
            FROM tweets t
            JOIN prompt_responses pr ON t.id = pr.tweet_id
            LEFT JOIN mode_decisions md ON t.id = md.tweet_id AND md.mode_id = ?
            WHERE pr.prompt_id = ? AND md.tweet_id IS NULL
        """
        if effective_model:
            query += " AND pr.model = ?"
            rows = conn.execute(query, (mode_id, mode["prompt_id"], effective_model)).fetchall()
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
                decisions_to_store.append(
                    {
                        "tweet_id": tweet_id,
                        "mode_id": mode_id,
                        "decision": "approved" if prefilter_result else "filtered",
                        "source": "prefilter",
                    }
                )
                continue

        # Apply extractor to prompt response
        try:
            response = json.loads(tweet["response_json"])
            if isinstance(response, dict) and response.get("_error"):
                continue  # Skip error responses, they stay pending

            approved = extractor_fn(response)
            decisions_to_store.append(
                {
                    "tweet_id": tweet_id,
                    "mode_id": mode_id,
                    "decision": "approved" if approved else "filtered",
                    "source": "extractor",
                }
            )
        except Exception:
            continue  # Skip on error

    if decisions_to_store:
        store_mode_decisions_batch(decisions_to_store, db_path)
        computed = len(decisions_to_store)

    # Estimate pending: total tweets - tweets with decisions
    with transaction(db_path) as conn:
        total = conn.execute("SELECT COUNT(*) FROM tweets").fetchone()[0]
        decided = conn.execute(
            "SELECT COUNT(*) FROM mode_decisions WHERE mode_id = ?", (mode_id,)
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

    return get_cached_decision_stats(mode_id, db_path=db_path)


def compute_mode_decisions_for_tweets(
    tweet_ids: list[str],
    model: str | None = None,
    mode_ids: list[str] | None = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> dict[str, dict[str, str]]:
    """
    Compute and cache decisions for specific tweets across modes.

    This is more efficient than compute_all_mode_decisions() when you only
    need to update decisions for a small set of tweets (e.g., after
    classifying a batch).

    Args:
        tweet_ids: List of tweet IDs to compute decisions for.
        model: Optional model filter for prompt responses (deprecated, each mode
            now uses its own model_name setting).
        mode_ids: Optional list of mode IDs to compute for. If None, computes
            for all modes.
        db_path: Database path.

    Returns:
        Dict mapping tweet_id -> {mode_id: decision} for computed modes.
    """
    if not tweet_ids:
        return {}

    all_modes = list_modes(db_path)
    if not all_modes:
        return {}

    # Filter to requested modes if specified
    if mode_ids is not None:
        mode_id_set = set(mode_ids)
        modes = [m for m in all_modes if m["id"] in mode_id_set]
    else:
        modes = all_modes

    if not modes:
        return {}

    # Get all tweets
    tweets = get_tweets_batch(tweet_ids, db_path)
    if not tweets:
        return {}

    # Collect all unique (prompt_id, model_name) pairs we need responses for
    # Each mode may have a different model_name
    prompt_model_pairs = set((m["prompt_id"], m.get("model_name") or model) for m in modes)

    # Get prompt responses for all tweets and (prompt, model) combinations
    # Cache keyed by (prompt_id, model_name)
    responses_cache: dict[tuple[str, str | None], dict] = {}
    for prompt_id, mode_model in prompt_model_pairs:
        responses_cache[(prompt_id, mode_model)] = get_prompt_responses_batch(
            tweet_ids, prompt_id, mode_model, db_path
        )

    # Track results and decisions to store
    results: dict[str, dict[str, str]] = {tid: {} for tid in tweet_ids}
    decisions_to_store = []

    for mode in modes:
        mode_id = mode["id"]
        prompt_id = mode["prompt_id"]
        mode_model = mode.get("model_name") or model

        # Parse configs
        prefilter_config = _parse_config(mode.get("prefilter_config"))
        extractor_config = _parse_config(mode.get("extractor_config"))

        prefilter_fn = (
            get_prefilter_with_config(mode["prefilter"], prefilter_config)
            if mode["prefilter"]
            else None
        )
        extractor_fn = get_extractor_with_config(mode["extractor"], extractor_config)

        responses = responses_cache.get((prompt_id, mode_model), {})

        for tweet_id in tweet_ids:
            if tweet_id not in tweets:
                continue

            tweet = tweets[tweet_id]

            # Try prefilter first
            if prefilter_fn:
                prefilter_result = prefilter_fn(tweet)
                if prefilter_result is not None:
                    decision = "approved" if prefilter_result else "filtered"
                    results[tweet_id][mode_id] = decision
                    decisions_to_store.append(
                        {
                            "tweet_id": tweet_id,
                            "mode_id": mode_id,
                            "decision": decision,
                            "source": "prefilter",
                        }
                    )
                    continue

            # Need prompt response for extractor
            if tweet_id not in responses:
                # No response yet, leave as pending (don't store anything)
                continue

            response_record = responses[tweet_id]
            response = response_record["response"]

            # Check for error responses
            if isinstance(response, dict) and response.get("_error"):
                continue  # Leave as pending

            # Apply extractor
            try:
                approved = extractor_fn(response)
                decision = "approved" if approved else "filtered"
                results[tweet_id][mode_id] = decision
                decisions_to_store.append(
                    {
                        "tweet_id": tweet_id,
                        "mode_id": mode_id,
                        "decision": decision,
                        "source": "extractor",
                    }
                )
            except Exception:
                continue  # Leave as pending on error

    # Store all decisions
    if decisions_to_store:
        store_mode_decisions_batch(decisions_to_store, db_path)

    return results
