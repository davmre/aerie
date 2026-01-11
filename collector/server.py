#!/usr/bin/env python3
"""
Aerie Tweet Collector Server

A simple Flask server that receives tweets from the browser extension
and stores them in SQLite for later classification.
"""

import json
import threading
import time
from pathlib import Path

from flask import Flask, current_app, jsonify, render_template, request

from batch_classifier import classify_and_store_batch
from classification_queue import Priority, get_classification_queue
from classifier import format_tweet_for_classification, setup_prompts_and_modes
from database import (
    DEFAULT_DB_PATH,
    add_human_label,
    compute_single_chain,
    count_modes_using_prompt,
    create_mode,
    create_prompt,
    delete_mode,
    delete_prompt,
    get_conversation_roots,
    get_mode,
    get_mode_decisions_batch,
    get_mode_label_counts,
    get_prompt,
    get_prompt_responses_batch,
    get_replies_for_tweet,
    get_retweets_batch,
    get_stats,
    get_thread_context_batch,
    get_tweet,
    get_tweets_batch,
    invalidate_mode_decisions,
    list_modes,
    list_prompts,
    store_retweets,
    store_tweets,
    transaction,
    update_mode,
    update_prompt,
)
from extractors import EXTRACTOR_SCHEMAS, list_extractor_schemas
from modes import (
    compute_mode_decisions,
    compute_mode_decisions_for_tweets,
    decide_tweets_batch,
    get_available_modes,
    get_mode_stats,
    get_mode_status_for_all_tweets,
)
from prefilters import PREFILTER_SCHEMAS, list_prefilter_schemas


def get_db_path() -> Path:
    """Get database path from current app config."""
    return Path(current_app.config["DATABASE"])


# =============================================================================
# Classification Worker Configuration
# =============================================================================

DEFAULT_CLASSIFICATION_CONFIG = {
    "enabled": True,
    "batch_size": 10,
    "batch_timeout": 0.2,  # seconds to wait for batch to fill
    "provider": "anthropic",  # LLM provider (anthropic, gemini)
    "model": None,  # Model to use (None = provider's default)
    "default_prompt_id": "binary_filter_v1",
}

_worker_thread: threading.Thread | None = None
_worker_lock = threading.Lock()


def start_classification_worker(db_path: Path, config: dict):
    """Start the background classification worker thread."""
    global _worker_thread

    with _worker_lock:
        if _worker_thread is not None and _worker_thread.is_alive():
            return  # Already running

        if not config.get("enabled", True):
            print("[Worker] Classification worker disabled")
            return

        # Check for API key based on configured provider
        from providers import get_default_provider, get_provider

        provider_name = config.get("provider") or get_default_provider()
        try:
            provider = get_provider(provider_name)
            has_api_key = bool(provider.get_api_key())
            if not has_api_key:
                print(
                    f"[Worker] No {provider.config.api_key_env_var}, LLM classification disabled (prefilters still work)"
                )
        except ValueError as e:
            print(f"[Worker] Invalid provider: {e}")
            has_api_key = False

        def worker_loop():
            print("[Worker] Classification worker started")
            queue = get_classification_queue()

            while True:
                try:
                    # Get a batch of jobs (waits up to batch_timeout for more)
                    jobs = queue.get_batch(
                        max_size=config["batch_size"],
                        timeout=config["batch_timeout"],
                    )

                    if not jobs:
                        # No jobs available, sleep briefly and retry
                        time.sleep(0.1)
                        continue

                    # Collect all tweet IDs and mode IDs from jobs
                    all_tweet_ids = [j.tweet_id for j in jobs]
                    mode_ids = list(set(j.mode_id for j in jobs))

                    # If we have an API key, do LLM classification
                    if has_api_key:
                        # Group jobs by mode_id to respect each mode's provider/model settings
                        by_mode: dict[str, list] = {}
                        for job in jobs:
                            if job.mode_id not in by_mode:
                                by_mode[job.mode_id] = []
                            by_mode[job.mode_id].append(job)

                        for mode_id, mode_jobs in by_mode.items():
                            # Get mode settings for provider/model
                            mode = get_mode(mode_id, db_path)
                            if not mode:
                                continue

                            prompt_id = mode["prompt_id"]
                            mode_provider = mode.get("provider") or config.get("provider")
                            mode_model = mode.get("model_name") or config.get("model")

                            tweet_ids = [j.tweet_id for j in mode_jobs]

                            # Get tweet data
                            tweets = get_tweets_batch(tweet_ids, db_path)
                            tweets_list = [tweets[tid] for tid in tweet_ids if tid in tweets]

                            if not tweets_list:
                                continue

                            # Classify the batch
                            model_info = f" ({mode_model})" if mode_model else ""
                            print(
                                f"[Worker] Classifying {len(tweets_list)} tweets for {mode_id}{model_info}"
                            )
                            results = classify_and_store_batch(
                                tweets_list,
                                prompt_id,
                                provider_name=mode_provider,
                                model=mode_model,
                                db_path=db_path,
                            )

                            # Log results
                            success = sum(1 for r in results.values() if "_error" not in r)
                            errors = len(results) - success
                            if errors > 0:
                                print(f"[Worker] Classified: {success} success, {errors} errors")

                    # Update mode decisions for classified tweets (only for requested modes)
                    # This also handles prefilter-only modes even without API key
                    if all_tweet_ids:
                        compute_mode_decisions_for_tweets(
                            all_tweet_ids, mode_ids=mode_ids, db_path=db_path
                        )

                    # Mark jobs as complete
                    queue.mark_complete([j.tweet_id for j in jobs])

                except Exception as e:
                    print(f"[Worker] Error: {e}")
                    time.sleep(1)  # Backoff on error

        _worker_thread = threading.Thread(target=worker_loop, daemon=True)
        _worker_thread.start()


# =============================================================================
# App Factory
# =============================================================================


def create_app(config=None):
    """
    Create and configure the Flask application.

    Args:
        config: Optional dict with configuration overrides. Keys:
            - DATABASE: Path to SQLite database (default: DEFAULT_DB_PATH)
            - TESTING: Set to True for test mode (disables worker)
            - CLASSIFICATION_ENABLED: Enable/disable classification worker
    """
    app = Flask(__name__)

    # Default configuration
    app.config["DATABASE"] = DEFAULT_DB_PATH
    app.config["TESTING"] = False
    app.config["CLASSIFICATION_CONFIG"] = DEFAULT_CLASSIFICATION_CONFIG.copy()

    # Override with provided config
    if config:
        app.config.update(config)

    # Merge classification config if provided separately
    if config and "CLASSIFICATION_ENABLED" in config:
        app.config["CLASSIFICATION_CONFIG"]["enabled"] = config["CLASSIFICATION_ENABLED"]

    @app.before_request
    def ensure_db():
        """Initialize database and start worker on first request."""
        if not hasattr(app, "_db_initialized"):
            db_path = Path(app.config["DATABASE"])
            setup_prompts_and_modes(db_path)
            if not app.config["TESTING"]:
                start_classification_worker(db_path, app.config["CLASSIFICATION_CONFIG"])
            app._db_initialized = True  # type: ignore[attr-defined]

    @app.after_request
    def add_cors_headers(response):
        """Add CORS headers to allow requests from browser extensions."""
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return response

    @app.route("/tweets", methods=["POST", "OPTIONS"])
    def receive_tweets():
        """
        Receive tweets from the browser extension.
        Expects JSON body: {"tweets": [...], "retweets": [...]}
        """
        # Handle CORS preflight
        if request.method == "OPTIONS":
            return "", 204

        data = request.get_json()
        if not data or "tweets" not in data:
            return jsonify({"error": "Missing 'tweets' field"}), 400

        tweets = data["tweets"]
        if not isinstance(tweets, list):
            return jsonify({"error": "'tweets' must be an array"}), 400

        db_path = get_db_path()
        result = store_tweets(tweets, db_path)

        # Also store retweets if provided
        retweets = data.get("retweets", [])
        rt_result = {"inserted": 0, "duplicates": 0}
        if retweets:
            rt_result = store_retweets(retweets, db_path)

        # Note: We don't queue tweets here. Classification happens on-demand
        # when the extension calls /tweets/check with a specific mode.
        # This avoids expensive LLM calls for modes that aren't being used.

        return jsonify(
            {
                "status": "ok",
                "received": len(tweets),
                "inserted": result["inserted"],
                "duplicates": result["duplicates"],
                "retweets_received": len(retweets),
                "retweets_inserted": rt_result["inserted"],
            }
        )

    @app.route("/stats", methods=["GET"])
    def stats():
        """
        Get database statistics.
        Query param: mode (optional) - mode ID for mode-specific stats
        """
        db_path = get_db_path()
        mode_id = request.args.get("mode")
        if mode_id:
            try:
                return jsonify(get_mode_stats(mode_id, db_path=db_path))
            except KeyError as e:
                return jsonify({"error": str(e)}), 400
        # Fallback to legacy stats for backwards compatibility
        return jsonify(get_stats(db_path=db_path))

    @app.route("/tweets/check", methods=["POST", "OPTIONS"])
    def check_tweets():
        """
        Check the approval status of multiple tweets.
        Expects JSON body: {"ids": ["123", "456", ...], "mode": "mode_id"}
        Returns: {"123": "approved", "456": "pending", ...}

        Prefilter decisions are computed synchronously (instant for prefilter-only modes).
        Tweets needing LLM classification are queued with HIGH priority.
        """
        if request.method == "OPTIONS":
            return "", 204

        data = request.get_json()
        if not data or "ids" not in data:
            return jsonify({"error": "Missing 'ids' field"}), 400

        ids = data["ids"]
        if not isinstance(ids, list):
            return jsonify({"error": "'ids' must be an array"}), 400

        mode_id = data.get("mode", "default")
        db_path = get_db_path()

        try:
            # First pass: get current statuses (applies prefilters but doesn't persist)
            statuses = decide_tweets_batch(ids, mode_id, db_path=db_path)
        except KeyError as e:
            return jsonify({"error": str(e)}), 400

        # Check which tweets already have cached decisions
        cached_decisions = get_mode_decisions_batch(ids, mode_id, db_path)

        # Find tweets that need decisions computed/cached
        # This includes:
        # - tweets with pending status (need LLM or prefilter)
        # - tweets with prefilter decisions but no cached entry yet
        uncached_ids = [
            tid for tid in ids if tid not in cached_decisions and statuses.get(tid) != "unknown"
        ]

        if uncached_ids:
            # Compute and persist decisions for this mode
            # This handles prefilter-only modes instantly
            computed = compute_mode_decisions_for_tweets(
                uncached_ids, mode_ids=[mode_id], db_path=db_path
            )

            # Update statuses with newly computed decisions
            for tid in uncached_ids:
                if tid in computed and mode_id in computed[tid]:
                    statuses[tid] = computed[tid][mode_id]

            # Queue remaining pending tweets for LLM classification
            still_pending = [tid for tid in uncached_ids if statuses.get(tid) == "pending"]
            if still_pending:
                mode = get_mode(mode_id, db_path)
                prompt_id = (
                    mode["prompt_id"]
                    if mode
                    else current_app.config["CLASSIFICATION_CONFIG"]["default_prompt_id"]
                )
                queue = get_classification_queue()
                queue.enqueue_batch(still_pending, prompt_id, mode_id, Priority.HIGH)

        return jsonify(statuses)

    @app.route("/tweets/classified-ids", methods=["GET"])
    def classified_ids():
        """
        Get all classified tweet IDs for cache pre-population.
        Query param: mode (defaults to "default")
        """
        mode_id = request.args.get("mode", "default")
        db_path = get_db_path()

        try:
            ids = get_mode_status_for_all_tweets(mode_id, db_path=db_path)
        except KeyError as e:
            return jsonify({"error": str(e)}), 400

        return jsonify(ids)

    @app.route("/modes", methods=["GET"])
    def list_modes_endpoint():
        """List all available filtering modes."""
        db_path = get_db_path()
        modes = get_available_modes(db_path)
        return jsonify({"modes": modes})

    @app.route("/health", methods=["GET"])
    def health():
        """Health check endpoint."""
        return jsonify({"status": "ok"})

    # =========================================================================
    # Modes Management API
    # =========================================================================

    def _parse_mode_config(mode: dict) -> dict:
        """Parse JSON config fields in a mode record."""
        mode = dict(mode)  # Copy to avoid mutating original
        if mode.get("extractor_config"):
            try:
                mode["extractor_config"] = json.loads(mode["extractor_config"])
            except (json.JSONDecodeError, TypeError):
                pass
        if mode.get("prefilter_config"):
            try:
                mode["prefilter_config"] = json.loads(mode["prefilter_config"])
            except (json.JSONDecodeError, TypeError):
                pass
        return mode

    @app.route("/api/modes", methods=["GET"])
    def api_list_modes():
        """List all modes with metadata."""
        db_path = get_db_path()
        modes = list_modes(db_path)
        label_counts = get_mode_label_counts(db_path)

        result = []
        for mode in modes:
            mode = _parse_mode_config(mode)
            mode["human_label_count"] = label_counts.get(mode["id"], 0)
            mode["is_protected"] = mode["id"] == "default"
            result.append(mode)

        return jsonify({"modes": result})

    @app.route("/api/modes/<mode_id>", methods=["GET"])
    def api_get_mode(mode_id):
        """Get a single mode."""
        db_path = get_db_path()
        mode = get_mode(mode_id, db_path)
        if not mode:
            return jsonify({"error": "Mode not found"}), 404

        mode = _parse_mode_config(mode)
        label_counts = get_mode_label_counts(db_path)
        mode["human_label_count"] = label_counts.get(mode_id, 0)
        mode["is_protected"] = mode_id == "default"

        return jsonify(mode)

    @app.route("/api/modes", methods=["POST"])
    def api_create_mode():
        """Create a new mode."""
        db_path = get_db_path()
        data = request.get_json()
        if not data:
            return jsonify({"error": "Missing JSON body"}), 400

        # Validate required fields
        required = ["id", "name", "prompt_id", "extractor"]
        for field in required:
            if not data.get(field):
                return jsonify({"error": f"Missing required field: {field}"}), 400

        # Validate ID format
        mode_id = data["id"]
        if not mode_id.replace("_", "").isalnum():
            return jsonify({"error": "ID must contain only letters, numbers, and underscores"}), 400

        # Check if mode already exists
        if get_mode(mode_id, db_path):
            return jsonify({"error": f"Mode already exists: {mode_id}"}), 409

        # Validate prompt exists
        if not get_prompt(data["prompt_id"], db_path):
            return jsonify({"error": f"Prompt not found: {data['prompt_id']}"}), 400

        # Validate extractor exists
        if data["extractor"] not in EXTRACTOR_SCHEMAS:
            return jsonify({"error": f"Unknown extractor: {data['extractor']}"}), 400

        # Validate prefilter if provided
        if data.get("prefilter") and data["prefilter"] not in PREFILTER_SCHEMAS:
            return jsonify({"error": f"Unknown prefilter: {data['prefilter']}"}), 400

        # Validate provider if provided
        provider = data.get("provider", "anthropic")
        from providers import PROVIDERS

        if provider not in PROVIDERS:
            return jsonify(
                {"error": f"Unknown provider: {provider}. Available: {', '.join(PROVIDERS.keys())}"}
            ), 400

        # Create mode
        create_mode(
            mode_id=mode_id,
            name=data["name"],
            prompt_id=data["prompt_id"],
            extractor=data["extractor"],
            prefilter=data.get("prefilter"),
            extractor_config=data.get("extractor_config"),
            prefilter_config=data.get("prefilter_config"),
            description=data.get("description"),
            provider=provider,
            model_name=data.get("model_name"),
            db_path=db_path,
        )

        # Compute cached decisions for the new mode
        # This is important for prefilter-only modes which can decide without LLM
        compute_mode_decisions(mode_id, db_path=db_path)

        # Return the created mode
        mode = get_mode(mode_id, db_path)
        assert mode is not None  # We just created it
        return jsonify({"status": "ok", "mode": _parse_mode_config(mode)})

    @app.route("/api/modes/<mode_id>", methods=["PUT"])
    def api_update_mode(mode_id):
        """Update an existing mode."""
        db_path = get_db_path()
        data = request.get_json()
        if not data:
            return jsonify({"error": "Missing JSON body"}), 400

        if not get_mode(mode_id, db_path):
            return jsonify({"error": "Mode not found"}), 404

        # Validate prompt if changing
        if data.get("prompt_id") and not get_prompt(data["prompt_id"], db_path):
            return jsonify({"error": f"Prompt not found: {data['prompt_id']}"}), 400

        # Validate extractor if changing
        if data.get("extractor") and data["extractor"] not in EXTRACTOR_SCHEMAS:
            return jsonify({"error": f"Unknown extractor: {data['extractor']}"}), 400

        # Validate prefilter if changing
        if data.get("prefilter") and data["prefilter"] not in PREFILTER_SCHEMAS:
            return jsonify({"error": f"Unknown prefilter: {data['prefilter']}"}), 400

        # Validate provider if changing
        if data.get("provider"):
            from providers import PROVIDERS

            if data["provider"] not in PROVIDERS:
                return jsonify(
                    {
                        "error": f"Unknown provider: {data['provider']}. Available: {', '.join(PROVIDERS.keys())}"
                    }
                ), 400

        # Determine what to clear vs update
        # If prefilter is explicitly null/empty, clear it
        clear_prefilter = "prefilter" in data and not data.get("prefilter")
        # If config is explicitly null or {}, clear it
        clear_extractor_config = "extractor_config" in data and not data.get("extractor_config")
        clear_prefilter_config = "prefilter_config" in data and not data.get("prefilter_config")
        # If model_name is explicitly null/empty, clear it
        clear_model_name = "model_name" in data and not data.get("model_name")

        update_mode(
            mode_id=mode_id,
            name=data.get("name"),
            prompt_id=data.get("prompt_id"),
            extractor=data.get("extractor"),
            prefilter=data.get("prefilter") if not clear_prefilter else None,
            extractor_config=data.get("extractor_config") if not clear_extractor_config else None,
            prefilter_config=data.get("prefilter_config") if not clear_prefilter_config else None,
            description=data.get("description"),
            provider=data.get("provider"),
            model_name=data.get("model_name") if not clear_model_name else None,
            clear_prefilter=clear_prefilter,
            clear_extractor_config=clear_extractor_config,
            clear_prefilter_config=clear_prefilter_config,
            clear_model_name=clear_model_name,
            db_path=db_path,
        )

        # Invalidate and recompute cached decisions if config changed
        # Note: provider/model_name changes don't require recomputing cached decisions
        # since they only affect future LLM calls, not the cached response interpretations
        config_changed = any(
            [
                data.get("prompt_id"),
                data.get("extractor"),
                "prefilter" in data,
                "extractor_config" in data,
                "prefilter_config" in data,
            ]
        )
        if config_changed:
            invalidate_mode_decisions(mode_id, db_path)
            compute_mode_decisions(mode_id, db_path=db_path)

        # Return updated mode
        mode = get_mode(mode_id, db_path)
        assert mode is not None  # We just updated it
        return jsonify({"status": "ok", "mode": _parse_mode_config(mode)})

    @app.route("/api/modes/<mode_id>", methods=["DELETE"])
    def api_delete_mode(mode_id):
        """Delete a mode."""
        db_path = get_db_path()
        if mode_id == "default":
            return jsonify({"error": "Cannot delete the default mode"}), 400

        if not get_mode(mode_id, db_path):
            return jsonify({"error": "Mode not found"}), 404

        # Invalidate cached decisions before deleting the mode
        invalidate_mode_decisions(mode_id, db_path)

        force = request.args.get("force", "").lower() == "true"
        result = delete_mode(mode_id, force=force, db_path=db_path)

        if "error" in result:
            return jsonify(result), 409

        return jsonify(result)

    @app.route("/api/extractors", methods=["GET"])
    def api_list_extractors():
        """List available extractors with their schemas."""
        return jsonify({"extractors": list_extractor_schemas()})

    @app.route("/api/prefilters", methods=["GET"])
    def api_list_prefilters():
        """List available prefilters with their schemas."""
        return jsonify({"prefilters": list_prefilter_schemas()})

    # =========================================================================
    # Prompts Management API
    # =========================================================================

    @app.route("/api/prompts", methods=["GET"])
    def api_list_prompts():
        """List all prompts with full details and mode usage counts."""
        db_path = get_db_path()
        prompts = list_prompts(db_path)

        result = []
        for prompt in prompts:
            prompt_data = dict(prompt)
            prompt_data["mode_count"] = count_modes_using_prompt(prompt["id"], db_path)
            result.append(prompt_data)

        return jsonify({"prompts": result})

    @app.route("/api/prompts/<prompt_id>", methods=["GET"])
    def api_get_prompt(prompt_id):
        """Get a single prompt with full details."""
        db_path = get_db_path()
        prompt = get_prompt(prompt_id, db_path)
        if not prompt:
            return jsonify({"error": "Prompt not found"}), 404

        prompt_data = dict(prompt)
        prompt_data["mode_count"] = count_modes_using_prompt(prompt_id, db_path)

        return jsonify(prompt_data)

    @app.route("/api/prompts", methods=["POST"])
    def api_create_prompt():
        """Create a new prompt."""
        db_path = get_db_path()
        data = request.get_json()
        if not data:
            return jsonify({"error": "Missing JSON body"}), 400

        # Validate required fields
        if not data.get("id"):
            return jsonify({"error": "Missing required field: id"}), 400
        if not data.get("prompt_text"):
            return jsonify({"error": "Missing required field: prompt_text"}), 400

        # Validate ID format
        prompt_id = data["id"]
        if not prompt_id.replace("_", "").isalnum():
            return jsonify({"error": "ID must contain only letters, numbers, and underscores"}), 400

        # Check if prompt already exists
        if get_prompt(prompt_id, db_path):
            return jsonify({"error": f"Prompt already exists: {prompt_id}"}), 409

        # Create prompt
        create_prompt(
            prompt_id=prompt_id,
            prompt_text=data["prompt_text"],
            response_schema=data.get("response_schema"),
            db_path=db_path,
        )

        # Return the created prompt
        prompt = get_prompt(prompt_id, db_path)
        assert prompt is not None  # We just created it
        prompt_data = dict(prompt)
        prompt_data["mode_count"] = 0
        return jsonify({"status": "ok", "prompt": prompt_data})

    @app.route("/api/prompts/<prompt_id>", methods=["PUT"])
    def api_update_prompt(prompt_id):
        """Update an existing prompt."""
        db_path = get_db_path()
        data = request.get_json()
        if not data:
            return jsonify({"error": "Missing JSON body"}), 400

        if not get_prompt(prompt_id, db_path):
            return jsonify({"error": "Prompt not found"}), 404

        # Determine what to clear vs update
        clear_response_schema = "response_schema" in data and not data.get("response_schema")

        update_prompt(
            prompt_id=prompt_id,
            prompt_text=data.get("prompt_text"),
            response_schema=data.get("response_schema") if not clear_response_schema else None,
            clear_response_schema=clear_response_schema,
            db_path=db_path,
        )

        # Return updated prompt
        prompt = get_prompt(prompt_id, db_path)
        assert prompt is not None  # We just updated it
        prompt_data = dict(prompt)
        prompt_data["mode_count"] = count_modes_using_prompt(prompt_id, db_path)
        return jsonify({"status": "ok", "prompt": prompt_data})

    @app.route("/api/prompts/<prompt_id>", methods=["DELETE"])
    def api_delete_prompt(prompt_id):
        """Delete a prompt."""
        db_path = get_db_path()

        if not get_prompt(prompt_id, db_path):
            return jsonify({"error": "Prompt not found"}), 404

        force = request.args.get("force", "").lower() == "true"
        result = delete_prompt(prompt_id, force=force, db_path=db_path)

        if "error" in result:
            return jsonify(result), 409

        return jsonify(result)

    @app.route("/api/providers", methods=["GET"])
    def api_list_providers():
        """List available LLM providers."""
        from providers import list_providers

        return jsonify({"providers": list_providers()})

    # =========================================================================
    # Web UI Routes
    # =========================================================================

    @app.route("/ui/label")
    def ui_label():
        """Labeling interface for tweets."""
        db_path = get_db_path()
        modes = get_available_modes(db_path)
        return render_template("label.html", modes=modes, active_page="label")

    @app.route("/ui/read")
    def ui_read():
        """Reading interface for approved tweets."""
        db_path = get_db_path()
        modes = get_available_modes(db_path)
        return render_template("read.html", modes=modes, active_page="read")

    @app.route("/ui/modes")
    def ui_modes():
        """Modes management interface."""
        db_path = get_db_path()
        modes = get_available_modes(db_path)
        prompts = list_prompts(db_path)
        return render_template("modes.html", modes=modes, prompts=prompts, active_page="modes")

    @app.route("/ui/prompts")
    def ui_prompts():
        """Prompts management interface."""
        db_path = get_db_path()
        modes = get_available_modes(db_path)
        return render_template("prompts.html", modes=modes, active_page="prompts")

    @app.route("/api/ui/tweets", methods=["GET"])
    def api_ui_tweets():
        """
        Get tweets for the UI with mode-based status and responses.

        Query params:
        - mode: Mode ID for status calculation
        - status: Filter by status (all, pending, approved, filtered, unlabeled)
        - search: Search in author or text
        - sort: Sort field (created_at, captured_at)
        - limit: Number of tweets
        - offset: Pagination offset
        - leaf_only: If "true", only show leaf tweets (no approved replies to them)
        """
        db_path = get_db_path()
        mode_id = request.args.get("mode", "default")
        status_filter = request.args.get("status", "all")
        search = request.args.get("search", "")
        sort_field = request.args.get("sort", "captured_at")
        limit = request.args.get("limit", 20, type=int)
        offset = request.args.get("offset", 0, type=int)
        leaf_only = request.args.get("leaf_only", "").lower() == "true"

        # Build query using cached mode_decisions table
        with transaction(db_path) as conn:
            params: list[str | int] = []
            if status_filter == "unlabeled":
                # Unlabeled by human - use SQL
                query = """
                    SELECT t.* FROM tweets t
                    LEFT JOIN human_labels hl ON t.id = hl.tweet_id AND hl.mode_id = ?
                    WHERE hl.tweet_id IS NULL
                """
                params = [mode_id]
            elif status_filter in ("approved", "filtered"):
                # Filter by cached mode decision using JOIN
                query = """
                    SELECT t.* FROM tweets t
                    JOIN mode_decisions md ON t.id = md.tweet_id
                    WHERE md.mode_id = ? AND md.decision = ?
                """
                params = [mode_id, status_filter]
            elif status_filter == "pending":
                # Pending = no cached decision for this mode
                query = """
                    SELECT t.* FROM tweets t
                    LEFT JOIN mode_decisions md ON t.id = md.tweet_id AND md.mode_id = ?
                    WHERE md.tweet_id IS NULL
                """
                params = [mode_id]
            else:
                # All tweets
                query = "SELECT * FROM tweets t"

            # Search filter
            if search:
                if "WHERE" in query:
                    query += " AND (t.author_username LIKE ? OR t.text LIKE ?)"
                else:
                    query += " WHERE (t.author_username LIKE ? OR t.text LIKE ?)"
                params.extend([f"%{search}%", f"%{search}%"])

            # Leaf-only filter: exclude tweets that have approved children or are quoted without context
            # Uses an optimized JOIN-based approach instead of correlated subqueries for performance
            if leaf_only:
                # Wrap the existing query and join with pre-computed parent/quoted sets
                query = f"""
                    WITH base_tweets AS ({query}),
                    replied_parents AS (
                        SELECT DISTINCT reply_to_tweet_id as parent_id
                        FROM tweets child
                        JOIN mode_decisions md_child ON child.id = md_child.tweet_id
                        WHERE md_child.mode_id = ? AND md_child.decision = 'approved'
                        AND reply_to_tweet_id IS NOT NULL
                    ),
                    quoted_originals AS (
                        SELECT DISTINCT quoted_tweet_id as quoted_id
                        FROM tweets quoter
                        JOIN mode_decisions md_quoter ON quoter.id = md_quoter.tweet_id
                        WHERE md_quoter.mode_id = ? AND md_quoter.decision = 'approved'
                        AND quoted_tweet_id IS NOT NULL
                    )
                    SELECT t.* FROM base_tweets t
                    LEFT JOIN replied_parents rp ON t.id = rp.parent_id
                    LEFT JOIN quoted_originals qo ON t.id = qo.quoted_id
                    WHERE rp.parent_id IS NULL
                    AND (t.reply_to_tweet_id IS NOT NULL OR qo.quoted_id IS NULL)
                """
                params.extend([mode_id, mode_id])

            # Sort
            if sort_field in ("created_at", "captured_at"):
                query += f" ORDER BY t.{sort_field} DESC"
            else:
                query += " ORDER BY t.captured_at DESC"

            # Get total count first
            # For CTE queries (leaf_only), wrap in subquery; otherwise use simple replacement
            if leaf_only:
                # For CTE queries, wrap the whole query (minus ORDER BY) in a subquery
                base_query = query.split(" ORDER BY")[0]
                count_query = f"SELECT COUNT(*) FROM ({base_query}) AS count_subq"
            else:
                count_query = query.split(" ORDER BY")[0]
                count_query = count_query.replace("SELECT t.*", "SELECT COUNT(*)", 1)
                count_query = count_query.replace("SELECT *", "SELECT COUNT(*)", 1)
            total = conn.execute(count_query, params).fetchone()[0]

            # Add pagination
            query += " LIMIT ? OFFSET ?"
            params.extend([limit, offset])

            rows = conn.execute(query, params).fetchall()
            tweets = [dict(row) for row in rows]

        if not tweets:
            return jsonify({"tweets": [], "total": 0})

        tweet_ids = [t["id"] for t in tweets]

        # Get cached mode decisions for the current mode
        mode_statuses = get_mode_decisions_batch(tweet_ids, mode_id, db_path)

        # Get cached decisions from all modes for display
        all_modes = get_available_modes(db_path)
        all_mode_decisions = {}
        for mode in all_modes:
            decisions = get_mode_decisions_batch(tweet_ids, mode["id"], db_path)
            for tid, status in decisions.items():
                if tid not in all_mode_decisions:
                    all_mode_decisions[tid] = {}
                all_mode_decisions[tid][mode["id"]] = status

        # Get prompt responses
        responses_by_tweet = {}
        seen_prompts = set()
        for mode in all_modes:
            pid = mode.get("prompt_id")
            if pid and pid not in seen_prompts:
                seen_prompts.add(pid)
                responses = get_prompt_responses_batch(tweet_ids, pid, db_path=db_path)
                for tid, resp in responses.items():
                    if tid not in responses_by_tweet:
                        responses_by_tweet[tid] = []
                    responses_by_tweet[tid].append(
                        {
                            "prompt_id": pid,
                            "model": resp.get("model"),
                            "response": resp.get("response"),
                        }
                    )

        # Get human labels
        human_labels = {}
        with transaction(db_path) as conn:
            placeholders = ",".join("?" * len(tweet_ids))
            rows = conn.execute(
                f"""
                SELECT tweet_id, should_show FROM human_labels
                WHERE tweet_id IN ({placeholders}) AND mode_id = ?
            """,
                (*tweet_ids, mode_id),
            ).fetchall()
            for row in rows:
                human_labels[row["tweet_id"]] = bool(row["should_show"])

        # Get retweet info
        retweets_by_tweet = get_retweets_batch(tweet_ids, db_path)

        # Get quoted tweets
        quoted_tweet_ids = [t["quoted_tweet_id"] for t in tweets if t.get("quoted_tweet_id")]
        quoted_tweets = {}
        if quoted_tweet_ids:
            quoted_tweets = get_tweets_batch(quoted_tweet_ids, db_path)

        # Get thread context for replies
        reply_tweet_ids = [t["id"] for t in tweets if t.get("reply_to_tweet_id")]
        thread_context = {}
        if reply_tweet_ids:
            thread_context = get_thread_context_batch(reply_tweet_ids, db_path=db_path)

        # Get quoted tweets for thread ancestors too
        ancestor_quoted_ids = []
        for ancestors in thread_context.values():
            for ancestor in ancestors:
                if ancestor.get("quoted_tweet_id"):
                    ancestor_quoted_ids.append(ancestor["quoted_tweet_id"])
        if ancestor_quoted_ids:
            ancestor_quoted_tweets = get_tweets_batch(ancestor_quoted_ids, db_path)
            # Enrich ancestors with their quoted tweets
            for ancestors in thread_context.values():
                for ancestor in ancestors:
                    if ancestor.get("quoted_tweet_id"):
                        ancestor["quoted_tweet"] = ancestor_quoted_tweets.get(
                            ancestor["quoted_tweet_id"]
                        )

        # Enrich tweets
        for tweet in tweets:
            tid = tweet["id"]
            tweet["mode_status"] = mode_statuses.get(tid, "pending")
            tweet["mode_decisions"] = all_mode_decisions.get(tid, {})
            tweet["responses"] = responses_by_tweet.get(tid, [])
            tweet["human_label"] = human_labels.get(tid)
            tweet["retweeted_by"] = retweets_by_tweet.get(tid, [])
            # Add quoted tweet data if this is a quote tweet
            if tweet.get("quoted_tweet_id"):
                tweet["quoted_tweet"] = quoted_tweets.get(tweet["quoted_tweet_id"])
            # Add thread context if this is a reply
            if tweet.get("reply_to_tweet_id"):
                tweet["thread_ancestors"] = thread_context.get(tid, [])

            # Add the exact text as rendered for the LLM classifier
            tweet["llm_input"] = format_tweet_for_classification(tweet)

        return jsonify({"tweets": tweets, "total": total})

    @app.route("/api/ui/label", methods=["POST"])
    def api_ui_label():
        """Add a human label for a tweet."""
        db_path = get_db_path()
        data = request.get_json()
        if not data:
            return jsonify({"error": "Missing JSON body"}), 400

        tweet_id = data.get("tweet_id")
        mode_id = data.get("mode_id")
        should_show = data.get("should_show")

        if not tweet_id or not mode_id or should_show is None:
            return jsonify({"error": "Missing required fields"}), 400

        add_human_label(tweet_id, mode_id, should_show, data.get("notes"), db_path)

        return jsonify({"status": "ok"})

    @app.route("/api/ui/tweet/<tweet_id>/replies", methods=["GET"])
    def api_ui_tweet_replies(tweet_id: str):
        """
        Get a tweet and its approved direct replies for the modal view.

        Query params:
        - mode: Mode ID for filtering approved tweets (default: "default")
        """
        db_path = get_db_path()
        mode_id = request.args.get("mode", "default")

        result = get_replies_for_tweet(tweet_id, mode_id, db_path)

        # Enrich replies with quoted tweets if present
        if result["replies"]:
            quoted_ids = [
                r["tweet"]["quoted_tweet_id"]
                for r in result["replies"]
                if r["tweet"].get("quoted_tweet_id")
            ]
            if quoted_ids:
                quoted_tweets = get_tweets_batch(quoted_ids, db_path)
                for r in result["replies"]:
                    qid = r["tweet"].get("quoted_tweet_id")
                    if qid:
                        r["tweet"]["quoted_tweet"] = quoted_tweets.get(qid)

        # Also enrich the parent tweet with its quoted tweet
        if result["tweet"] and result["tweet"].get("quoted_tweet_id"):
            quoted = get_tweet(result["tweet"]["quoted_tweet_id"], db_path)
            if quoted:
                result["tweet"]["quoted_tweet"] = quoted

        return jsonify(result)

    @app.route("/api/ui/chains", methods=["GET"])
    def api_ui_chains():
        """
        Get approved tweets as conversation chains for the Read page.

        Query params:
        - mode: Mode ID for filtering (default: "default")
        - sort: Sort field for chains (created_at or captured_at)
        - limit: Number of chains to return
        - offset: Pagination offset

        Returns chains with hidden_replies counts for expandable UI.

        Scalability: This endpoint paginates at the conversation root level,
        computing chains on-demand for each root. This avoids loading all
        approved tweets into memory.
        """
        db_path = get_db_path()
        mode_id = request.args.get("mode", "default")
        sort_field = request.args.get("sort", "captured_at")
        limit = request.args.get("limit", 20, type=int)
        offset = request.args.get("offset", 0, type=int)

        # Get paginated conversation roots
        roots, total = get_conversation_roots(
            mode_id=mode_id,
            sort_field=sort_field,
            limit=limit,
            offset=offset,
            db_path=db_path,
        )

        if not roots:
            return jsonify({"chains": [], "total": total})

        # Compute chain for each root on-demand
        chains = []
        for root in roots:
            chain_data = compute_single_chain(root["id"], mode_id, db_path)
            if chain_data["chain"]:
                chains.append(chain_data)

        # Enrich chains with additional data
        # Collect all tweet IDs in chains for batch operations
        all_tweet_ids = []
        for chain_entry in chains:
            for tweet in chain_entry["chain"]:
                all_tweet_ids.append(tweet["id"])

        if not all_tweet_ids:
            return jsonify({"chains": [], "total": total})

        # Get retweet info
        retweets_by_tweet = get_retweets_batch(all_tweet_ids, db_path)

        # Get quoted tweets
        quoted_ids = []
        for chain_entry in chains:
            for tweet in chain_entry["chain"]:
                if tweet.get("quoted_tweet_id"):
                    quoted_ids.append(tweet["quoted_tweet_id"])

        quoted_tweets = {}
        if quoted_ids:
            quoted_tweets = get_tweets_batch(quoted_ids, db_path)

        # Get prompt responses for all tweets
        all_modes = get_available_modes(db_path)
        responses_by_tweet: dict[str, list] = {}
        seen_prompts: set[str] = set()
        for mode in all_modes:
            pid = mode.get("prompt_id")
            if pid and pid not in seen_prompts:
                seen_prompts.add(pid)
                responses = get_prompt_responses_batch(all_tweet_ids, pid, db_path=db_path)
                for tid, resp in responses.items():
                    if tid not in responses_by_tweet:
                        responses_by_tweet[tid] = []
                    responses_by_tweet[tid].append(
                        {
                            "prompt_id": pid,
                            "model": resp.get("model"),
                            "response": resp.get("response"),
                        }
                    )

        # Enrich tweets in chains
        for chain_entry in chains:
            for tweet in chain_entry["chain"]:
                tid = tweet["id"]
                tweet["retweeted_by"] = retweets_by_tweet.get(tid, [])
                if tweet.get("quoted_tweet_id"):
                    tweet["quoted_tweet"] = quoted_tweets.get(tweet["quoted_tweet_id"])
                tweet["responses"] = responses_by_tweet.get(tid, [])

        return jsonify({"chains": chains, "total": total})

    return app


# =============================================================================
# Main Entry Point
# =============================================================================

if __name__ == "__main__":
    print("Aerie Tweet Collector")
    print("=====================")
    print("Starting server on http://localhost:8080")
    print("The browser extension will POST captured tweets here.")
    print()

    app = create_app()
    app.run(host="127.0.0.1", port=8080, debug=True)
