#!/usr/bin/env python3
"""
Aerie Tweet Collector Server

A simple Flask server that receives tweets from the browser extension
and stores them in SQLite for later classification.
"""

import json

from flask import Flask, request, jsonify, render_template
from database import (
    init_database, store_tweets, store_retweets, get_stats, list_modes,
    add_human_label, transaction, get_prompt_responses_batch,
    get_retweets_batch, get_tweets_batch, get_thread_context_batch, get_mode,
    create_mode, update_mode, delete_mode, get_mode_label_counts, list_prompts,
    get_prompt, get_mode_decisions_batch, invalidate_mode_decisions,
)
from modes import decide_tweets_batch, get_mode_status_for_all_tweets, get_available_modes, get_mode_stats, compute_mode_decisions
from classifier import setup_prompts_and_modes
from extractors import list_extractor_schemas, get_extractor_with_config, EXTRACTOR_SCHEMAS
from prefilters import list_prefilter_schemas, get_prefilter_with_config, PREFILTER_SCHEMAS

app = Flask(__name__)


@app.before_request
def ensure_db():
    """Initialize database on first request."""
    if not hasattr(app, '_db_initialized'):
        setup_prompts_and_modes()  # Also initializes database
        app._db_initialized = True


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

    result = store_tweets(tweets)

    # Also store retweets if provided
    retweets = data.get("retweets", [])
    rt_result = {"inserted": 0, "duplicates": 0}
    if retweets:
        rt_result = store_retweets(retweets)

    return jsonify({
        "status": "ok",
        "received": len(tweets),
        "inserted": result["inserted"],
        "duplicates": result["duplicates"],
        "retweets_received": len(retweets),
        "retweets_inserted": rt_result["inserted"],
    })


@app.route("/stats", methods=["GET"])
def stats():
    """
    Get database statistics.
    Query param: mode (optional) - mode ID for mode-specific stats
    """
    mode_id = request.args.get("mode")
    if mode_id:
        try:
            return jsonify(get_mode_stats(mode_id))
        except KeyError as e:
            return jsonify({"error": str(e)}), 400
    # Fallback to legacy stats for backwards compatibility
    return jsonify(get_stats())


@app.route("/tweets/check", methods=["POST", "OPTIONS"])
def check_tweets():
    """
    Check the approval status of multiple tweets.
    Expects JSON body: {"ids": ["123", "456", ...], "mode": "mode_id"}
    Returns: {"123": "approved", "456": "pending", ...}
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

    try:
        statuses = decide_tweets_batch(ids, mode_id)
    except KeyError as e:
        return jsonify({"error": str(e)}), 400

    return jsonify(statuses)


@app.route("/tweets/classified-ids", methods=["GET"])
def classified_ids():
    """
    Get all classified tweet IDs for cache pre-population.
    Query param: mode (defaults to "default")
    """
    mode_id = request.args.get("mode", "default")

    try:
        ids = get_mode_status_for_all_tweets(mode_id)
    except KeyError as e:
        return jsonify({"error": str(e)}), 400

    return jsonify(ids)


@app.route("/modes", methods=["GET"])
def list_modes_endpoint():
    """List all available filtering modes."""
    modes = get_available_modes()
    return jsonify({"modes": modes})


@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint."""
    return jsonify({"status": "ok"})


# =============================================================================
# Modes Management API
# =============================================================================


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
    modes = list_modes()
    label_counts = get_mode_label_counts()

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
    mode = get_mode(mode_id)
    if not mode:
        return jsonify({"error": "Mode not found"}), 404

    mode = _parse_mode_config(mode)
    label_counts = get_mode_label_counts()
    mode["human_label_count"] = label_counts.get(mode_id, 0)
    mode["is_protected"] = mode_id == "default"

    return jsonify(mode)


@app.route("/api/modes", methods=["POST"])
def api_create_mode():
    """Create a new mode."""
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
    if get_mode(mode_id):
        return jsonify({"error": f"Mode already exists: {mode_id}"}), 409

    # Validate prompt exists
    if not get_prompt(data["prompt_id"]):
        return jsonify({"error": f"Prompt not found: {data['prompt_id']}"}), 400

    # Validate extractor exists
    if data["extractor"] not in EXTRACTOR_SCHEMAS:
        return jsonify({"error": f"Unknown extractor: {data['extractor']}"}), 400

    # Validate prefilter if provided
    if data.get("prefilter") and data["prefilter"] not in PREFILTER_SCHEMAS:
        return jsonify({"error": f"Unknown prefilter: {data['prefilter']}"}), 400

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
    )

    # Compute cached decisions for the new mode
    # This is important for prefilter-only modes which can decide without LLM
    compute_mode_decisions(mode_id)

    # Return the created mode
    mode = get_mode(mode_id)
    return jsonify({"status": "ok", "mode": _parse_mode_config(mode)})


@app.route("/api/modes/<mode_id>", methods=["PUT"])
def api_update_mode(mode_id):
    """Update an existing mode."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "Missing JSON body"}), 400

    if not get_mode(mode_id):
        return jsonify({"error": "Mode not found"}), 404

    # Validate prompt if changing
    if data.get("prompt_id") and not get_prompt(data["prompt_id"]):
        return jsonify({"error": f"Prompt not found: {data['prompt_id']}"}), 400

    # Validate extractor if changing
    if data.get("extractor") and data["extractor"] not in EXTRACTOR_SCHEMAS:
        return jsonify({"error": f"Unknown extractor: {data['extractor']}"}), 400

    # Validate prefilter if changing
    if data.get("prefilter") and data["prefilter"] not in PREFILTER_SCHEMAS:
        return jsonify({"error": f"Unknown prefilter: {data['prefilter']}"}), 400

    # Determine what to clear vs update
    # If prefilter is explicitly null/empty, clear it
    clear_prefilter = "prefilter" in data and not data.get("prefilter")
    # If config is explicitly null or {}, clear it
    clear_extractor_config = "extractor_config" in data and not data.get("extractor_config")
    clear_prefilter_config = "prefilter_config" in data and not data.get("prefilter_config")

    update_mode(
        mode_id=mode_id,
        name=data.get("name"),
        prompt_id=data.get("prompt_id"),
        extractor=data.get("extractor"),
        prefilter=data.get("prefilter") if not clear_prefilter else None,
        extractor_config=data.get("extractor_config") if not clear_extractor_config else None,
        prefilter_config=data.get("prefilter_config") if not clear_prefilter_config else None,
        description=data.get("description"),
        clear_prefilter=clear_prefilter,
        clear_extractor_config=clear_extractor_config,
        clear_prefilter_config=clear_prefilter_config,
    )

    # Invalidate and recompute cached decisions if config changed
    config_changed = any([
        data.get("prompt_id"),
        data.get("extractor"),
        "prefilter" in data,
        "extractor_config" in data,
        "prefilter_config" in data,
    ])
    if config_changed:
        invalidate_mode_decisions(mode_id)
        compute_mode_decisions(mode_id)

    # Return updated mode
    mode = get_mode(mode_id)
    return jsonify({"status": "ok", "mode": _parse_mode_config(mode)})


@app.route("/api/modes/<mode_id>", methods=["DELETE"])
def api_delete_mode(mode_id):
    """Delete a mode."""
    if mode_id == "default":
        return jsonify({"error": "Cannot delete the default mode"}), 400

    if not get_mode(mode_id):
        return jsonify({"error": "Mode not found"}), 404

    # Invalidate cached decisions before deleting the mode
    invalidate_mode_decisions(mode_id)

    force = request.args.get("force", "").lower() == "true"
    result = delete_mode(mode_id, force=force)

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


@app.route("/api/prompts", methods=["GET"])
def api_list_prompts():
    """List prompts for dropdown."""
    prompts = list_prompts()
    # Return minimal info for dropdowns
    return jsonify({
        "prompts": [{"id": p["id"], "created_at": p.get("created_at")} for p in prompts]
    })


# =============================================================================
# Web UI Routes
# =============================================================================


@app.route("/ui/label")
def ui_label():
    """Labeling interface for tweets."""
    modes = get_available_modes()
    return render_template("label.html", modes=modes, active_page="label")


@app.route("/ui/read")
def ui_read():
    """Reading interface for approved tweets."""
    modes = get_available_modes()
    return render_template("read.html", modes=modes, active_page="read")


@app.route("/ui/modes")
def ui_modes():
    """Modes management interface."""
    modes = get_available_modes()
    prompts = list_prompts()
    return render_template("modes.html", modes=modes, prompts=prompts, active_page="modes")


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
    mode_id = request.args.get("mode", "default")
    status_filter = request.args.get("status", "all")
    search = request.args.get("search", "")
    sort_field = request.args.get("sort", "captured_at")
    limit = request.args.get("limit", 20, type=int)
    offset = request.args.get("offset", 0, type=int)
    leaf_only = request.args.get("leaf_only", "").lower() == "true"

    # Get mode config
    mode_config = get_mode(mode_id)
    prompt_id = mode_config["prompt_id"] if mode_config else "binary_filter_v1"

    # Build query using cached mode_decisions table
    with transaction() as conn:
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
            params = []

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
    mode_statuses = get_mode_decisions_batch(tweet_ids, mode_id)

    # Get cached decisions from all modes for display
    all_modes = get_available_modes()
    all_mode_decisions = {}
    for mode in all_modes:
        decisions = get_mode_decisions_batch(tweet_ids, mode["id"])
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
            responses = get_prompt_responses_batch(tweet_ids, pid)
            for tid, resp in responses.items():
                if tid not in responses_by_tweet:
                    responses_by_tweet[tid] = []
                responses_by_tweet[tid].append({
                    "prompt_id": pid,
                    "model": resp.get("model"),
                    "response": resp.get("response"),
                })

    # Get human labels
    human_labels = {}
    with transaction() as conn:
        placeholders = ",".join("?" * len(tweet_ids))
        rows = conn.execute(f"""
            SELECT tweet_id, should_show FROM human_labels
            WHERE tweet_id IN ({placeholders}) AND mode_id = ?
        """, (*tweet_ids, mode_id)).fetchall()
        for row in rows:
            human_labels[row["tweet_id"]] = bool(row["should_show"])

    # Get retweet info
    retweets_by_tweet = get_retweets_batch(tweet_ids)

    # Get quoted tweets
    quoted_tweet_ids = [t["quoted_tweet_id"] for t in tweets if t.get("quoted_tweet_id")]
    quoted_tweets = {}
    if quoted_tweet_ids:
        quoted_tweets = get_tweets_batch(quoted_tweet_ids)

    # Get thread context for replies
    reply_tweet_ids = [t["id"] for t in tweets if t.get("reply_to_tweet_id")]
    thread_context = {}
    if reply_tweet_ids:
        thread_context = get_thread_context_batch(reply_tweet_ids)

    # Get quoted tweets for thread ancestors too
    ancestor_quoted_ids = []
    for ancestors in thread_context.values():
        for ancestor in ancestors:
            if ancestor.get("quoted_tweet_id"):
                ancestor_quoted_ids.append(ancestor["quoted_tweet_id"])
    if ancestor_quoted_ids:
        ancestor_quoted_tweets = get_tweets_batch(ancestor_quoted_ids)
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

    return jsonify({"tweets": tweets, "total": total})


@app.route("/api/ui/label", methods=["POST"])
def api_ui_label():
    """Add a human label for a tweet."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "Missing JSON body"}), 400

    tweet_id = data.get("tweet_id")
    mode_id = data.get("mode_id")
    should_show = data.get("should_show")

    if not tweet_id or not mode_id or should_show is None:
        return jsonify({"error": "Missing required fields"}), 400

    add_human_label(tweet_id, mode_id, should_show, data.get("notes"))

    return jsonify({"status": "ok"})


if __name__ == "__main__":
    print("Aerie Tweet Collector")
    print("=====================")
    print("Starting server on http://localhost:8080")
    print("The browser extension will POST captured tweets here.")
    print()

    setup_prompts_and_modes()  # Initialize database and create default prompts/modes
    app.run(host="127.0.0.1", port=8080, debug=True)
