#!/usr/bin/env python3
"""
Aerie Tweet Collector Server

A simple Flask server that receives tweets from the browser extension
and stores them in SQLite for later classification.
"""

from flask import Flask, request, jsonify, render_template
from database import (
    init_database, store_tweets, store_retweets, get_stats, get_pending_tweets,
    get_approved_tweets, check_tweet_statuses, update_classification,
    approve_all_pending, get_all_classified_ids, list_modes,
    add_human_label, get_human_labels, transaction, get_prompt_responses_batch,
    get_retweets_batch, get_tweets_batch, get_thread_context_batch,
)
from modes import decide_tweets_batch, get_mode_status_for_all_tweets, get_available_modes
from classifier import setup_prompts_and_modes

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
    """Get database statistics."""
    return jsonify(get_stats())


@app.route("/tweets/pending", methods=["GET"])
def pending_tweets():
    """Get tweets awaiting classification."""
    limit = request.args.get("limit", 100, type=int)
    tweets = get_pending_tweets(limit)
    return jsonify({"tweets": tweets, "count": len(tweets)})


@app.route("/tweets/approved", methods=["GET"])
def approved_tweets():
    """Get tweets that passed classification."""
    limit = request.args.get("limit", 100, type=int)
    offset = request.args.get("offset", 0, type=int)
    tweets = get_approved_tweets(limit, offset)
    return jsonify({"tweets": tweets, "count": len(tweets)})


@app.route("/tweets/check", methods=["POST", "OPTIONS"])
def check_tweets():
    """
    Check the approval status of multiple tweets.
    Expects JSON body: {"ids": ["123", "456", ...], "mode": "optional_mode_id"}
    Returns: {"123": "approved", "456": "pending", ...}

    If mode is specified, uses the new mode-based classification.
    Otherwise falls back to legacy classification_result field.
    """
    if request.method == "OPTIONS":
        return "", 204

    data = request.get_json()
    if not data or "ids" not in data:
        return jsonify({"error": "Missing 'ids' field"}), 400

    ids = data["ids"]
    if not isinstance(ids, list):
        return jsonify({"error": "'ids' must be an array"}), 400

    mode_id = data.get("mode")

    if mode_id:
        # Use new mode-based classification
        try:
            statuses = decide_tweets_batch(ids, mode_id)
        except KeyError as e:
            return jsonify({"error": str(e)}), 400
    else:
        # Fall back to legacy classification
        statuses = check_tweet_statuses(ids)

    return jsonify(statuses)


@app.route("/tweets/approve-all", methods=["POST", "OPTIONS"])
def approve_all():
    """Approve all pending tweets. For testing/debugging."""
    if request.method == "OPTIONS":
        return "", 204

    count = approve_all_pending()
    return jsonify({"approved": count})


@app.route("/tweets/classified-ids", methods=["GET"])
def classified_ids():
    """
    Get all classified tweet IDs for cache pre-population.
    Optional query param: mode (uses mode-based classification)
    """
    mode_id = request.args.get("mode")

    if mode_id:
        # Use new mode-based classification
        try:
            ids = get_mode_status_for_all_tweets(mode_id)
        except KeyError as e:
            return jsonify({"error": str(e)}), 400
    else:
        # Fall back to legacy classification
        ids = get_all_classified_ids()

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
    """
    import json
    from database import get_mode

    mode_id = request.args.get("mode", "default")
    status_filter = request.args.get("status", "all")
    search = request.args.get("search", "")
    sort_field = request.args.get("sort", "captured_at")
    limit = request.args.get("limit", 20, type=int)
    offset = request.args.get("offset", 0, type=int)

    # Get mode config
    mode_config = get_mode(mode_id)
    prompt_id = mode_config["prompt_id"] if mode_config else "binary_filter_v1"

    # Build query
    with transaction() as conn:
        # Base query - for status filtering, we need to join with prompt_responses
        if status_filter in ("approved", "filtered", "pending"):
            # Join with prompt_responses to filter by status
            query = """
                SELECT DISTINCT t.* FROM tweets t
                LEFT JOIN prompt_responses pr ON t.id = pr.tweet_id AND pr.prompt_id = ?
            """
            params = [prompt_id]

            if status_filter == "pending":
                # No response yet
                query = query.replace("LEFT JOIN", "LEFT JOIN")
                conditions = ["pr.tweet_id IS NULL"]
            else:
                # Has response - filter by approved field in JSON
                # For binary_filter prompts, check the 'approved' field
                if status_filter == "approved":
                    conditions = ["json_extract(pr.response_json, '$.approved') = 1"]
                else:
                    conditions = ["json_extract(pr.response_json, '$.approved') = 0"]
        elif status_filter == "unlabeled":
            query = """
                SELECT t.* FROM tweets t
                LEFT JOIN human_labels hl ON t.id = hl.tweet_id AND hl.mode_id = ?
            """
            params = [mode_id]
            conditions = ["hl.tweet_id IS NULL"]
        else:
            query = "SELECT * FROM tweets t"
            params = []
            conditions = []

        # Search filter
        if search:
            conditions.append("(t.author_username LIKE ? OR t.text LIKE ?)")
            params.extend([f"%{search}%", f"%{search}%"])

        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        # Sort
        if sort_field in ("created_at", "captured_at"):
            query += f" ORDER BY t.{sort_field} DESC"
        else:
            query += " ORDER BY t.captured_at DESC"

        # Get total count first
        count_query = query.replace("SELECT DISTINCT t.*", "SELECT COUNT(DISTINCT t.id)", 1)
        count_query = count_query.replace("SELECT t.*", "SELECT COUNT(DISTINCT t.id)", 1)
        count_query = count_query.replace("SELECT *", "SELECT COUNT(*)", 1)
        total = conn.execute(count_query, params).fetchone()[0]

        # Add pagination
        query += " LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = conn.execute(query, params).fetchall()
        tweets = [dict(row) for row in rows]

    if not tweets:
        return jsonify({"tweets": [], "total": 0})

    # Get mode decisions for all tweets
    tweet_ids = [t["id"] for t in tweets]
    try:
        mode_statuses = decide_tweets_batch(tweet_ids, mode_id)
    except KeyError:
        mode_statuses = {}

    # Get all mode decisions for display
    all_modes = get_available_modes()
    all_mode_decisions = {}
    for mode in all_modes:
        try:
            decisions = decide_tweets_batch(tweet_ids, mode["id"])
            for tid, status in decisions.items():
                if tid not in all_mode_decisions:
                    all_mode_decisions[tid] = {}
                all_mode_decisions[tid][mode["id"]] = status
        except Exception:
            pass

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
