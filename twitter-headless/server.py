#!/usr/bin/env python3
"""
HTTP server for triggering Twitter collection.

This server exposes a simple API for the main Aerie collector to trigger
headless Twitter collection runs.

Endpoints:
    GET  /health  - Health check
    POST /collect - Trigger collection (runs in background)
    GET  /status  - Get collection status
"""

import threading

from flask import Flask, jsonify, request

from collector import CollectionStatus, load_config, run_collection

app = Flask(__name__)

# Shared state
_status = CollectionStatus()
_lock = threading.Lock()
_collection_thread: threading.Thread | None = None


@app.after_request
def add_cors_headers(response):
    """Add CORS headers for cross-origin requests."""
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return response


@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint."""
    return jsonify({"status": "ok"})


@app.route("/collect", methods=["POST", "OPTIONS"])
def collect():
    """
    Trigger collection. Returns immediately, collection runs in background.

    Request body (JSON, optional):
        - duration: Scroll duration in seconds (default: from config)
        - headless: Whether to run headless (default: true)

    Response:
        - status: "started" or "already_running"
        - duration: Configured scroll duration
    """
    global _collection_thread

    if request.method == "OPTIONS":
        return "", 204

    with _lock:
        if _status.running:
            return jsonify({"status": "already_running"})

        data = request.get_json() or {}
        duration = data.get("duration")
        headless = data.get("headless", True)

        # Get configured duration if not specified
        config = load_config()
        if duration is None:
            duration = config.scroll_duration

        def run_in_thread():
            try:
                run_collection(duration=duration, status=_status, headless=headless)
            except Exception as e:
                print(f"[Server] Collection error: {e}")

        _collection_thread = threading.Thread(target=run_in_thread, daemon=True)
        _collection_thread.start()

        return jsonify({"status": "started", "duration": duration})


@app.route("/status", methods=["GET"])
def status():
    """Get collection status."""
    return jsonify({
        "running": _status.running,
        "last_result": _status.last_result,
        "tweets_captured": _status.tweets_captured,
        "error": _status.error,
    })


def main():
    """Server entry point."""
    config = load_config()
    port = config.server_port

    print("Aerie Headless Twitter Collector")
    print("=================================")
    print(f"Starting server on http://0.0.0.0:{port}")
    print(f"Main collector URL: {config.collector_url}")
    print()
    print("Endpoints:")
    print(f"  GET  http://localhost:{port}/health  - Health check")
    print(f"  POST http://localhost:{port}/collect - Trigger collection")
    print(f"  GET  http://localhost:{port}/status  - Collection status")
    print()

    app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
