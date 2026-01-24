"""
Type definitions for the collector service.

This module provides TypedDict definitions for common data structures
to improve type safety and IDE support.
"""

from typing import Any, NotRequired, TypedDict

# =============================================================================
# Author Data
# =============================================================================


class AuthorData(TypedDict, total=False):
    """Author information as received from API."""

    id: str
    username: str
    display_name: str
    verified: bool
    blue_verified: bool
    bio: str
    following: bool
    followers_count: int


# =============================================================================
# Tweet Data
# =============================================================================


class TweetInput(TypedDict, total=False):
    """Tweet data as received from extension/poller (input format)."""

    id: str
    text: str
    created_at: str
    captured_at: str
    platform: str
    platform_metadata: dict[str, Any]
    author: AuthorData
    metrics: dict[str, int]
    reply_to_tweet_id: str
    reply_to_user_id: str
    reply_to_username: str
    is_retweet: bool
    is_quote: bool
    is_promoted: bool
    quoted_tweet_id: str
    media_json: str
    urls_json: str
    hashtags_json: str
    mentions_json: str


class TweetData(TypedDict, total=False):
    """Tweet data as stored in database (flattened format)."""

    id: str
    text: str
    created_at: str | None
    captured_at: str
    platform: str
    platform_metadata: str | None
    author_id: str | None
    author_username: str | None
    author_display_name: str | None
    author_verified: int
    author_blue_verified: int | None
    author_bio: str | None
    author_following: int | None
    author_followers_count: int | None
    retweet_count: int
    reply_count: int
    like_count: int
    quote_count: int
    reply_to_tweet_id: str | None
    reply_to_user_id: str | None
    reply_to_username: str | None
    is_retweet: int
    is_quote: int
    is_promoted: int
    quoted_tweet_id: str | None
    media_json: str | None
    urls_json: str | None
    hashtags_json: str | None
    mentions_json: str | None


class TweetWithContext(TweetData):
    """Tweet with additional context for classification/display."""

    quoted_tweet: NotRequired[TweetData | None]
    thread_ancestors: NotRequired[list[TweetData]]
    retweeted_by: NotRequired[list[dict[str, Any]]]


# =============================================================================
# Store Result
# =============================================================================


class StoreResult(TypedDict):
    """Result from store_tweets() function."""

    inserted: int
    duplicates: int
    inserted_tweets: list[dict[str, Any]]


# =============================================================================
# Mode and Prompt
# =============================================================================


class ModeConfig(TypedDict, total=False):
    """Mode configuration from database."""

    id: str
    name: str
    prompt_id: str
    extractor: str
    prefilter: str | None
    extractor_config: str | None
    prefilter_config: str | None
    description: str | None
    provider: str
    model_name: str | None


class PromptConfig(TypedDict):
    """Prompt configuration from database."""

    id: str
    prompt_text: str
    response_schema: str | None
    created_at: str


# =============================================================================
# Classification Response
# =============================================================================


class PromptResponse(TypedDict, total=False):
    """LLM prompt response record."""

    tweet_id: str
    prompt_id: str
    model: str
    response_json: str
    response: dict[str, Any]
    classified_at: str
    classification_batch_id: str | None
    context_id: str | None


class ClassificationResult(TypedDict, total=False):
    """Result from LLM classification."""

    approved: bool
    reason: str
    _error: str
    _message: str
    _raw: str


# =============================================================================
# Conversation Chains
# =============================================================================


class ChainData(TypedDict):
    """Conversation chain for read view."""

    chain: list[TweetData]
    anchor_id: str
    hidden_replies: int


# =============================================================================
# Decision
# =============================================================================


class ModeDecision(TypedDict):
    """Cached mode decision for a tweet."""

    tweet_id: str
    mode_id: str
    decision: str  # 'approved' | 'filtered'
    source: str  # 'prefilter' | 'extractor'
    computed_at: str


# =============================================================================
# API Responses
# =============================================================================


class ApiError(TypedDict):
    """Standard API error response."""

    error: str


class ApiSuccess(TypedDict):
    """Standard API success response."""

    status: str


class StatsResponse(TypedDict, total=False):
    """Response from /stats endpoint."""

    total: int
    approved: int
    filtered: int
    pending: int
    platforms: dict[str, int]
