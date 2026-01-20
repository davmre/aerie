"""Tests for prefilter decision caching in mode_decisions table."""

import json

from database import create_mode, get_mode_decisions_batch, transaction
from tests.fixtures import create_and_store_tweets, make_tweet


class TestPrefilterDecisionCaching:
    """Test that prefilter decisions are properly cached in mode_decisions table."""

    def test_approve_all_prefilter_caches_on_check(self, client, test_db):
        """
        When tweets are checked via /tweets/check with an approve_all prefilter,
        the decisions should be cached in mode_decisions table.

        This reproduces the issue where tweets show as "approved" in the browser
        but "pending" in the web UI.
        """
        # Create a mode with approve_all prefilter
        create_mode(
            mode_id="test_approve_all",
            name="Test Approve All",
            prompt_id="binary_filter_v1",  # Required but won't be used due to prefilter
            extractor="binary_approved",
            prefilter="approve_all",
            db_path=test_db,
        )

        # Create and store some tweets
        tweets = [
            make_tweet(id="1", text="Tweet 1"),
            make_tweet(id="2", text="Tweet 2"),
            make_tweet(id="3", text="Tweet 3"),
        ]
        create_and_store_tweets(tweets, test_db)

        # Simulate browser extension checking tweet status
        response = client.post(
            "/tweets/check",
            json={"ids": ["1", "2", "3"], "mode": "test_approve_all"},
        )
        assert response.status_code == 200
        statuses = response.get_json()

        # All tweets should be approved (browser sees this)
        assert statuses["1"] == "approved"
        assert statuses["2"] == "approved"
        assert statuses["3"] == "approved"

        # CRITICAL: Decisions should be cached in mode_decisions table
        cached_decisions = get_mode_decisions_batch(
            ["1", "2", "3"], "test_approve_all", test_db
        )

        # This is the bug - cached_decisions is empty even though statuses show "approved"
        assert "1" in cached_decisions, "Tweet 1 should have cached decision"
        assert "2" in cached_decisions, "Tweet 2 should have cached decision"
        assert "3" in cached_decisions, "Tweet 3 should have cached decision"
        assert cached_decisions["1"] == "approved"
        assert cached_decisions["2"] == "approved"
        assert cached_decisions["3"] == "approved"

        # Verify source is 'prefilter' in database
        with transaction(test_db) as conn:
            rows = conn.execute(
                "SELECT tweet_id, decision, source FROM mode_decisions WHERE mode_id = ?",
                ("test_approve_all",),
            ).fetchall()

        assert len(rows) == 3
        for row in rows:
            assert row["decision"] == "approved"
            assert row["source"] == "prefilter"

    def test_web_ui_shows_approved_not_pending(self, client, test_db):
        """
        After tweets are checked with approve_all prefilter,
        the web UI should show them as approved, not pending.
        """
        # Create mode with approve_all prefilter
        create_mode(
            mode_id="test_mode",
            name="Test Mode",
            prompt_id="binary_filter_v1",
            extractor="binary_approved",
            prefilter="approve_all",
            db_path=test_db,
        )

        # Store tweets
        tweets = [
            make_tweet(id="100", text="Test tweet 100"),
            make_tweet(id="101", text="Test tweet 101"),
        ]
        create_and_store_tweets(tweets, test_db)

        # Browser checks status (simulates extension behavior)
        client.post(
            "/tweets/check",
            json={"ids": ["100", "101"], "mode": "test_mode"},
        )

        # Web UI requests approved tweets
        response = client.get("/api/ui/tweets?status=approved&mode=test_mode")
        assert response.status_code == 200
        data = response.get_json()

        # Both tweets should appear in approved list
        assert data["total"] == 2
        tweet_ids = {t["id"] for t in data["tweets"]}
        assert tweet_ids == {"100", "101"}

        # Web UI requests pending tweets
        response = client.get("/api/ui/tweets?status=pending&mode=test_mode")
        assert response.status_code == 200
        data = response.get_json()

        # No tweets should be pending
        assert data["total"] == 0
        assert len(data["tweets"]) == 0

    def test_multiple_checks_dont_duplicate_decisions(self, client, test_db):
        """
        Calling /tweets/check multiple times for the same tweets
        shouldn't create duplicate entries in mode_decisions.
        """
        create_mode(
            mode_id="multi_check",
            name="Multi Check",
            prompt_id="binary_filter_v1",
            extractor="binary_approved",
            prefilter="approve_all",
            db_path=test_db,
        )

        tweets = [make_tweet(id="200", text="Test")]
        create_and_store_tweets(tweets, test_db)

        # Check the same tweet multiple times
        for _ in range(3):
            response = client.post(
                "/tweets/check",
                json={"ids": ["200"], "mode": "multi_check"},
            )
            assert response.status_code == 200
            assert response.get_json()["200"] == "approved"

        # Should have exactly one entry in mode_decisions
        with transaction(test_db) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM mode_decisions WHERE tweet_id = ? AND mode_id = ?",
                ("200", "multi_check"),
            ).fetchone()[0]

        assert count == 1

    def test_prefilter_decision_source_is_recorded(self, client, test_db):
        """
        Decisions made by prefilter should have source='prefilter' in database.
        This helps distinguish them from LLM-based decisions.
        """
        create_mode(
            mode_id="source_test",
            name="Source Test",
            prompt_id="binary_filter_v1",
            extractor="binary_approved",
            prefilter="approve_all",
            db_path=test_db,
        )

        tweets = [make_tweet(id="300", text="Test")]
        create_and_store_tweets(tweets, test_db)

        # Check tweets
        client.post(
            "/tweets/check",
            json={"ids": ["300"], "mode": "source_test"},
        )

        # Verify source field
        with transaction(test_db) as conn:
            row = conn.execute(
                "SELECT source FROM mode_decisions WHERE tweet_id = ? AND mode_id = ?",
                ("300", "source_test"),
            ).fetchone()

        assert row is not None
        assert row["source"] == "prefilter"

    def test_stats_endpoint_reflects_cached_decisions(self, client, test_db):
        """
        The /stats endpoint should show correct counts based on cached decisions.
        """
        create_mode(
            mode_id="stats_test",
            name="Stats Test",
            prompt_id="binary_filter_v1",
            extractor="binary_approved",
            prefilter="approve_all",
            db_path=test_db,
        )

        # Create and check 5 tweets
        tweets = [make_tweet(id=str(i), text=f"Tweet {i}") for i in range(400, 405)]
        create_and_store_tweets(tweets, test_db)

        client.post(
            "/tweets/check",
            json={"ids": [str(i) for i in range(400, 405)], "mode": "stats_test"},
        )

        # Check stats
        response = client.get("/stats?mode=stats_test")
        assert response.status_code == 200
        stats = response.get_json()

        assert stats["total"] == 5
        assert stats["approved"] == 5
        assert stats["filtered"] == 0
        assert stats["pending"] == 0

    def test_partial_prefilter_only_caches_matched_tweets(self, client, test_db):
        """
        Prefilters that return None for some tweets should only cache
        decisions for tweets they actually decide on.
        """
        # Create mode with skip_retweets prefilter (returns False for retweets, None otherwise)
        create_mode(
            mode_id="partial_filter",
            name="Partial Filter",
            prompt_id="binary_filter_v1",
            extractor="binary_approved",
            prefilter="skip_retweets",
            db_path=test_db,
        )

        tweets = [
            make_tweet(id="500", text="Normal tweet", is_retweet=False),
            make_tweet(id="501", text="RT @someone: retweet", is_retweet=True),
            make_tweet(id="502", text="Another normal tweet", is_retweet=False),
        ]
        create_and_store_tweets(tweets, test_db)

        # Check tweets
        response = client.post(
            "/tweets/check",
            json={"ids": ["500", "501", "502"], "mode": "partial_filter"},
        )
        assert response.status_code == 200
        statuses = response.get_json()

        # Retweet should be filtered immediately
        assert statuses["501"] == "filtered"

        # Normal tweets need LLM classification (no response yet)
        assert statuses["500"] == "pending"
        assert statuses["502"] == "pending"

        # Only the retweet should have a cached decision
        cached = get_mode_decisions_batch(
            ["500", "501", "502"], "partial_filter", test_db
        )

        assert "501" in cached
        assert cached["501"] == "filtered"

        # Normal tweets shouldn't have cached decisions yet (need LLM)
        assert "500" not in cached
        assert "502" not in cached
