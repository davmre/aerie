"""
Classification queue for real-time tweet classification.

Provides a priority queue that batches tweets for efficient LLM classification.
HIGH priority tweets (actively being checked) are processed before NORMAL
priority tweets (newly captured).
"""

import queue
import threading
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Optional


class Priority(IntEnum):
    """Classification priority levels."""
    HIGH = 1    # Tweets being actively checked by extension (visible on screen)
    NORMAL = 2  # Newly captured tweets (not yet visible)


@dataclass(order=True)
class ClassificationJob:
    """A tweet classification job in the queue."""
    priority: Priority
    added_at: float = field(compare=True)
    tweet_id: str = field(compare=False)
    prompt_id: str = field(compare=False)


class ClassificationQueue:
    """
    Thread-safe priority queue for classification jobs.

    Features:
    - Priority ordering (HIGH before NORMAL)
    - Deduplication (same tweet won't be queued twice)
    - Batch collection with timeout
    - Thread-safe operations
    """

    def __init__(self, max_size: int = 1000):
        self._queue: queue.PriorityQueue = queue.PriorityQueue()
        self._pending: set[str] = set()  # Tweet IDs currently in queue
        self._lock = threading.Lock()
        self._max_size = max_size
        self._dropped_count = 0

    def enqueue(
        self,
        tweet_id: str,
        prompt_id: str,
        priority: Priority = Priority.NORMAL,
    ) -> bool:
        """
        Add a tweet to the classification queue.

        Returns True if added, False if already queued or queue is full.
        """
        with self._lock:
            # Skip if already queued
            if tweet_id in self._pending:
                return False

            # Check queue size
            if len(self._pending) >= self._max_size:
                # Drop oldest NORMAL priority items if we're adding HIGH priority
                if priority == Priority.HIGH:
                    self._drop_old_normal_items()
                else:
                    self._dropped_count += 1
                    return False

            job = ClassificationJob(
                priority=priority,
                added_at=time.time(),
                tweet_id=tweet_id,
                prompt_id=prompt_id,
            )
            self._queue.put(job)
            self._pending.add(tweet_id)
            return True

    def enqueue_batch(
        self,
        tweet_ids: list[str],
        prompt_id: str,
        priority: Priority = Priority.NORMAL,
    ) -> int:
        """
        Add multiple tweets to the queue.

        Returns the number of tweets actually added.
        """
        added = 0
        for tweet_id in tweet_ids:
            if self.enqueue(tweet_id, prompt_id, priority):
                added += 1
        return added

    def get_batch(
        self,
        max_size: int = 10,
        timeout: float = 0.2,
    ) -> list[ClassificationJob]:
        """
        Get a batch of jobs from the queue.

        Waits up to `timeout` seconds for the batch to fill, but returns
        early if we have `max_size` jobs or if the queue becomes empty
        after getting at least one job.

        Returns an empty list if no jobs are available within timeout.
        """
        jobs = []
        deadline = time.time() + timeout

        while len(jobs) < max_size:
            remaining = deadline - time.time()
            if remaining <= 0:
                break

            try:
                # Wait for a job, but not longer than remaining timeout
                job = self._queue.get(timeout=min(remaining, 0.05))
                jobs.append(job)
            except queue.Empty:
                # If we have some jobs, return them; otherwise keep waiting
                if jobs:
                    break

        return jobs

    def mark_complete(self, tweet_ids: list[str]) -> None:
        """Mark tweets as no longer pending classification."""
        with self._lock:
            for tweet_id in tweet_ids:
                self._pending.discard(tweet_id)

    def is_pending(self, tweet_id: str) -> bool:
        """Check if a tweet is currently in the queue."""
        with self._lock:
            return tweet_id in self._pending

    @property
    def size(self) -> int:
        """Current number of items in the queue."""
        with self._lock:
            return len(self._pending)

    @property
    def dropped_count(self) -> int:
        """Number of items dropped due to queue overflow."""
        return self._dropped_count

    def _drop_old_normal_items(self, max_age: float = 300.0) -> int:
        """
        Drop NORMAL priority items older than max_age seconds.
        Called when queue is full and we need to add HIGH priority items.

        Returns number of items dropped.
        """
        # This is a simplified implementation - we can't efficiently
        # remove items from the middle of a PriorityQueue.
        # For now, we just allow the queue to grow slightly over max_size
        # for HIGH priority items.
        return 0


# Global queue instance
_queue: Optional[ClassificationQueue] = None
_queue_lock = threading.Lock()


def get_classification_queue() -> ClassificationQueue:
    """Get the global classification queue instance."""
    global _queue
    with _queue_lock:
        if _queue is None:
            _queue = ClassificationQueue()
        return _queue
