"""Tests for prefilter execution at collection time."""

from database import (
    create_mode,
    get_mode_decisions_batch,
    store_tweets,
)
from modes import run_prefilters_for_new_tweets
from tests.fixtures import make_tweet


class TestStoreReturnsInsertedTweets:
    """Tests that store_tweets() returns inserted tweets."""

    def test_returns_inserted_tweets_on_new_tweets(self, test_db):
        """store_tweets should return list of actually inserted tweet dicts."""
        tweets = [make_tweet(id="1"), make_tweet(id="2")]
        result = store_tweets(tweets, test_db)

        assert result["inserted"] == 2
        assert len(result["inserted_tweets"]) == 2
        assert result["inserted_tweets"][0]["id"] == "1"
        assert result["inserted_tweets"][1]["id"] == "2"

    def test_returns_empty_on_duplicates(self, test_db):
        """Storing same tweets again should return empty inserted_tweets."""
        tweets = [make_tweet(id="1"), make_tweet(id="2")]
        store_tweets(tweets, test_db)

        # Store again
        result = store_tweets(tweets, test_db)

        assert result["inserted"] == 0
        assert result["duplicates"] == 2
        assert len(result["inserted_tweets"]) == 0

    def test_partial_duplicates(self, test_db):
        """Mixed new and duplicate tweets should only return new ones."""
        # First store some tweets
        tweets1 = [make_tweet(id="1"), make_tweet(id="2")]
        store_tweets(tweets1, test_db)

        # Now store a mix of new and existing
        tweets2 = [make_tweet(id="2"), make_tweet(id="3")]
        result = store_tweets(tweets2, test_db)

        assert result["inserted"] == 1
        assert result["duplicates"] == 1
        assert len(result["inserted_tweets"]) == 1
        assert result["inserted_tweets"][0]["id"] == "3"


class TestRunPrefiltersForNewTweets:
    """Tests for run_prefilters_for_new_tweets()."""

    def test_runs_prefilters_for_all_modes(self, test_db):
        """Should run prefilters for all modes with prefilters configured."""
        # Create a mode with approve_all prefilter
        create_mode(
            mode_id="approve_mode",
            name="Approve All",
            prompt_id="binary_filter_v1",
            extractor="binary_approved",
            prefilter="approve_all",
            db_path=test_db,
        )

        tweets = [make_tweet(id="1"), make_tweet(id="2")]
        store_tweets(tweets, test_db)

        stats = run_prefilters_for_new_tweets(tweets, test_db)

        # approve_all should decide all tweets
        assert stats["approve_mode"] == 2

        # Verify decisions were cached
        decisions = get_mode_decisions_batch(["1", "2"], "approve_mode", test_db)
        assert decisions["1"] == "approved"
        assert decisions["2"] == "approved"

    def test_skips_modes_without_prefilters(self, test_db):
        """Modes without prefilters should be skipped (stats show 0)."""
        tweets = [make_tweet(id="1")]
        store_tweets(tweets, test_db)

        stats = run_prefilters_for_new_tweets(tweets, test_db)

        # default mode has no prefilter
        assert stats.get("default", 0) == 0

    def test_partial_prefilter_decisions(self, test_db):
        """Prefilters that return None for some tweets should only cache decided ones."""
        # Create mode with skip_retweets prefilter
        create_mode(
            mode_id="skip_rt_mode",
            name="Skip Retweets",
            prompt_id="binary_filter_v1",
            extractor="binary_approved",
            prefilter="skip_retweets",
            db_path=test_db,
        )

        tweets = [
            make_tweet(id="1", is_retweet=True),   # Should be filtered
            make_tweet(id="2", is_retweet=False),  # Prefilter returns None
        ]
        store_tweets(tweets, test_db)

        stats = run_prefilters_for_new_tweets(tweets, test_db)

        # Only the retweet should have a decision
        assert stats["skip_rt_mode"] == 1

        decisions = get_mode_decisions_batch(["1", "2"], "skip_rt_mode", test_db)
        assert decisions["1"] == "filtered"
        assert "2" not in decisions  # No decision yet (needs LLM)

    def test_does_not_overwrite_existing_decisions(self, test_db):
        """Should skip tweets that already have decisions."""
        # Create mode with approve_all
        create_mode(
            mode_id="approve_mode",
            name="Approve All",
            prompt_id="binary_filter_v1",
            extractor="binary_approved",
            prefilter="approve_all",
            db_path=test_db,
        )

        # Store and run prefilters
        tweets = [make_tweet(id="1")]
        store_tweets(tweets, test_db)
        run_prefilters_for_new_tweets(tweets, test_db)

        # Run again - should not double-count
        stats = run_prefilters_for_new_tweets(tweets, test_db)
        assert stats["approve_mode"] == 0  # Already decided

    def test_empty_tweets_list(self, test_db):
        """Empty tweets list should return empty stats."""
        stats = run_prefilters_for_new_tweets([], test_db)
        assert stats == {}


class TestPrefiltersOnCollectionIntegration:
    """Integration tests for prefilters running at collection time via server."""

    def test_endpoint_runs_prefilters(self, client, test_db):
        """POST /tweets should run prefilters and return prefilter_decisions count."""
        # Create a mode with approve_all prefilter
        create_mode(
            mode_id="approve_mode",
            name="Approve All",
            prompt_id="binary_filter_v1",
            extractor="binary_approved",
            prefilter="approve_all",
            db_path=test_db,
        )

        tweets = [
            {"id": "1", "text": "Hello", "author": {"username": "user1"}},
            {"id": "2", "text": "World", "author": {"username": "user2"}},
        ]

        resp = client.post("/tweets", json={"tweets": tweets})
        assert resp.status_code == 200
        data = resp.get_json()

        assert data["inserted"] == 2
        assert data["prefilter_decisions"] > 0  # At least some prefilter decisions made

    def test_prefilter_decisions_available_immediately(self, client, test_db):
        """After POST /tweets, prefilter decisions should be queryable."""
        # Create a mode with approve_all prefilter
        create_mode(
            mode_id="approve_mode",
            name="Approve All",
            prompt_id="binary_filter_v1",
            extractor="binary_approved",
            prefilter="approve_all",
            db_path=test_db,
        )

        tweets = [{"id": "1", "text": "Hello", "author": {"username": "user1"}}]
        client.post("/tweets", json={"tweets": tweets})

        # Check status via /tweets/check
        resp = client.post("/tweets/check", json={"ids": ["1"], "mode": "approve_mode"})
        assert resp.status_code == 200
        data = resp.get_json()

        assert data["1"] == "approved"
