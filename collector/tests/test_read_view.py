"""Integration tests for the Read view API."""

from datetime import datetime, timedelta

from database import decode_cursor, encode_cursor
from tests.fixtures import (
    approve_tweets,
    create_and_store_tweets,
    filter_tweets,
    make_thread,
    make_tweet,
)


class TestLeafOnlyFiltering:
    """Tests for the leaf_only parameter that collapses threads."""

    def test_leaf_only_excludes_thread_ancestors(self, client, test_db):
        """
        In a thread C -> B -> A, only C (the leaf) should be returned.
        A and B should appear in C's thread_ancestors.
        """
        # Create thread: A <- B <- C (C replies to B, B replies to A)
        tweets = [
            make_tweet(id="1", text="Tweet A"),
            make_tweet(id="2", text="Tweet B", reply_to_tweet_id="1"),
            make_tweet(id="3", text="Tweet C", reply_to_tweet_id="2"),
        ]
        create_and_store_tweets(tweets, test_db)
        approve_tweets(["1", "2", "3"], test_db)

        # Query with leaf_only=true
        resp = client.get("/api/ui/tweets?status=approved&leaf_only=true")
        assert resp.status_code == 200
        data = resp.get_json()

        # Only C should appear as main tweet
        assert len(data["tweets"]) == 1
        assert data["tweets"][0]["id"] == "3"

        # C should have A and B as thread ancestors (oldest first)
        ancestors = data["tweets"][0].get("thread_ancestors", [])
        assert len(ancestors) == 2
        assert ancestors[0]["id"] == "1"  # A (oldest)
        assert ancestors[1]["id"] == "2"  # B

    def test_leaf_only_returns_all_leaf_tweets(self, client, test_db):
        """Multiple independent tweets should all be returned."""
        tweets = [
            make_tweet(id="1", text="Independent A", author_username="user1"),
            make_tweet(id="2", text="Independent B", author_username="user2"),
            make_tweet(id="3", text="Independent C", author_username="user3"),
        ]
        create_and_store_tweets(tweets, test_db)
        approve_tweets(["1", "2", "3"], test_db)

        resp = client.get("/api/ui/tweets?status=approved&leaf_only=true")
        assert resp.status_code == 200
        data = resp.get_json()

        # All three should be returned (no replies among them)
        assert len(data["tweets"]) == 3

    def test_leaf_only_false_returns_all_tweets(self, client, test_db):
        """When leaf_only=false, all tweets in thread are returned."""
        tweets = make_thread(base_id=100, length=3)
        create_and_store_tweets(tweets, test_db)
        approve_tweets(["100", "101", "102"], test_db)

        resp = client.get("/api/ui/tweets?status=approved&leaf_only=false")
        assert resp.status_code == 200
        data = resp.get_json()

        # All three tweets in the thread should be returned
        assert len(data["tweets"]) == 3

    def test_leaf_only_excludes_quoted_tweets_without_own_thread(self, client, test_db):
        """
        A quoted tweet should be hidden if:
        - It has no replies of its own (not a thread starter)
        - It's only shown as context within the quoter

        Quote: A quotes Z, where Z is standalone -> only A is shown
        """
        tweets = [
            make_tweet(id="Z", text="Original standalone tweet"),
            make_tweet(id="A", text="Quote with commentary", quoted_tweet_id="Z"),
        ]
        create_and_store_tweets(tweets, test_db)
        approve_tweets(["Z", "A"], test_db)

        resp = client.get("/api/ui/tweets?status=approved&leaf_only=true")
        assert resp.status_code == 200
        data = resp.get_json()

        # Only A should appear (Z is just context)
        assert len(data["tweets"]) == 1
        assert data["tweets"][0]["id"] == "A"

        # A should have Z as quoted_tweet
        assert data["tweets"][0].get("quoted_tweet") is not None
        assert data["tweets"][0]["quoted_tweet"]["id"] == "Z"

    def test_leaf_only_shows_quoted_tweet_with_own_thread(self, client, test_db):
        """
        A quoted tweet should be shown if it has its own thread/replies.

        If Z has replies (Z <- Y), Z is a conversation starter and should
        be shown separately, not just as context.
        """
        tweets = [
            make_tweet(id="Z", text="Original tweet with discussion"),
            make_tweet(id="Y", text="Reply to Z", reply_to_tweet_id="Z"),
            make_tweet(id="A", text="Quote of Z", quoted_tweet_id="Z"),
        ]
        create_and_store_tweets(tweets, test_db)
        approve_tweets(["Z", "Y", "A"], test_db)

        resp = client.get("/api/ui/tweets?status=approved&leaf_only=true")
        assert resp.status_code == 200
        data = resp.get_json()

        # Both Y (leaf of Z's thread) and A (quoter) should appear
        ids = {t["id"] for t in data["tweets"]}
        assert ids == {"Y", "A"}


class TestThreadContext:
    """Tests for thread ancestor context in API responses."""

    def test_reply_includes_thread_ancestors(self, client, test_db):
        """A reply tweet should include its ancestors in thread_ancestors."""
        tweets = [
            make_tweet(id="1", text="Root"),
            make_tweet(id="2", text="Reply", reply_to_tweet_id="1"),
        ]
        create_and_store_tweets(tweets, test_db)
        approve_tweets(["1", "2"], test_db)

        resp = client.get("/api/ui/tweets?status=approved&leaf_only=true")
        data = resp.get_json()

        # Tweet 2 should have tweet 1 as ancestor
        assert len(data["tweets"]) == 1
        reply = data["tweets"][0]
        assert reply["id"] == "2"
        assert len(reply["thread_ancestors"]) == 1
        assert reply["thread_ancestors"][0]["id"] == "1"

    def test_thread_ancestor_includes_quoted_tweet(self, client, test_db):
        """
        If a thread ancestor is a quote tweet, its quoted_tweet should be included.

        Thread: B -> A, where A quotes Z
        When viewing B, A should include Z as its quoted_tweet.
        """
        tweets = [
            make_tweet(id="Z", text="Quoted original"),
            make_tweet(id="A", text="Quote tweet", quoted_tweet_id="Z"),
            make_tweet(id="B", text="Reply to quote", reply_to_tweet_id="A"),
        ]
        create_and_store_tweets(tweets, test_db)
        approve_tweets(["Z", "A", "B"], test_db)

        resp = client.get("/api/ui/tweets?status=approved&leaf_only=true")
        data = resp.get_json()

        # B should be the main tweet
        ids = {t["id"] for t in data["tweets"]}
        assert "B" in ids

        # Find B and check its ancestors
        tweet_b = next(t for t in data["tweets"] if t["id"] == "B")
        ancestors = tweet_b.get("thread_ancestors", [])
        assert len(ancestors) == 1
        ancestor_a = ancestors[0]
        assert ancestor_a["id"] == "A"

        # A should have Z as its quoted_tweet
        assert ancestor_a.get("quoted_tweet") is not None
        assert ancestor_a["quoted_tweet"]["id"] == "Z"


class TestStatusFiltering:
    """Tests for the status parameter."""

    def test_approved_filter(self, client, test_db):
        """status=approved should only return approved tweets."""
        tweets = [
            make_tweet(id="1", text="Approved"),
            make_tweet(id="2", text="Filtered"),
            make_tweet(id="3", text="Pending"),
        ]
        create_and_store_tweets(tweets, test_db)
        approve_tweets(["1"], test_db)
        filter_tweets(["2"], test_db)
        # Tweet 3 has no decision -> pending

        resp = client.get("/api/ui/tweets?status=approved")
        data = resp.get_json()

        assert len(data["tweets"]) == 1
        assert data["tweets"][0]["id"] == "1"

    def test_filtered_filter(self, client, test_db):
        """status=filtered should only return filtered tweets."""
        tweets = [
            make_tweet(id="1", text="Approved"),
            make_tweet(id="2", text="Filtered"),
        ]
        create_and_store_tweets(tweets, test_db)
        approve_tweets(["1"], test_db)
        filter_tweets(["2"], test_db)

        resp = client.get("/api/ui/tweets?status=filtered")
        data = resp.get_json()

        assert len(data["tweets"]) == 1
        assert data["tweets"][0]["id"] == "2"

    def test_pending_filter(self, client, test_db):
        """status=pending should return tweets without decisions."""
        tweets = [
            make_tweet(id="1", text="Approved"),
            make_tweet(id="2", text="Pending"),
        ]
        create_and_store_tweets(tweets, test_db)
        approve_tweets(["1"], test_db)
        # Tweet 2 has no decision

        resp = client.get("/api/ui/tweets?status=pending")
        data = resp.get_json()

        assert len(data["tweets"]) == 1
        assert data["tweets"][0]["id"] == "2"


class TestPagination:
    """Tests for pagination parameters."""

    def test_limit_parameter(self, client, test_db):
        """limit parameter should restrict number of results."""
        tweets = [make_tweet(id=str(i), text=f"Tweet {i}") for i in range(10)]
        create_and_store_tweets(tweets, test_db)
        approve_tweets([str(i) for i in range(10)], test_db)

        resp = client.get("/api/ui/tweets?status=approved&limit=3")
        data = resp.get_json()

        assert len(data["tweets"]) == 3
        assert data["total"] == 10

    def test_offset_parameter(self, client, test_db):
        """offset parameter should skip results."""
        tweets = [make_tweet(id=str(i), text=f"Tweet {i}") for i in range(10)]
        create_and_store_tweets(tweets, test_db)
        approve_tweets([str(i) for i in range(10)], test_db)

        resp = client.get("/api/ui/tweets?status=approved&limit=3&offset=3")
        data = resp.get_json()

        assert len(data["tweets"]) == 3
        assert data["total"] == 10


class TestCursorEncoding:
    """Tests for cursor encode/decode functions."""

    def test_encode_decode_roundtrip(self):
        """Cursor should encode and decode back to original values."""
        created_at = "2025-01-15T12:00:00+00:00"
        tweet_id = "12345"

        cursor = encode_cursor(created_at, tweet_id)
        decoded = decode_cursor(cursor)

        assert decoded is not None
        assert decoded[0] == created_at
        assert decoded[1] == tweet_id

    def test_decode_invalid_cursor(self):
        """Invalid cursor should return None."""
        assert decode_cursor("invalid") is None
        assert decode_cursor("") is None
        assert decode_cursor("not-base64!!!") is None

    def test_cursor_is_url_safe(self):
        """Cursor should be URL-safe (no special chars that need escaping)."""
        cursor = encode_cursor("2025-01-15T12:00:00+00:00", "12345")
        # URL-safe base64 uses - and _ instead of + and /
        assert "+" not in cursor
        assert "/" not in cursor


class TestCursorBasedPagination:
    """Tests for cursor-based pagination in /api/ui/chains endpoint."""

    def test_chains_returns_cursors(self, client, test_db):
        """API should return oldest_cursor and newest_cursor."""
        now = datetime.utcnow()
        tweets = [
            make_tweet(id="1", text="Tweet 1", created_at=(now - timedelta(hours=2)).isoformat()),
            make_tweet(id="2", text="Tweet 2", created_at=(now - timedelta(hours=1)).isoformat()),
            make_tweet(id="3", text="Tweet 3", created_at=now.isoformat()),
        ]
        create_and_store_tweets(tweets, test_db)
        approve_tweets(["1", "2", "3"], test_db)

        resp = client.get("/api/ui/chains?mode=default&sort=created_at")
        assert resp.status_code == 200
        data = resp.get_json()

        assert "oldest_cursor" in data
        assert "newest_cursor" in data
        assert data["oldest_cursor"] is not None
        assert data["newest_cursor"] is not None

    def test_before_cursor_loads_older_posts(self, client, test_db):
        """before_cursor should load posts older than the cursor."""
        now = datetime.utcnow()
        tweets = [
            make_tweet(id="1", text="Oldest", created_at=(now - timedelta(hours=4)).isoformat()),
            make_tweet(id="2", text="Older", created_at=(now - timedelta(hours=3)).isoformat()),
            make_tweet(id="3", text="Middle", created_at=(now - timedelta(hours=2)).isoformat()),
            make_tweet(id="4", text="Newer", created_at=(now - timedelta(hours=1)).isoformat()),
            make_tweet(id="5", text="Newest", created_at=now.isoformat()),
        ]
        create_and_store_tweets(tweets, test_db)
        approve_tweets(["1", "2", "3", "4", "5"], test_db)

        # Get first page
        resp = client.get("/api/ui/chains?mode=default&sort=created_at&limit=2")
        data = resp.get_json()

        # Should get newest 2: 5 and 4
        assert len(data["chains"]) == 2
        chain_ids = [c["chain"][0]["id"] for c in data["chains"]]
        assert "5" in chain_ids
        assert "4" in chain_ids

        # Use oldest_cursor to get next page
        oldest_cursor = data["oldest_cursor"]
        resp2 = client.get(f"/api/ui/chains?mode=default&sort=created_at&limit=2&before_cursor={oldest_cursor}")
        data2 = resp2.get_json()

        # Should get next 2 older: 3 and 2
        assert len(data2["chains"]) == 2
        chain_ids2 = [c["chain"][0]["id"] for c in data2["chains"]]
        assert "3" in chain_ids2
        assert "2" in chain_ids2

    def test_count_new_endpoint(self, client, test_db):
        """count-new endpoint should return count of posts newer than cursor."""
        now = datetime.utcnow()
        tweets = [
            make_tweet(id="1", text="Old", created_at=(now - timedelta(hours=2)).isoformat()),
            make_tweet(id="2", text="Middle", created_at=(now - timedelta(hours=1)).isoformat()),
            make_tweet(id="3", text="New", created_at=now.isoformat()),
        ]
        create_and_store_tweets(tweets, test_db)
        approve_tweets(["1", "2", "3"], test_db)

        # Create cursor pointing to tweet 2
        cursor = encode_cursor((now - timedelta(hours=1)).isoformat(), "2")

        resp = client.get(f"/api/ui/chains/count-new?mode=default&after_cursor={cursor}&sort=created_at")
        assert resp.status_code == 200
        data = resp.get_json()

        # Should count 1 post newer than cursor (tweet 3)
        assert data["count"] == 1

    def test_count_new_no_cursor_returns_zero(self, client, test_db):
        """count-new without cursor should return 0."""
        resp = client.get("/api/ui/chains/count-new?mode=default")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["count"] == 0

    def test_identical_timestamps_tie_breaking(self, client, test_db):
        """Posts with identical timestamps should be ordered consistently and cursor pagination should not skip posts."""
        same_time = "2025-01-15T12:00:00+00:00"
        tweets = [
            make_tweet(id="aaa", text="First by ID", created_at=same_time),
            make_tweet(id="bbb", text="Second by ID", created_at=same_time),
            make_tweet(id="ccc", text="Third by ID", created_at=same_time),
        ]
        create_and_store_tweets(tweets, test_db)
        approve_tweets(["aaa", "bbb", "ccc"], test_db)

        # Get first page - when timestamps are identical, cursor pagination uses ID as tiebreaker
        # The initial request (no cursor) returns results ordered by (created_at DESC, id DESC)
        resp = client.get("/api/ui/chains?mode=default&sort=created_at&limit=2")
        data = resp.get_json()

        assert len(data["chains"]) == 2
        first_page_ids = {c["chain"][0]["id"] for c in data["chains"]}

        # Get next page using cursor - should get the remaining post without duplicates
        oldest_cursor = data["oldest_cursor"]
        resp2 = client.get(f"/api/ui/chains?mode=default&sort=created_at&limit=2&before_cursor={oldest_cursor}")
        data2 = resp2.get_json()

        assert len(data2["chains"]) == 1
        second_page_ids = {c["chain"][0]["id"] for c in data2["chains"]}

        # Combined, all three should be present with no duplicates
        all_ids = first_page_ids | second_page_ids
        assert all_ids == {"aaa", "bbb", "ccc"}
        assert len(first_page_ids & second_page_ids) == 0  # No overlap

    def test_offset_still_works_for_backwards_compatibility(self, client, test_db):
        """offset parameter should still work for backwards compatibility."""
        now = datetime.utcnow()
        tweets = [
            make_tweet(id=str(i), text=f"Tweet {i}", created_at=(now - timedelta(hours=i)).isoformat())
            for i in range(5)
        ]
        create_and_store_tweets(tweets, test_db)
        approve_tweets([str(i) for i in range(5)], test_db)

        # Use offset-based pagination
        resp = client.get("/api/ui/chains?mode=default&sort=created_at&limit=2&offset=2")
        data = resp.get_json()

        assert len(data["chains"]) == 2
        # Should return cursors even with offset-based pagination
        assert data["oldest_cursor"] is not None
        assert data["newest_cursor"] is not None

    def test_empty_results_return_null_cursors(self, client, test_db):
        """Empty results should return null cursors."""
        resp = client.get("/api/ui/chains?mode=default")
        data = resp.get_json()

        assert data["chains"] == []
        assert data["oldest_cursor"] is None
        assert data["newest_cursor"] is None
