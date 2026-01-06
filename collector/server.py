#!/usr/bin/env python3
"""
Aerie Tweet Collector Server

A simple Flask server that receives tweets from the browser extension
and stores them in SQLite for later classification.
"""

from flask import Flask, request, jsonify
from database import (
    init_database, store_tweets, get_stats, get_pending_tweets,
    get_approved_tweets, check_tweet_statuses, update_classification,
    approve_all_pending, get_all_classified_ids
)

app = Flask(__name__)


@app.before_request
def ensure_db():
    """Initialize database on first request."""
    if not hasattr(app, '_db_initialized'):
        init_database()
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
    Expects JSON body: {"tweets": [...]}
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

    return jsonify({
        "status": "ok",
        "received": len(tweets),
        "inserted": result["inserted"],
        "duplicates": result["duplicates"],
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
    Expects JSON body: {"ids": ["123", "456", ...]}
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
    """Get all classified tweet IDs for cache pre-population."""
    ids = get_all_classified_ids()
    return jsonify(ids)


@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint."""
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    print("Aerie Tweet Collector")
    print("=====================")
    print("Starting server on http://localhost:8080")
    print("The browser extension will POST captured tweets here.")
    print()

    init_database()
    app.run(host="127.0.0.1", port=8080, debug=True)
