import os
from flask import Flask, request, jsonify

app = Flask(__name__)

@app.route("/health")
def health():
    # Distinctive response so we can tell this version is live
    return jsonify({
        "status": "ok",
        "app": "MINIMAL",
        "version": 2
    })

@app.route("/api/sync-session", methods=["POST"])
def sync_session():
    data = request.get_json(silent=True) or {}
    return jsonify({
        "route": "sync-session",
        "received": data
    }), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5065")), debug=True)
