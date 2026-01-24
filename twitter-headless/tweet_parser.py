"""
Tweet parsing logic for headless collector.

This module ports the tweet extraction logic from the browser extension
(extension/background.js) to Python. It extracts tweets from Twitter's
GraphQL API responses.
"""

from datetime import UTC, datetime
from typing import Any

# Twitter API endpoints that contain timeline/tweet data
TIMELINE_PATTERNS = [
    "HomeTimeline",
    "HomeLatestTimeline",
    "UserTweets",
    "TweetDetail",
]


def is_timeline_endpoint(url: str) -> bool:
    """Check if URL is a Twitter timeline GraphQL endpoint."""
    if "/graphql/" not in url:
        return False
    return any(pattern in url for pattern in TIMELINE_PATTERNS)


def extract_tweets_and_retweets(data: dict[str, Any]) -> tuple[list[dict], list[dict]]:
    """
    Extract tweet objects from Twitter's nested API response.

    Returns:
        Tuple of (tweets, retweets) where retweets link retweeters to original tweets.
    """
    tweets: list[dict] = []
    retweets: list[dict] = []
    seen_tweets: set[str] = set()
    seen_retweets: set[str] = set()  # "originalId:retweeterUsername"

    def process_tweet(tweet_obj: dict, context_info: dict) -> None:
        legacy = tweet_obj.get("legacy", tweet_obj)

        # Check if this is a retweet
        retweeted_result = legacy.get("retweeted_status_result", {}).get("result")
        if retweeted_result:
            # This is a retweet - extract the original tweet and create a retweet record

            # Get retweeter info from the current tweet
            retweeter_result = (
                tweet_obj.get("core", {}).get("user_results", {}).get("result")
                or tweet_obj.get("user_results", {}).get("result")
                or {}
            )
            retweeter_core = retweeter_result.get("core", {})
            retweeter_legacy = retweeter_result.get("legacy", {})

            retweeter_username = retweeter_core.get("screen_name") or retweeter_legacy.get(
                "screen_name"
            )
            retweeter_display_name = retweeter_core.get("name") or retweeter_legacy.get("name")
            retweeter_user_id = retweeter_result.get("rest_id") or retweeter_legacy.get("id_str")

            # Normalize and store the original tweet
            original_context = {**context_info, "is_promoted": False}
            original_tweet = normalize_tweet(retweeted_result, original_context)
            if original_tweet and original_tweet["id"] not in seen_tweets:
                seen_tweets.add(original_tweet["id"])
                tweets.append(original_tweet)

            # Create retweet record if we have the original and retweeter info
            if original_tweet and retweeter_username:
                rt_key = f"{original_tweet['id']}:{retweeter_username}"
                if rt_key not in seen_retweets:
                    seen_retweets.add(rt_key)
                    retweets.append({
                        "original_tweet_id": original_tweet["id"],
                        "retweeter_user_id": retweeter_user_id,
                        "retweeter_username": retweeter_username,
                        "retweeter_display_name": retweeter_display_name,
                        "retweeted_at": legacy.get("created_at"),
                        "captured_at": datetime.now(UTC).isoformat(),
                    })

            # Don't add the "RT @..." wrapper tweet - we only want the original
            return

        # Not a retweet - process normally
        tweet = normalize_tweet(tweet_obj, context_info)
        if tweet and tweet["id"] not in seen_tweets:
            seen_tweets.add(tweet["id"])
            tweets.append(tweet)

    def traverse(obj: Any, parent: Any = None, grandparent: Any = None) -> None:
        if not isinstance(obj, dict):
            if isinstance(obj, list):
                for item in obj:
                    traverse(item, obj, parent)
            return

        # Check for promoted content indicators at the entry/item level
        is_promoted = bool(
            obj.get("promotedMetadata")
            or obj.get("advertiser_results")
            or (isinstance(parent, dict) and parent.get("promotedMetadata"))
            or (isinstance(parent, dict) and parent.get("advertiser_results"))
            or (obj.get("entryId") and "promoted" in str(obj.get("entryId")))
            or (obj.get("entryId") and "cursor-ad" in str(obj.get("entryId")))
            or (isinstance(parent, dict) and parent.get("entryId") and "promoted" in str(parent.get("entryId")))
        )

        context_info = {
            "is_promoted": is_promoted,
            "entry_id": obj.get("entryId") or (parent.get("entryId") if isinstance(parent, dict) else None),
            "entry_type": obj.get("__typename") or obj.get("entryType"),
        }

        # Look for tweet_results.result pattern (most reliable)
        tweet_results = obj.get("tweet_results", {}).get("result")
        if tweet_results:
            if tweet_results.get("__typename") == "Tweet" or tweet_results.get("legacy", {}).get("full_text") is not None:
                process_tweet(tweet_results, context_info)

        # Also look for direct Tweet objects (fallback)
        if obj.get("__typename") == "Tweet" and obj.get("legacy", {}).get("full_text") is not None:
            if obj.get("core", {}).get("user_results") or obj.get("user_results"):
                process_tweet(obj, context_info)

        # Recurse into objects and arrays
        for value in obj.values():
            traverse(value, obj, parent)

    traverse(data)
    return tweets, retweets


def normalize_tweet(raw: dict, context_info: dict | None = None) -> dict | None:
    """
    Normalize a tweet object into our standard schema.

    This matches the format expected by the collector's /tweets endpoint.
    """
    if context_info is None:
        context_info = {}

    try:
        # Handle both direct tweet objects and wrapped ones
        legacy = raw.get("legacy", raw)

        # Extract full text - prefer note_tweet for long-form content
        note_tweet_text = (
            raw.get("note_tweet", {})
            .get("note_tweet_results", {})
            .get("result", {})
            .get("text")
        )
        full_text = note_tweet_text or legacy.get("full_text") or legacy.get("text") or ""

        # Check if this is a promoted/ad tweet
        is_promoted = context_info.get("is_promoted", False)

        # Twitter has multiple paths to user data - try them all
        user_result = (
            raw.get("core", {}).get("user_results", {}).get("result")
            or raw.get("user_results", {}).get("result")
            or raw.get("author", {}).get("result")
            or {}
        )

        # Twitter now nests screen_name/name in userResult.core (not userResult.legacy)
        user_core = user_result.get("core", {})
        user_legacy = user_result.get("legacy", {})

        # Sometimes user is directly on legacy
        legacy_user = legacy.get("user", {})

        # Extract tweet ID - could be in various places
        tweet_id = raw.get("rest_id") or legacy.get("id_str") or legacy.get("id")
        if not tweet_id:
            return None

        # Try multiple sources for author info - userCore is the new primary location
        author_username = (
            user_core.get("screen_name")
            or user_legacy.get("screen_name")
            or legacy_user.get("screen_name")
            or legacy.get("user_screen_name")
        )
        author_display_name = (
            user_core.get("name")
            or user_legacy.get("name")
            or legacy_user.get("name")
            or legacy.get("user_name")
        )
        author_id = (
            user_result.get("rest_id")
            or user_legacy.get("id_str")
            or legacy_user.get("id_str")
            or legacy.get("user_id_str")
        )
        author_verified = user_legacy.get("verified") or legacy_user.get("verified") or False

        # Additional author info useful for classification
        author_bio = (
            user_result.get("profile_bio", {}).get("description")
            or user_legacy.get("description")
        )
        author_following = user_result.get("relationship_perspectives", {}).get("following")
        author_blue_verified = user_result.get("is_blue_verified")
        author_followers_count = user_legacy.get("followers_count")

        return {
            "id": str(tweet_id),
            "text": full_text,
            "created_at": legacy.get("created_at"),
            "author": {
                "id": author_id,
                "username": author_username,
                "display_name": author_display_name,
                "verified": author_verified,
                "blue_verified": author_blue_verified,
                "bio": author_bio,
                "following": author_following,
                "followers_count": author_followers_count,
            },
            "metrics": {
                "retweet_count": legacy.get("retweet_count", 0),
                "reply_count": legacy.get("reply_count", 0),
                "like_count": legacy.get("favorite_count", 0),
                "quote_count": legacy.get("quote_count", 0),
            },
            "reply_to": {
                "tweet_id": legacy.get("in_reply_to_status_id_str"),
                "user_id": legacy.get("in_reply_to_user_id_str"),
                "username": legacy.get("in_reply_to_screen_name"),
            },
            "is_retweet": bool(legacy.get("retweeted_status_result")),
            "is_quote": bool(raw.get("quoted_status_result")),
            "is_promoted": is_promoted,
            "quoted_tweet_id": (
                raw.get("quoted_status_result", {}).get("result", {}).get("rest_id")
                or legacy.get("quoted_status_id_str")
            ),
            "media": _extract_media(legacy.get("extended_entities") or legacy.get("entities")),
            "urls": _extract_urls(legacy.get("entities")),
            "hashtags": [h.get("text") for h in legacy.get("entities", {}).get("hashtags", [])],
            "mentions": [
                {"id": m.get("id_str"), "username": m.get("screen_name")}
                for m in legacy.get("entities", {}).get("user_mentions", [])
            ],
            "captured_at": datetime.now(UTC).isoformat(),
        }

    except Exception as e:
        print(f"[Parser] Error normalizing tweet: {e}")
        return None


def _extract_media(entities: dict | None) -> list[dict]:
    """Extract media items from entities."""
    if not entities or not entities.get("media"):
        return []

    return [
        {
            "type": m.get("type"),
            "url": m.get("media_url_https") or m.get("media_url"),
            "expanded_url": m.get("expanded_url"),
            "alt": m.get("alt_text") or m.get("ext_alt_text") or "",
        }
        for m in entities["media"]
    ]


def _extract_urls(entities: dict | None) -> list[dict]:
    """Extract URLs from entities."""
    if not entities or not entities.get("urls"):
        return []

    return [
        {
            "url": u.get("url"),
            "expanded_url": u.get("expanded_url"),
            "display_url": u.get("display_url"),
        }
        for u in entities["urls"]
    ]
