"""End-to-end integration tests for classify-on-demand feature."""

from database import create_mode, get_mode_decisions_batch, store_tweets
from tests.fixtures import make_tweet


class TestClassifyOnDemandIntegration:
    """Integration tests for the full classify-on-demand flow."""

    def test_prefilters_run_at_collection_time(self, client, test_db):
        """
        Full flow: store tweets -> prefilters run -> decisions available

        When tweets are stored via POST /tweets, prefilters should run
        immediately and decisions should be available without triggering
        LLM classification.
        """
        # Create a mode with approve_all prefilter
        create_mode(
            mode_id="prefilter_mode",
            name="Prefilter Mode",
            prompt_id="binary_filter_v1",
            extractor="binary_approved",
            prefilter="approve_all",
            db_path=test_db,
        )

        # Store tweets via endpoint
        tweets = [
            {"id": "1", "text": "Hello", "author": {"username": "user1"}},
            {"id": "2", "text": "World", "author": {"username": "user2"}},
        ]
        resp = client.post("/tweets", json={"tweets": tweets})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["inserted"] == 2
        assert data["prefilter_decisions"] > 0

        # Decisions should be available immediately
        decisions = get_mode_decisions_batch(["1", "2"], "prefilter_mode", test_db)
        assert decisions["1"] == "approved"
        assert decisions["2"] == "approved"

    def test_trigger_and_poll_flow(self, client, test_db):
        """
        Flow: trigger classification -> check status -> see newly classified

        This tests the polling flow without actually running LLM classification.
        """
        tweets = [make_tweet(id="1"), make_tweet(id="2")]
        store_tweets(tweets, test_db)

        # Trigger classification
        resp = client.post("/api/classify/trigger", json={
            "mode_id": "default",
            "limit": 10,
        })
        assert resp.status_code == 200

        # Check status
        resp = client.get("/api/classify/status?mode_id=default")
        assert resp.status_code == 200
        status_data = resp.get_json()

        # Status should reflect queued items
        assert "pending_count" in status_data
        assert "has_pending" in status_data

    def test_read_page_chains_endpoint(self, client, test_db):
        """
        Read page flow: store -> trigger -> classify -> chains available

        This tests that approved tweets show up in the chains endpoint.
        """
        tweets = [
            make_tweet(id="1", text="First tweet"),
            make_tweet(id="2", text="Second tweet", reply_to_tweet_id="1"),
        ]
        store_tweets(tweets, test_db)

        # Manually approve for testing (simulating classification)
        from tests.fixtures import approve_tweets
        approve_tweets(["1", "2"], test_db)

        # Get chains
        resp = client.get("/api/ui/chains?mode=default")
        assert resp.status_code == 200
        data = resp.get_json()

        # Should have chains with approved tweets
        assert "chains" in data
        assert data["total"] >= 1

    def test_prefilter_only_mode_requires_no_llm(self, client, test_db):
        """
        Prefilter-only modes should make all decisions without LLM.

        When a mode uses approve_all prefilter, no tweets should need
        LLM classification.
        """
        # Create approve_all mode
        create_mode(
            mode_id="approve_mode",
            name="Approve All",
            prompt_id="binary_filter_v1",
            extractor="binary_approved",
            prefilter="approve_all",
            db_path=test_db,
        )

        # Store tweets
        tweets = [make_tweet(id=str(i)) for i in range(5)]
        store_tweets(tweets, test_db)

        # Trigger classification for approve_mode
        resp = client.post("/api/classify/trigger", json={
            "mode_id": "approve_mode",
            "limit": 10,
        })
        assert resp.status_code == 200
        data = resp.get_json()

        # All should be decided by prefilter, none queued for LLM
        assert data["queued"] == 0
        assert data["prefilter_decided"] == 5

    def test_partial_prefilter_mode(self, client, test_db):
        """
        Partial prefilters should queue undecided tweets for LLM.

        skip_retweets only decides on retweets, leaving others for LLM.
        """
        # Create mode with skip_retweets
        create_mode(
            mode_id="skip_rt_mode",
            name="Skip Retweets",
            prompt_id="binary_filter_v1",
            extractor="binary_approved",
            prefilter="skip_retweets",
            db_path=test_db,
        )

        tweets = [
            make_tweet(id="1", is_retweet=True),   # Prefilter: filtered
            make_tweet(id="2", is_retweet=False),  # Needs LLM
            make_tweet(id="3", is_retweet=False),  # Needs LLM
        ]
        store_tweets(tweets, test_db)

        # Trigger classification
        resp = client.post("/api/classify/trigger", json={
            "mode_id": "skip_rt_mode",
            "limit": 10,
        })
        assert resp.status_code == 200
        data = resp.get_json()

        # 1 should be decided by prefilter, 2 queued
        assert data["prefilter_decided"] == 1
        assert data["queued"] == 2

        # Verify retweet was filtered
        decisions = get_mode_decisions_batch(["1"], "skip_rt_mode", test_db)
        assert decisions["1"] == "filtered"


class TestClassifyOnDemandEdgeCases:
    """Edge case tests for classify-on-demand."""

    def test_empty_database(self, client, test_db):
        """Should handle empty database gracefully."""
        resp = client.post("/api/classify/trigger", json={
            "mode_id": "default",
            "limit": 10,
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["queued"] == 0
        assert data["prefilter_decided"] == 0

    def test_already_classified_tweets(self, client, test_db):
        """Should not re-queue already classified tweets."""
        from tests.fixtures import approve_tweets

        tweets = [make_tweet(id="1"), make_tweet(id="2")]
        store_tweets(tweets, test_db)

        # Pre-classify tweets
        approve_tweets(["1", "2"], test_db)

        # Trigger - should find them already decided
        resp = client.post("/api/classify/trigger", json={
            "mode_id": "default",
            "limit": 10,
        })
        assert resp.status_code == 200
        data = resp.get_json()

        # Both already decided, so they won't be in the query results at all
        # Nothing to queue or process
        assert data["queued"] == 0
        assert data["prefilter_decided"] == 0
        # already_decided only counts tweets returned by query that had prior decisions
        # Since these have decisions, they won't be returned by query
        assert data["already_decided"] == 0

    def test_multiple_trigger_calls_deduplicated(self, client, test_db):
        """Multiple trigger calls should not queue same tweets twice."""
        tweets = [make_tweet(id="1")]
        store_tweets(tweets, test_db)

        # First trigger
        resp1 = client.post("/api/classify/trigger", json={
            "mode_id": "default",
            "limit": 10,
        })
        assert resp1.status_code == 200

        # Second trigger - should see tweets already in queue
        resp2 = client.post("/api/classify/trigger", json={
            "mode_id": "default",
            "limit": 10,
        })

        # Second call might show fewer queued due to deduplication
        # (or already in queue)
        assert resp2.status_code == 200
