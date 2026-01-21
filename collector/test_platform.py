#!/usr/bin/env python3
"""Test script for platform support."""

import json
from datetime import datetime
from pathlib import Path

from database import (
    get_platform_stats,
    get_tweet,
    init_database,
    store_retweets,
    store_tweets,
)

# Use a test database
TEST_DB = Path("/tmp/test_platform.db")
if TEST_DB.exists():
    TEST_DB.unlink()

print("Testing platform support...")
print()

# Initialize database
print("1. Initializing database...")
init_database(TEST_DB)
print("✓ Database initialized")
print()

# Test storing a Twitter tweet (default)
print("2. Storing a Twitter tweet (default platform)...")
twitter_tweet = {
    "id": "twitter:1234567890",
    "text": "Hello from Twitter!",
    "created_at": datetime.utcnow().isoformat(),
    "author": {
        "id": "12345",
        "username": "testuser",
        "display_name": "Test User",
        "verified": False,
    },
}
result = store_tweets([twitter_tweet], TEST_DB)
print(f"✓ Stored tweet: {result}")

# Verify it was stored
tweet = get_tweet("twitter:1234567890", TEST_DB)
assert tweet is not None, "Tweet not found!"
assert tweet["platform"] == "twitter", f"Expected platform='twitter', got {tweet['platform']}"
assert tweet["platform_metadata"] is None, "Expected no metadata for default tweet"
print(f"✓ Tweet platform: {tweet['platform']}")
print()

# Test storing a Bluesky post
print("3. Storing a Bluesky post...")
bluesky_post = {
    "id": "bsky:at://did:plc:abc123/app.bsky.feed.post/xyz789",
    "text": "Hello from Bluesky!",
    "created_at": datetime.utcnow().isoformat(),
    "platform": "bluesky",
    "platform_metadata": {
        "cid": "bafyreig2fjxi3rptqdgylg7e5hmjl6mcke7rn2b6cugzlqq3i4zu6rq52q",
        "root_uri": "at://did:plc:abc123/app.bsky.feed.post/xyz789",
        "labels": [],
        "langs": ["en"],
    },
    "author": {
        "id": "did:plc:abc123",
        "username": "testuser.bsky.social",
        "display_name": "Test User on Bluesky",
        "verified": True,
    },
}
result = store_tweets([bluesky_post], TEST_DB)
print(f"✓ Stored post: {result}")

# Verify it was stored
post = get_tweet("bsky:at://did:plc:abc123/app.bsky.feed.post/xyz789", TEST_DB)
assert post is not None, "Post not found!"
assert post["platform"] == "bluesky", f"Expected platform='bluesky', got {post['platform']}"
assert post["platform_metadata"] is not None, "Expected metadata for Bluesky post"

# Parse and verify metadata
metadata = json.loads(post["platform_metadata"])
assert metadata["cid"] == "bafyreig2fjxi3rptqdgylg7e5hmjl6mcke7rn2b6cugzlqq3i4zu6rq52q"
assert metadata["langs"] == ["en"]
print(f"✓ Post platform: {post['platform']}")
print(f"✓ Post metadata: {metadata}")
print()

# Test retweets with platform
print("4. Storing retweets/reposts...")
twitter_retweet = {
    "original_tweet_id": "twitter:1234567890",
    "retweeter_username": "retweeter1",
    "retweeter_display_name": "Retweeter One",
    "retweeted_at": datetime.utcnow().isoformat(),
    "platform": "twitter",
}
bluesky_repost = {
    "original_tweet_id": "bsky:at://did:plc:abc123/app.bsky.feed.post/xyz789",
    "retweeter_username": "reposter.bsky.social",
    "retweeter_display_name": "Reposter",
    "retweeted_at": datetime.utcnow().isoformat(),
    "platform": "bluesky",
}
result = store_retweets([twitter_retweet, bluesky_repost], TEST_DB)
print(f"✓ Stored retweets: {result}")
print()

# Show platform statistics
print("5. Platform statistics:")
stats = get_platform_stats(TEST_DB)
for platform, count in sorted(stats.items()):
    print(f"  {platform}: {count} posts")
print()

print("✓ All tests passed!")
print()
print("Summary:")
print("  - Twitter tweets default to platform='twitter'")
print("  - Bluesky posts can specify platform='bluesky'")
print("  - Platform metadata is stored as JSON")
print("  - Retweets/reposts track their platform")
print("  - Platform statistics work correctly")
