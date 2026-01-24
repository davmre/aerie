#!/usr/bin/env python3
"""
Headless Twitter collector using Camoufox.

This module handles browser automation for collecting tweets from Twitter
by intercepting GraphQL API responses.

Usage:
    python collector.py                    # Run with default config
    python collector.py --duration 120     # Scroll for 2 minutes
    python collector.py --headful          # Show browser (for debugging)
"""

import argparse
import json
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from tweet_parser import extract_tweets_and_retweets, is_timeline_endpoint


@dataclass
class CollectionStatus:
    """Shared status for server/collector communication."""

    running: bool = False
    tweets_captured: int = 0
    last_result: str | None = None
    error: str | None = None


@dataclass
class CollectionConfig:
    """Configuration for collection runs."""

    collector_url: str = "http://localhost:8080"
    cookies_file: str = "cookies.json"
    scroll_duration: int = 60
    server_port: int = 8081
    auth_username: str = ""
    auth_password: str = ""


def load_config(config_path: Path | None = None) -> CollectionConfig:
    """Load configuration from JSON file."""
    if config_path is None:
        config_path = Path(__file__).parent / "config.json"

    if not config_path.exists():
        print("[Config] No config.json found, using defaults")
        return CollectionConfig()

    with open(config_path) as f:
        data = json.load(f)

    return CollectionConfig(
        collector_url=data.get("collector_url", "http://localhost:8080"),
        cookies_file=data.get("cookies_file", "cookies.json"),
        scroll_duration=data.get("scroll_duration", 60),
        server_port=data.get("server_port", 8081),
        auth_username=data.get("auth", {}).get("username", ""),
        auth_password=data.get("auth", {}).get("password", ""),
    )


def load_cookies(cookies_file: str) -> list[dict[str, Any]]:
    """
    Load cookies from JSON file.

    The cookies file should be exported from a browser extension like
    "Cookie Editor" in the Netscape/JSON format.
    """
    cookies_path = Path(__file__).parent / cookies_file

    if not cookies_path.exists():
        raise FileNotFoundError(
            f"Cookies file not found: {cookies_path}\n"
            "Export your Twitter cookies from your browser and save them as cookies.json"
        )

    with open(cookies_path) as f:
        cookies = json.load(f)

    # Validate required cookies
    cookie_names = {c.get("name") for c in cookies}
    required = {"auth_token", "ct0"}
    missing = required - cookie_names
    if missing:
        raise ValueError(f"Missing required cookies: {missing}")

    # Convert to playwright cookie format
    playwright_cookies = []
    for cookie in cookies:
        pc = {
            "name": cookie.get("name"),
            "value": cookie.get("value"),
            "domain": cookie.get("domain", ".twitter.com"),
            "path": cookie.get("path", "/"),
        }
        # Add optional fields if present
        if cookie.get("secure") is not None:
            pc["secure"] = cookie["secure"]
        if cookie.get("httpOnly") is not None:
            pc["httpOnly"] = cookie["httpOnly"]
        if cookie.get("sameSite"):
            # Playwright expects capitalized sameSite values
            pc["sameSite"] = cookie["sameSite"].capitalize()

        playwright_cookies.append(pc)

    return playwright_cookies


@dataclass
class CollectedTweets:
    """Accumulator for intercepted tweets."""

    tweets: list[dict] = field(default_factory=list)
    retweets: list[dict] = field(default_factory=list)


def human_scroll(page: Any, duration: int, collected: CollectedTweets) -> None:
    """
    Scroll the page with human-like timing.

    Args:
        page: Playwright page object
        duration: Total scroll duration in seconds
        collected: CollectedTweets accumulator for tracking progress
    """
    start_time = time.time()
    last_count = 0
    stall_count = 0

    print(f"[Scroll] Starting {duration}s scroll session")

    while time.time() - start_time < duration:
        # Random scroll distance (300-800px)
        scroll_distance = random.randint(300, 800)

        # Scroll down
        page.evaluate(f"window.scrollBy(0, {scroll_distance})")

        # Random pause between scrolls (0.5-2.0s)
        pause = random.uniform(0.5, 2.0)

        # Occasionally take a longer pause (simulating reading)
        if random.random() < 0.15:
            pause = random.uniform(3.0, 6.0)

        time.sleep(pause)

        # Check if we're getting new content
        current_count = len(collected.tweets)
        if current_count == last_count:
            stall_count += 1
            if stall_count >= 5:
                print("[Scroll] No new tweets detected, scrolling more aggressively")
                # Try a larger scroll to trigger new content loading
                page.evaluate("window.scrollBy(0, 1500)")
                time.sleep(1.5)
                stall_count = 0
        else:
            stall_count = 0

        last_count = current_count

        # Progress update every ~10 seconds
        elapsed = time.time() - start_time
        if int(elapsed) % 10 == 0 and int(elapsed) > 0:
            print(f"[Scroll] {int(elapsed)}s elapsed, {current_count} tweets captured")

    print(f"[Scroll] Complete. {len(collected.tweets)} tweets, {len(collected.retweets)} retweets")


def create_route_handler(collected: CollectedTweets):
    """Create a Playwright route handler to intercept GraphQL responses."""

    def handle_route(route: Any) -> None:
        url = route.request.url

        # Let the request proceed and get the response
        response = route.fetch()

        # Only process timeline endpoints
        if is_timeline_endpoint(url):
            try:
                body = response.text()
                data = json.loads(body)
                tweets, retweets = extract_tweets_and_retweets(data)

                if tweets or retweets:
                    collected.tweets.extend(tweets)
                    collected.retweets.extend(retweets)
                    print(f"[Intercept] Captured {len(tweets)} tweets, {len(retweets)} retweets")

            except json.JSONDecodeError:
                pass  # Not JSON, ignore
            except Exception as e:
                print(f"[Intercept] Error processing response: {e}")

        # Continue with the original response
        route.fulfill(response=response)

    return handle_route


def send_to_collector(
    tweets: list[dict],
    retweets: list[dict],
    config: CollectionConfig,
) -> dict[str, Any]:
    """
    POST tweets to the main collector service.

    Returns the response from the collector.
    """
    if not tweets and not retweets:
        return {"status": "ok", "inserted": 0, "duplicates": 0}

    url = f"{config.collector_url}/tweets"
    auth = None
    if config.auth_username and config.auth_password:
        auth = (config.auth_username, config.auth_password)

    try:
        response = httpx.post(
            url,
            json={"tweets": tweets, "retweets": retweets},
            auth=auth,
            timeout=30.0,
        )
        response.raise_for_status()
        return response.json()

    except httpx.RequestError as e:
        print(f"[Collector] Request error: {e}")
        raise
    except httpx.HTTPStatusError as e:
        print(f"[Collector] HTTP error: {e.response.status_code} - {e.response.text}")
        raise


def run_collection(
    duration: int | None = None,
    status: CollectionStatus | None = None,
    headless: bool = True,
) -> int:
    """
    Run a collection session.

    Args:
        duration: Scroll duration in seconds (overrides config)
        status: Optional status object for tracking progress
        headless: Whether to run in headless mode

    Returns:
        Number of tweets captured
    """
    if status:
        status.running = True
        status.error = None
        status.tweets_captured = 0

    try:
        # Import camoufox here to avoid import errors if not installed
        from camoufox.sync_api import Camoufox

        config = load_config()
        cookies = load_cookies(config.cookies_file)
        scroll_duration = duration or config.scroll_duration

        collected = CollectedTweets()

        print(f"[Collection] Starting {'headless' if headless else 'headful'} browser")
        print(f"[Collection] Scroll duration: {scroll_duration}s")
        print(f"[Collection] Collector URL: {config.collector_url}")

        with Camoufox(headless=headless) as browser:
            context = browser.new_context()
            context.add_cookies(cookies)
            page = context.new_page()

            # Set up GraphQL interception
            page.route("**/graphql/**", create_route_handler(collected))

            # Navigate to Twitter home
            print("[Collection] Navigating to twitter.com/home")
            page.goto("https://twitter.com/home", wait_until="domcontentloaded")

            # Wait for initial content to load
            time.sleep(3)

            # Check if we're logged in by looking for timeline
            try:
                page.wait_for_selector('[data-testid="primaryColumn"]', timeout=10000)
                print("[Collection] Timeline loaded, starting scroll")
            except Exception:
                print("[Collection] Warning: Timeline selector not found, proceeding anyway")

            # Scroll to capture tweets
            human_scroll(page, scroll_duration, collected)

        # Deduplicate tweets by ID
        seen_ids: set[str] = set()
        unique_tweets = []
        for tweet in collected.tweets:
            if tweet["id"] not in seen_ids:
                seen_ids.add(tweet["id"])
                unique_tweets.append(tweet)

        # Deduplicate retweets
        seen_rts: set[str] = set()
        unique_retweets = []
        for rt in collected.retweets:
            key = f"{rt['original_tweet_id']}:{rt['retweeter_username']}"
            if key not in seen_rts:
                seen_rts.add(key)
                unique_retweets.append(rt)

        print(f"[Collection] Unique tweets: {len(unique_tweets)}")
        print(f"[Collection] Unique retweets: {len(unique_retweets)}")

        # Send to collector
        if unique_tweets or unique_retweets:
            print(f"[Collection] Sending to collector at {config.collector_url}")
            result = send_to_collector(unique_tweets, unique_retweets, config)
            print(
                f"[Collection] Collector response: {result.get('inserted', 0)} inserted, "
                f"{result.get('duplicates', 0)} duplicates"
            )

        if status:
            status.tweets_captured = len(unique_tweets)
            status.last_result = "success"

        return len(unique_tweets)

    except Exception as e:
        print(f"[Collection] Error: {e}")
        if status:
            status.error = str(e)
            status.last_result = "error"
        raise

    finally:
        if status:
            status.running = False


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Headless Twitter collector")
    parser.add_argument(
        "--duration",
        type=int,
        help="Scroll duration in seconds (default: from config or 60)",
    )
    parser.add_argument(
        "--headful",
        action="store_true",
        help="Show browser window (for debugging)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="Path to config.json",
    )

    args = parser.parse_args()

    try:
        tweet_count = run_collection(
            duration=args.duration,
            headless=not args.headful,
        )
        print(f"\nCollection complete: {tweet_count} tweets captured")

    except FileNotFoundError as e:
        print(f"\nError: {e}")
        return 1
    except Exception as e:
        print(f"\nCollection failed: {e}")
        return 1

    return 0


if __name__ == "__main__":
    exit(main())
