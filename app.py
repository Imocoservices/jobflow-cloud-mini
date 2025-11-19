from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})

@app.route("/api/sync-session", methods=["POST"])
def sync_session():
    data = request.get_json(force=True)
    print("SYNC SESSION RECEIVED:", data)
    return jsonify({"received": True, "data": data})
