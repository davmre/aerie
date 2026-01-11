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
# Universal prefilters
# =============================================================================


def approve_all(tweet: dict[str, Any]) -> bool | None:
    """Approve all tweets without review."""
    return True


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
    username = (tweet.get("author_username") or "").lower()
    if username in {u.lower() for u in WHITELIST_AUTHORS}:
        return True
    if username in {u.lower() for u in BLACKLIST_AUTHORS}:
        return False
    return None


def make_author_filter(whitelist: set[str] | None = None, blacklist: set[str] | None = None):
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
        username = (tweet.get("author_username") or "").lower()
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


def keyword_filter(show_keywords: set[str] | None = None, hide_keywords: set[str] | None = None):
    """
    Factory for keyword-based prefilters.
    Checks tweet text for keywords (case-insensitive).
    """
    show_keywords = {k.lower() for k in (show_keywords or set())}
    hide_keywords = {k.lower() for k in (hide_keywords or set())}

    def prefilter(tweet: dict[str, Any]) -> bool | None:
        text_lower = (tweet.get("text") or "").lower()

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
    "approve_all": approve_all,
    "author_whitelist": author_whitelist,
    "skip_retweets": skip_retweets,
    "skip_replies": skip_replies,
    "only_original": only_original,
}

# Schema definitions for each prefilter
# "simple" prefilters have no config, "factory" prefilters accept parameters
PREFILTER_SCHEMAS = {
    "approve_all": {
        "type": "simple",
        "description": "Approve all tweets without further review.",
    },
    "author_whitelist": {
        "type": "simple",
        "description": "Show/hide based on global WHITELIST_AUTHORS and BLACKLIST_AUTHORS sets",
    },
    "skip_retweets": {
        "type": "simple",
        "description": "Always hide retweets, let LLM decide on original tweets",
    },
    "skip_replies": {
        "type": "simple",
        "description": "Always hide replies, let LLM decide on original tweets",
    },
    "only_original": {
        "type": "simple",
        "description": "Only show original tweets (hide retweets, quotes, and replies)",
    },
    "make_author_filter": {
        "type": "factory",
        "description": "Custom author whitelist/blacklist filter",
        "params": {
            "whitelist": {
                "type": "array",
                "items": "string",
                "required": False,
                "default": [],
                "description": "Usernames to always show (case-insensitive)",
            },
            "blacklist": {
                "type": "array",
                "items": "string",
                "required": False,
                "default": [],
                "description": "Usernames to always hide (case-insensitive)",
            },
        },
    },
    "high_engagement_only": {
        "type": "factory",
        "description": "Only process tweets above a minimum like count (hide low-engagement tweets)",
        "params": {
            "min_likes": {
                "type": "number",
                "required": False,
                "default": 100,
                "min": 0,
                "description": "Minimum likes required to consider a tweet",
            },
        },
    },
    "viral_auto_show": {
        "type": "factory",
        "description": "Auto-show viral tweets, let LLM decide on others",
        "params": {
            "min_likes": {
                "type": "number",
                "required": False,
                "default": 10000,
                "min": 0,
                "description": "Like threshold for auto-showing",
            },
        },
    },
    "keyword_filter": {
        "type": "factory",
        "description": "Filter tweets by keywords in text (case-insensitive)",
        "params": {
            "show_keywords": {
                "type": "array",
                "items": "string",
                "required": False,
                "default": [],
                "description": "Keywords that auto-show a tweet",
            },
            "hide_keywords": {
                "type": "array",
                "items": "string",
                "required": False,
                "default": [],
                "description": "Keywords that auto-hide a tweet (takes precedence)",
            },
        },
    },
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


def get_prefilter_with_config(name: str, config: dict | None = None):
    """
    Get a prefilter function, applying config for factory-type prefilters.
    For simple prefilters, config is ignored.
    For factory prefilters, config params are passed to the factory function.
    Returns None if name is None or empty.
    """
    if not name:
        return None

    schema = PREFILTER_SCHEMAS.get(name, {})

    if schema.get("type") == "factory":
        config = config or {}
        if name == "make_author_filter":
            whitelist = set(config.get("whitelist", []))
            blacklist = set(config.get("blacklist", []))
            return make_author_filter(whitelist, blacklist)
        elif name == "high_engagement_only":
            min_likes = config.get("min_likes", 100)
            return high_engagement_only(min_likes)
        elif name == "viral_auto_show":
            min_likes = config.get("min_likes", 10000)
            return viral_auto_show(min_likes)
        elif name == "keyword_filter":
            show_keywords = set(config.get("show_keywords", []))
            hide_keywords = set(config.get("hide_keywords", []))
            return keyword_filter(show_keywords, hide_keywords)
        else:
            raise KeyError(
                f"Factory prefilter '{name}' not implemented in get_prefilter_with_config"
            )

    # Simple prefilter - just look it up
    return get_prefilter(name)


def list_prefilter_schemas() -> list[dict]:
    """Return all prefilter schemas for the API."""
    return [{"name": name, **schema} for name, schema in PREFILTER_SCHEMAS.items()]


def register_prefilter(name: str, func, schema: dict | None = None):
    """Register a custom prefilter function with optional schema."""
    PREFILTERS[name] = func
    if schema:
        PREFILTER_SCHEMAS[name] = schema
