"""Tests for the classification queue."""

import time
import threading

import pytest

from classification_queue import (
    ClassificationQueue,
    ClassificationJob,
    Priority,
    get_classification_queue,
)


class TestClassificationQueue:
    """Tests for ClassificationQueue functionality."""

    def test_enqueue_adds_job(self):
        """Enqueueing a tweet adds it to the queue."""
        queue = ClassificationQueue()
        assert queue.enqueue("tweet1", "prompt1") is True
        assert queue.size == 1

    def test_enqueue_returns_false_for_duplicate(self):
        """Enqueueing the same tweet twice returns False."""
        queue = ClassificationQueue()
        assert queue.enqueue("tweet1", "prompt1") is True
        assert queue.enqueue("tweet1", "prompt1") is False
        assert queue.size == 1

    def test_priority_ordering(self):
        """HIGH priority jobs are returned before NORMAL priority."""
        queue = ClassificationQueue()

        # Add NORMAL priority first
        queue.enqueue("tweet1", "prompt1", Priority.NORMAL)
        time.sleep(0.01)  # Ensure different timestamps
        queue.enqueue("tweet2", "prompt1", Priority.HIGH)

        # Get batch - HIGH should come first
        jobs = queue.get_batch(max_size=2, timeout=0.1)
        assert len(jobs) == 2
        assert jobs[0].tweet_id == "tweet2"  # HIGH priority
        assert jobs[1].tweet_id == "tweet1"  # NORMAL priority

    def test_get_batch_respects_max_size(self):
        """get_batch returns at most max_size jobs."""
        queue = ClassificationQueue()
        for i in range(10):
            queue.enqueue(f"tweet{i}", "prompt1")

        jobs = queue.get_batch(max_size=3, timeout=0.1)
        assert len(jobs) == 3

    def test_get_batch_returns_empty_on_timeout(self):
        """get_batch returns empty list if queue is empty after timeout."""
        queue = ClassificationQueue()
        jobs = queue.get_batch(max_size=10, timeout=0.05)
        assert jobs == []

    def test_get_batch_returns_partial_on_timeout(self):
        """get_batch returns available jobs when batch doesn't fill."""
        queue = ClassificationQueue()
        queue.enqueue("tweet1", "prompt1")
        queue.enqueue("tweet2", "prompt1")

        jobs = queue.get_batch(max_size=10, timeout=0.1)
        assert len(jobs) == 2

    def test_mark_complete_removes_from_pending(self):
        """mark_complete allows re-enqueueing of completed tweets."""
        queue = ClassificationQueue()
        queue.enqueue("tweet1", "prompt1")

        # Can't enqueue again while pending
        assert queue.enqueue("tweet1", "prompt1") is False

        # Mark complete
        queue.mark_complete(["tweet1"])

        # Now can enqueue again
        assert queue.enqueue("tweet1", "prompt1") is True

    def test_is_pending(self):
        """is_pending returns True for queued tweets."""
        queue = ClassificationQueue()
        assert queue.is_pending("tweet1") is False

        queue.enqueue("tweet1", "prompt1")
        assert queue.is_pending("tweet1") is True

        queue.mark_complete(["tweet1"])
        assert queue.is_pending("tweet1") is False

    def test_enqueue_batch(self):
        """enqueue_batch adds multiple tweets at once."""
        queue = ClassificationQueue()
        added = queue.enqueue_batch(["t1", "t2", "t3"], "prompt1")
        assert added == 3
        assert queue.size == 3

    def test_enqueue_batch_skips_duplicates(self):
        """enqueue_batch skips already-queued tweets."""
        queue = ClassificationQueue()
        queue.enqueue("t1", "prompt1")

        added = queue.enqueue_batch(["t1", "t2", "t3"], "prompt1")
        assert added == 2  # t1 was already queued
        assert queue.size == 3

    def test_max_size_limit(self):
        """Queue respects max_size limit for NORMAL priority."""
        queue = ClassificationQueue(max_size=3)

        assert queue.enqueue("t1", "p1", Priority.NORMAL) is True
        assert queue.enqueue("t2", "p1", Priority.NORMAL) is True
        assert queue.enqueue("t3", "p1", Priority.NORMAL) is True
        assert queue.enqueue("t4", "p1", Priority.NORMAL) is False  # Queue full

        assert queue.size == 3
        assert queue.dropped_count == 1

    def test_high_priority_bypasses_max_size(self):
        """HIGH priority items can exceed max_size slightly."""
        queue = ClassificationQueue(max_size=2)

        queue.enqueue("t1", "p1", Priority.NORMAL)
        queue.enqueue("t2", "p1", Priority.NORMAL)
        # Queue is now "full" for NORMAL priority

        # HIGH priority should still be accepted
        assert queue.enqueue("t3", "p1", Priority.HIGH) is True
        assert queue.size == 3


class TestClassificationJob:
    """Tests for ClassificationJob dataclass."""

    def test_job_ordering_by_priority(self):
        """Jobs are ordered by priority first."""
        job_normal = ClassificationJob(
            priority=Priority.NORMAL,
            added_at=1.0,
            tweet_id="t1",
            prompt_id="p1",
        )
        job_high = ClassificationJob(
            priority=Priority.HIGH,
            added_at=2.0,  # Added later but higher priority
            tweet_id="t2",
            prompt_id="p1",
        )

        assert job_high < job_normal  # HIGH (1) < NORMAL (2)

    def test_job_ordering_by_time_within_priority(self):
        """Jobs with same priority are ordered by time."""
        job_early = ClassificationJob(
            priority=Priority.NORMAL,
            added_at=1.0,
            tweet_id="t1",
            prompt_id="p1",
        )
        job_late = ClassificationJob(
            priority=Priority.NORMAL,
            added_at=2.0,
            tweet_id="t2",
            prompt_id="p1",
        )

        assert job_early < job_late


class TestThreadSafety:
    """Tests for thread-safe queue operations."""

    def test_concurrent_enqueue(self):
        """Multiple threads can enqueue simultaneously."""
        queue = ClassificationQueue(max_size=1000)
        results = []

        def enqueue_range(start, count):
            added = 0
            for i in range(start, start + count):
                if queue.enqueue(f"tweet{i}", "prompt1"):
                    added += 1
            results.append(added)

        threads = [
            threading.Thread(target=enqueue_range, args=(i * 100, 100))
            for i in range(5)
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All 500 unique tweets should be added
        assert sum(results) == 500
        assert queue.size == 500

    def test_concurrent_enqueue_same_ids(self):
        """Concurrent enqueues of same IDs are deduplicated."""
        queue = ClassificationQueue()
        results = []

        def enqueue_same():
            added = 0
            for _ in range(10):
                if queue.enqueue("same_tweet", "prompt1"):
                    added += 1
            results.append(added)

        threads = [threading.Thread(target=enqueue_same) for _ in range(5)]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Only one thread should successfully enqueue
        assert sum(results) == 1
        assert queue.size == 1
