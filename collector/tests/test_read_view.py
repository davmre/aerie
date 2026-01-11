"""Integration tests for the Read view API."""

from tests.fixtures import (
    approve_tweets,
    create_and_store_tweets,
    filter_tweets,
    make_thread,
    make_tweet,
)


class TestConversationClustering:
    """Tests for the conversation clustering feature (group_conversations=true with status=approved)."""

    def test_linear_thread_becomes_single_cluster(self, client, test_db):
        """
        A linear thread A -> B -> C should become a single conversation cluster
        with the full chain as primary_chain.
        """
        tweets = [
            make_tweet(id="1", text="Tweet A"),
            make_tweet(id="2", text="Tweet B", reply_to_tweet_id="1"),
            make_tweet(id="3", text="Tweet C", reply_to_tweet_id="2"),
        ]
        create_and_store_tweets(tweets, test_db)
        approve_tweets(["1", "2", "3"], test_db)

        resp = client.get("/api/ui/tweets?status=approved&group_conversations=true")
        assert resp.status_code == 200
        data = resp.get_json()

        # Should return conversations, not tweets
        assert "conversations" in data
        assert len(data["conversations"]) == 1

        # The primary chain should contain all three tweets in order
        chain = data["conversations"][0]["primary_chain"]
        assert len(chain) == 3
        assert chain[0]["id"] == "1"  # Root first
        assert chain[1]["id"] == "2"
        assert chain[2]["id"] == "3"  # Leaf last

    def test_independent_tweets_are_separate_clusters(self, client, test_db):
        """Multiple independent tweets should each be their own cluster."""
        tweets = [
            make_tweet(id="1", text="Independent A", author_username="user1"),
            make_tweet(id="2", text="Independent B", author_username="user2"),
            make_tweet(id="3", text="Independent C", author_username="user3"),
        ]
        create_and_store_tweets(tweets, test_db)
        approve_tweets(["1", "2", "3"], test_db)

        resp = client.get("/api/ui/tweets?status=approved&group_conversations=true")
        assert resp.status_code == 200
        data = resp.get_json()

        # Three separate clusters, each with a single tweet
        assert len(data["conversations"]) == 3
        for conv in data["conversations"]:
            assert len(conv["primary_chain"]) == 1

    def test_fan_out_picks_longest_chain(self, client, test_db):
        """
        When multiple tweets reply to the same parent, pick the longest chain.
        A <- B <- C  (length 3)
        A <- D       (length 2)
        Should show A -> B -> C as primary, with D as hidden reply.
        """
        tweets = [
            make_tweet(id="A", text="Root"),
            make_tweet(id="B", text="Reply B", reply_to_tweet_id="A"),
            make_tweet(id="C", text="Reply C to B", reply_to_tweet_id="B"),
            make_tweet(id="D", text="Reply D to A", reply_to_tweet_id="A"),
        ]
        create_and_store_tweets(tweets, test_db)
        approve_tweets(["A", "B", "C", "D"], test_db)

        resp = client.get("/api/ui/tweets?status=approved&group_conversations=true")
        data = resp.get_json()

        assert len(data["conversations"]) == 1
        conv = data["conversations"][0]

        # Primary chain should be A -> B -> C (longest)
        chain_ids = [t["id"] for t in conv["primary_chain"]]
        assert chain_ids == ["A", "B", "C"]

        # D should be a hidden reply to A
        assert conv["hidden_reply_counts"].get("A") == 1

    def test_fan_out_equal_length_picks_by_engagement(self, client, test_db):
        """
        When chains are equal length, pick by highest engagement.
        A <- B (B has 100 likes)
        A <- C (C has 10 likes)
        Should pick A -> B as primary.
        """
        tweets = [
            make_tweet(id="A", text="Root"),
            make_tweet(id="B", text="Popular reply", reply_to_tweet_id="A", like_count=100),
            make_tweet(id="C", text="Less popular", reply_to_tweet_id="A", like_count=10),
        ]
        create_and_store_tweets(tweets, test_db)
        approve_tweets(["A", "B", "C"], test_db)

        resp = client.get("/api/ui/tweets?status=approved&group_conversations=true")
        data = resp.get_json()

        assert len(data["conversations"]) == 1
        conv = data["conversations"][0]

        # Primary chain should be A -> B (higher engagement)
        chain_ids = [t["id"] for t in conv["primary_chain"]]
        assert chain_ids == ["A", "B"]

        # C should be hidden
        assert conv["hidden_reply_counts"].get("A") == 1

    def test_quoted_tweet_in_chain(self, client, test_db):
        """Tweets in the chain should include their quoted tweets."""
        tweets = [
            make_tweet(id="Z", text="Quoted original"),
            make_tweet(id="A", text="Quote tweet", quoted_tweet_id="Z"),
        ]
        create_and_store_tweets(tweets, test_db)
        approve_tweets(["A"], test_db)  # Only approve A, not Z

        resp = client.get("/api/ui/tweets?status=approved&group_conversations=true")
        data = resp.get_json()

        assert len(data["conversations"]) == 1
        chain = data["conversations"][0]["primary_chain"]
        assert len(chain) == 1
        assert chain[0]["id"] == "A"
        assert chain[0].get("quoted_tweet") is not None
        assert chain[0]["quoted_tweet"]["id"] == "Z"


class TestGroupConversationsFalse:
    """Tests for when group_conversations=false (returns individual tweets)."""

    def test_ungrouped_returns_all_tweets(self, client, test_db):
        """When group_conversations=false, all tweets in thread are returned individually."""
        tweets = make_thread(base_id=100, length=3)
        create_and_store_tweets(tweets, test_db)
        approve_tweets(["100", "101", "102"], test_db)

        resp = client.get("/api/ui/tweets?status=approved&group_conversations=false")
        assert resp.status_code == 200
        data = resp.get_json()

        # Should return tweets, not conversations
        assert "tweets" in data
        assert len(data["tweets"]) == 3


class TestRepliesEndpoint:
    """Tests for the /api/ui/tweet/<id>/replies endpoint."""

    def test_get_direct_replies(self, client, test_db):
        """Should return the tweet and its direct approved replies."""
        tweets = [
            make_tweet(id="A", text="Parent"),
            make_tweet(id="B", text="Reply 1", reply_to_tweet_id="A"),
            make_tweet(id="C", text="Reply 2", reply_to_tweet_id="A"),
            make_tweet(id="D", text="Reply to B", reply_to_tweet_id="B"),  # Not direct
        ]
        create_and_store_tweets(tweets, test_db)
        approve_tweets(["A", "B", "C", "D"], test_db)

        resp = client.get("/api/ui/tweet/A/replies?mode=default")
        assert resp.status_code == 200
        data = resp.get_json()

        # Should have parent tweet A
        assert data["tweet"]["id"] == "A"

        # Should have direct replies B and C, but not D
        reply_ids = {r["id"] for r in data["replies"]}
        assert reply_ids == {"B", "C"}

    def test_replies_include_nested_reply_count(self, client, test_db):
        """Replies should include count of their own approved replies."""
        tweets = [
            make_tweet(id="A", text="Parent"),
            make_tweet(id="B", text="Reply with children", reply_to_tweet_id="A"),
            make_tweet(id="C", text="Reply without children", reply_to_tweet_id="A"),
            make_tweet(id="D", text="Reply to B", reply_to_tweet_id="B"),
            make_tweet(id="E", text="Another reply to B", reply_to_tweet_id="B"),
        ]
        create_and_store_tweets(tweets, test_db)
        approve_tweets(["A", "B", "C", "D", "E"], test_db)

        resp = client.get("/api/ui/tweet/A/replies?mode=default")
        data = resp.get_json()

        # Find B and C in replies
        reply_b = next(r for r in data["replies"] if r["id"] == "B")
        reply_c = next(r for r in data["replies"] if r["id"] == "C")

        # B should have 2 nested replies
        assert reply_b["reply_count_approved"] == 2

        # C should have 0 nested replies
        assert reply_c["reply_count_approved"] == 0

    def test_replies_not_found(self, client, test_db):
        """Should return 404 for non-existent tweet."""
        resp = client.get("/api/ui/tweet/nonexistent/replies?mode=default")
        assert resp.status_code == 404


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

        # Use group_conversations=false to get tweets format
        resp = client.get("/api/ui/tweets?status=approved&group_conversations=false")
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

        # Use group_conversations=false to get individual tweets
        resp = client.get("/api/ui/tweets?status=approved&limit=3&group_conversations=false")
        data = resp.get_json()

        assert len(data["tweets"]) == 3
        assert data["total"] == 10

    def test_offset_parameter(self, client, test_db):
        """offset parameter should skip results."""
        tweets = [make_tweet(id=str(i), text=f"Tweet {i}") for i in range(10)]
        create_and_store_tweets(tweets, test_db)
        approve_tweets([str(i) for i in range(10)], test_db)

        resp = client.get("/api/ui/tweets?status=approved&limit=3&offset=3&group_conversations=false")
        data = resp.get_json()

        assert len(data["tweets"]) == 3
        assert data["total"] == 10

    def test_conversation_pagination(self, client, test_db):
        """Pagination should work with conversation clusters."""
        # Create 5 independent tweets (5 separate conversations)
        tweets = [make_tweet(id=str(i), text=f"Tweet {i}") for i in range(5)]
        create_and_store_tweets(tweets, test_db)
        approve_tweets([str(i) for i in range(5)], test_db)

        resp = client.get("/api/ui/tweets?status=approved&limit=2&group_conversations=true")
        data = resp.get_json()

        assert len(data["conversations"]) == 2
        assert data["total"] == 5
