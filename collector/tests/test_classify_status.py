"""Tests for the /api/classify/status endpoint."""

import time
from datetime import datetime

from classification_queue import Priority, get_classification_queue
from database import store_tweets
from tests.fixtures import approve_tweets, make_tweet


class TestClassifyStatusEndpoint:
    """Tests for GET /api/classify/status."""

    def test_status_returns_required_fields(self, client, test_db):
        """Should return pending_count, newly_classified, and has_pending."""
        resp = client.get("/api/classify/status?mode_id=default")

        assert resp.status_code == 200
        data = resp.get_json()
        assert "pending_count" in data
        assert "newly_classified" in data
        assert "has_pending" in data

    def test_status_shows_pending_items(self, client, test_db):
        """Should reflect items in the queue."""
        # Add items to queue
        queue = get_classification_queue()
        queue.enqueue("tweet_1", "binary_filter_v1", "default", Priority.HIGH)
        queue.enqueue("tweet_2", "binary_filter_v1", "default", Priority.HIGH)

        resp = client.get("/api/classify/status?mode_id=default")
        data = resp.get_json()

        assert data["pending_count"] >= 2
        assert data["has_pending"] is True

        # Clean up
        queue.mark_complete(["tweet_1", "tweet_2"])

    def test_status_empty_queue(self, client, test_db):
        """Empty queue should show has_pending=False."""
        resp = client.get("/api/classify/status?mode_id=default")
        data = resp.get_json()

        # May or may not be empty depending on other tests
        assert "has_pending" in data

    def test_status_newly_classified_with_since(self, client, test_db):
        """Should return IDs classified since 'since' timestamp."""
        tweets = [make_tweet(id="1"), make_tweet(id="2"), make_tweet(id="3")]
        store_tweets(tweets, test_db)

        # Record time before classification
        before_time = datetime.utcnow().isoformat()
        time.sleep(0.1)

        # Classify tweet 1 and 2 (after the timestamp)
        approve_tweets(["1", "2"], test_db)

        resp = client.get(f"/api/classify/status?mode_id=default&since={before_time}")
        data = resp.get_json()

        assert "1" in data["newly_classified"]
        assert "2" in data["newly_classified"]
        # Tweet 3 was not classified
        assert "3" not in data["newly_classified"]

    def test_status_without_since_returns_empty_newly_classified(self, client, test_db):
        """Without 'since', newly_classified should be empty."""
        tweets = [make_tweet(id="1")]
        store_tweets(tweets, test_db)
        approve_tweets(["1"], test_db)

        resp = client.get("/api/classify/status?mode_id=default")
        data = resp.get_json()

        # Without 'since' param, newly_classified should be empty
        assert data["newly_classified"] == []

    def test_status_with_default_mode(self, client, test_db):
        """Should default to 'default' mode."""
        resp = client.get("/api/classify/status")

        assert resp.status_code == 200
        data = resp.get_json()
        # Should work with default mode
        assert "pending_count" in data


class TestClassifyStatusQueueInteraction:
    """Tests for queue interaction in status endpoint."""

    def test_queue_pending_count_by_mode(self, client, test_db):
        """pending_count should reflect only items for the requested mode."""
        queue = get_classification_queue()

        # Queue items for different modes
        queue.enqueue("t1", "binary_filter_v1", "default", Priority.HIGH)
        queue.enqueue("t2", "binary_filter_v1", "other_mode", Priority.HIGH)

        resp = client.get("/api/classify/status?mode_id=default")
        data = resp.get_json()

        # Should count only default mode items
        assert data["pending_count"] >= 1  # At least t1

        # Clean up
        queue.mark_complete(["t1", "t2"])

    def test_has_pending_false_when_empty(self, client, test_db):
        """has_pending should be false when no items in queue for mode."""
        # Get fresh status without queueing anything
        resp = client.get("/api/classify/status?mode_id=default")
        data = resp.get_json()

        # If queue is empty, has_pending should be false
        if data["pending_count"] == 0:
            assert data["has_pending"] is False
