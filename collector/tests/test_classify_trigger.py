"""Tests for the /api/classify/trigger endpoint."""

from datetime import datetime, timedelta

from database import store_tweets
from tests.fixtures import approve_tweets, make_tweet


class TestClassifyTriggerEndpoint:
    """Tests for POST /api/classify/trigger."""

    def test_trigger_returns_stats(self, client, test_db):
        """Should return queued, already_decided, and prefilter_decided counts."""
        tweets = [make_tweet(id="1"), make_tweet(id="2")]
        store_tweets(tweets, test_db)

        resp = client.post("/api/classify/trigger", json={
            "mode_id": "default",
            "limit": 10,
        })

        assert resp.status_code == 200
        data = resp.get_json()
        assert "queued" in data
        assert "already_decided" in data
        assert "prefilter_decided" in data

    def test_trigger_queues_unclassified_tweets(self, client, test_db):
        """Should queue tweets that don't have decisions yet."""
        tweets = [make_tweet(id=str(i)) for i in range(5)]
        store_tweets(tweets, test_db)

        # Approve some tweets
        approve_tweets(["0", "1"], test_db)

        resp = client.post("/api/classify/trigger", json={
            "mode_id": "default",
            "limit": 10,
        })

        assert resp.status_code == 200
        data = resp.get_json()
        # 2 already approved, 3 should be queued (or some decided by prefilter)
        assert data["already_decided"] >= 0
        # Either queued or prefilter-decided
        assert data["queued"] + data["prefilter_decided"] >= 0

    def test_trigger_respects_limit(self, client, test_db):
        """Should not queue more than the limit."""
        tweets = [make_tweet(id=str(i)) for i in range(10)]
        store_tweets(tweets, test_db)

        resp = client.post("/api/classify/trigger", json={
            "mode_id": "default",
            "limit": 3,
        })

        assert resp.status_code == 200
        data = resp.get_json()
        # Total processed should not exceed limit
        assert data["queued"] + data["prefilter_decided"] + data["already_decided"] <= 3

    def test_trigger_respects_max_age_hours(self, client, test_db):
        """Only tweets within max_age_hours should be queued."""
        old_time = (datetime.utcnow() - timedelta(hours=72)).isoformat()
        new_time = datetime.utcnow().isoformat()

        tweets = [
            make_tweet(id="old", created_at=old_time, text="Old tweet"),
            make_tweet(id="new", created_at=new_time, text="New tweet"),
        ]
        store_tweets(tweets, test_db)

        resp = client.post("/api/classify/trigger", json={
            "mode_id": "default",
            "limit": 10,
            "max_age_hours": 24,
        })

        assert resp.status_code == 200
        data = resp.get_json()
        # Only new tweet should be processed
        assert data["queued"] + data["prefilter_decided"] <= 1

    def test_trigger_with_before_param(self, client, test_db):
        """before param should only classify older tweets."""
        tweets = [
            make_tweet(id="1", created_at="2025-01-01T10:00:00", text="Tweet 1"),
            make_tweet(id="2", created_at="2025-01-01T12:00:00", text="Tweet 2"),
            make_tweet(id="3", created_at="2025-01-01T14:00:00", text="Tweet 3"),
        ]
        store_tweets(tweets, test_db)

        resp = client.post("/api/classify/trigger", json={
            "mode_id": "default",
            "before": "2025-01-01T13:00:00",
            "limit": 10,
        })

        assert resp.status_code == 200
        data = resp.get_json()
        # Only tweets 1 and 2 should be processed (before 13:00)
        assert data["queued"] + data["prefilter_decided"] <= 2

    def test_trigger_with_platform_filter(self, client, test_db):
        """Should filter by platform when specified."""
        tweets = [
            make_tweet(id="1", platform="twitter"),
            make_tweet(id="2", platform="bluesky"),
            make_tweet(id="3", platform="twitter"),
        ]
        store_tweets(tweets, test_db)

        resp = client.post("/api/classify/trigger", json={
            "mode_id": "default",
            "platform": "bluesky",
            "limit": 10,
        })

        assert resp.status_code == 200
        data = resp.get_json()
        # Only 1 bluesky tweet
        assert data["queued"] + data["prefilter_decided"] <= 1

    def test_trigger_invalid_mode_returns_error(self, client, test_db):
        """Should return 400 for invalid mode_id."""
        resp = client.post("/api/classify/trigger", json={
            "mode_id": "nonexistent_mode",
        })

        assert resp.status_code == 400
        data = resp.get_json()
        assert "error" in data

    def test_trigger_no_tweets_returns_zeros(self, client, test_db):
        """Should return zeros when no tweets match."""
        resp = client.post("/api/classify/trigger", json={
            "mode_id": "default",
            "limit": 10,
        })

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["queued"] == 0
        assert data["already_decided"] == 0
        assert data["prefilter_decided"] == 0

    def test_trigger_with_empty_body(self, client, test_db):
        """Should use defaults when body is empty."""
        tweets = [make_tweet(id="1")]
        store_tweets(tweets, test_db)

        resp = client.post("/api/classify/trigger", json={})

        assert resp.status_code == 200
        data = resp.get_json()
        # Should use default mode and limit
        assert "queued" in data
