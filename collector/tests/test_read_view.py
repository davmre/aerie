"""Integration tests for the Read view API."""

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
