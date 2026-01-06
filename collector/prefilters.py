"""
Prefilter functions for mode-based tweet filtering.

Prefilters run BEFORE the LLM is called and can short-circuit the decision:
- Return True: Show this tweet (skip LLM)
- Return False: Hide this tweet (skip LLM)
- Return None: Need LLM evaluation

Prefilters receive the raw tweet dict with all metadata.
"""

from typing import Any


# =============================================================================
# Author-based prefilters
# =============================================================================

# Customize these sets for your preferences
WHITELIST_AUTHORS: set[str] = set()  # Always show tweets from these users
BLACKLIST_AUTHORS: set[str] = set()  # Always hide tweets from these users


def author_whitelist(tweet: dict[str, Any]) -> bool | None:
    """
    Always show tweets from whitelisted authors.
    Always hide tweets from blacklisted authors.
    """
    username = tweet.get("author_username", "").lower()
    if username in {u.lower() for u in WHITELIST_AUTHORS}:
        return True
    if username in {u.lower() for u in BLACKLIST_AUTHORS}:
        return False
    return None


def make_author_filter(whitelist: set[str] = None, blacklist: set[str] = None):
    """
    Factory for custom author filters.

    Example:
        my_filter = make_author_filter(
            whitelist={"friend1", "friend2"},
            blacklist={"spammer"}
        )
    """
    whitelist = {u.lower() for u in (whitelist or set())}
    blacklist = {u.lower() for u in (blacklist or set())}

    def prefilter(tweet: dict[str, Any]) -> bool | None:
        username = tweet.get("author_username", "").lower()
        if username in whitelist:
            return True
        if username in blacklist:
            return False
        return None

    return prefilter


# =============================================================================
# Tweet type prefilters
# =============================================================================


def skip_retweets(tweet: dict[str, Any]) -> bool | None:
    """Always hide retweets."""
    if tweet.get("is_retweet"):
        return False
    return None


def skip_replies(tweet: dict[str, Any]) -> bool | None:
    """Always hide replies (show only original tweets)."""
    if tweet.get("reply_to_tweet_id"):
        return False
    return None


def only_original(tweet: dict[str, Any]) -> bool | None:
    """Only show original tweets (not retweets, quotes, or replies)."""
    if tweet.get("is_retweet") or tweet.get("is_quote") or tweet.get("reply_to_tweet_id"):
        return False
    return None


# =============================================================================
# Engagement-based prefilters
# =============================================================================


def high_engagement_only(min_likes: int = 100):
    """
    Factory for engagement threshold filters.
    Only passes tweets with minimum engagement to LLM.
    """
    def prefilter(tweet: dict[str, Any]) -> bool | None:
        likes = tweet.get("like_count", 0)
        if likes >= min_likes:
            return None  # High engagement, let LLM decide
        return False  # Low engagement, skip
    return prefilter


def viral_auto_show(min_likes: int = 10000):
    """
    Auto-show viral tweets, let LLM decide on others.
    Useful for "don't miss big discussions" mode.
    """
    def prefilter(tweet: dict[str, Any]) -> bool | None:
        likes = tweet.get("like_count", 0)
        if likes >= min_likes:
            return True  # Viral, auto-show
        return None  # Normal, let LLM decide
    return prefilter


# =============================================================================
# Content-based prefilters (keyword matching)
# =============================================================================


def keyword_filter(show_keywords: set[str] = None, hide_keywords: set[str] = None):
    """
    Factory for keyword-based prefilters.
    Checks tweet text for keywords (case-insensitive).
    """
    show_keywords = {k.lower() for k in (show_keywords or set())}
    hide_keywords = {k.lower() for k in (hide_keywords or set())}

    def prefilter(tweet: dict[str, Any]) -> bool | None:
        text_lower = tweet.get("text", "").lower()

        # Check hide keywords first (blacklist takes precedence)
        for kw in hide_keywords:
            if kw in text_lower:
                return False

        # Check show keywords
        for kw in show_keywords:
            if kw in text_lower:
                return True

        return None

    return prefilter


# =============================================================================
# Combining prefilters
# =============================================================================


def chain(*prefilters):
    """
    Chain multiple prefilters. First non-None result wins.

    Example:
        combined = chain(author_whitelist, skip_retweets, keyword_filter(...))
    """
    def combined_prefilter(tweet: dict[str, Any]) -> bool | None:
        for pf in prefilters:
            result = pf(tweet)
            if result is not None:
                return result
        return None

    return combined_prefilter


# =============================================================================
# Prefilter registry
# =============================================================================

PREFILTERS = {
    "author_whitelist": author_whitelist,
    "skip_retweets": skip_retweets,
    "skip_replies": skip_replies,
    "only_original": only_original,
    # Note: factory functions can't be registered directly
    # Use register_prefilter() for custom instances
}


def get_prefilter(name: str):
    """
    Get a prefilter function by name.
    Returns None if name is None or empty.
    Raises KeyError if name is provided but not found.
    """
    if not name:
        return None
    if name not in PREFILTERS:
        raise KeyError(f"Unknown prefilter: {name}. Available: {list(PREFILTERS.keys())}")
    return PREFILTERS[name]


def register_prefilter(name: str, func):
    """Register a custom prefilter function."""
    PREFILTERS[name] = func
