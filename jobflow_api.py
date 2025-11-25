import os
import json
import sqlite3
from datetime import datetime
from typing import Any, Dict

from flask import Flask, jsonify, request, g
from flask_cors import CORS

# -------------------------------------------------
# Flask setup
# -------------------------------------------------

app = Flask(__name__)

# Make sure instance dir exists
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INSTANCE_DIR = os.path.join(BASE_DIR, "instance")
os.makedirs(INSTANCE_DIR, exist_ok=True)

DB_PATH = os.path.join(INSTANCE_DIR, "jobflow.db")

CORS(app)


# -------------------------------------------------
# DB helpers
# -------------------------------------------------

def dict_factory(cursor, row):
    """Return rows as dicts instead of tuples."""
    d = {}
    for idx, col in enumerate(cursor.description):
        d[col[0]] = row[idx]
    return d


def ensure_schema(conn: sqlite3.Connection):
    """Create basic sessions table if it does not exist."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT UNIQUE NOT NULL,
            client_name TEXT,
            title TEXT,
            status TEXT,
            source TEXT,
            payload TEXT,
            created_at TEXT,
            updated_at TEXT
        )
        """
    )
    conn.commit()


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = dict_factory
        # Make sure schema exists on first use for this request
        ensure_schema(conn)
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# -------------------------------------------------
# Utility helpers
# -------------------------------------------------

def now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="microseconds")


def load_payload(row: Dict[str, Any]) -> Dict[str, Any]:
    raw = row.get("payload")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


def save_payload(db: sqlite3.Connection, session_row: Dict[str, Any], payload: Dict[str, Any]):
    db.execute(
        """
        UPDATE sessions
        SET payload = ?, updated_at = ?
        WHERE id = ?
        """,
        (json.dumps(payload), now_iso(), session_row["id"]),
    )
    db.commit()


def session_to_public(row: Dict[str, Any]) -> Dict[str, Any]:
    payload = load_payload(row)
    meta = payload.get("meta") or {}
    return {
        "id": row["id"],
        "session_id": row["session_id"],
        "client_name": row.get("client_name"),
        "title": row.get("title"),
        "status": row.get("status"),
        "source": row.get("source"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "payload": payload,
        "meta": meta,
    }


# -------------------------------------------------
# Core API
# -------------------------------------------------

@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


# ---------- Sessions ----------

@app.route("/api/sessions/<session_id>/upsert", methods=["POST"])
def upsert_session(session_id: str):
    """
    Upsert a session.

    Expected JSON body (example):
    {
      "meta": {
        "title": "Test Job",
        "client_name": "Cloud",
        "source": "bulk",
        "status": "new"
      }
    }
    """
    db = get_db()

    try:
        data = request.get_json(force=True, silent=False) or {}
    except Exception:
        return jsonify({"error": "Invalid JSON body"}), 400

    meta = data.get("meta") or {}
    title = meta.get("title") or f"Job {session_id}"
    client_name = meta.get("client_name")
    status = meta.get("status") or "new"
    source = meta.get("source") or "unknown"

    cur = db.execute(
        "SELECT * FROM sessions WHERE session_id = ?",
        (session_id,),
    )
    existing = cur.fetchone()
    now = now_iso()

    if existing:
        # merge payload
        payload = load_payload(existing)
        payload["meta"] = meta

        db.execute(
            """
            UPDATE sessions
            SET client_name = ?, title = ?, status = ?, source = ?, payload = ?, updated_at = ?
            WHERE session_id = ?
            """,
            (client_name, title, status, source, json.dumps(payload), now, session_id),
        )
        db.commit()

        cur = db.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,))
        row = cur.fetchone()
    else:
        payload = {"meta": meta}
        db.execute(
            """
            INSERT INTO sessions (session_id, client_name, title, status, source, payload, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (session_id, client_name, title, status, source, json.dumps(payload), now, now),
        )
        db.commit()

        cur = db.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,))
        row = cur.fetchone()

    return jsonify({"session": session_to_public(row), "status": "ok"})


@app.route("/api/sessions", methods=["GET"])
def list_sessions():
    """Return all sessions (simple listing for now)."""
    db = get_db()
    cur = db.execute(
        "SELECT * FROM sessions ORDER BY updated_at DESC"
    )
    rows = cur.fetchall()
    return jsonify(
        {
            "count": len(rows),
            "sessions": [session_to_public(r) for r in rows],
            "status": "ok",
        }
    )


@app.route("/api/sessions/<session_id>", methods=["GET"])
def get_session(session_id: str):
    db = get_db()
    cur = db.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,))
    row = cur.fetchone()
    if not row:
        return jsonify({"error": "Session not found"}), 404
    return jsonify({"session": session_to_public(row), "status": "ok"})


# ---------- Media stored INSIDE payload ----------

@app.route("/api/sessions/<session_id>/media", methods=["POST"])
def add_media_to_session(session_id: str):
    """
    Attach a media record to a session by *logical URI*.

    Body:
    {
      "uri": "local://C:/Users/Joeyv/jobflow-cloud-mini/test/20251121_142240.jpg",
      "media_type": "image",        # "image" | "audio" | "video"
      "note": "before picture"
    }

    This is stored in sessions.payload.media[] as JSON.
    """
    db = get_db()

    # find session
    cur = db.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,))
    row = cur.fetchone()
    if not row:
        return jsonify({"error": "Session not found"}), 404

    try:
        body = request.get_json(force=True, silent=False) or {}
    except Exception:
        return jsonify({"error": "Invalid JSON body"}), 400

    uri = body.get("uri")
    media_type = (body.get("media_type") or "").lower()
    note = body.get("note")

    if not uri:
        return jsonify({"error": "Missing 'uri'"}), 400
    if media_type not in {"image", "audio", "video"}:
        return jsonify({"error": "media_type must be 'image', 'audio', or 'video'"}), 400

    payload = load_payload(row)
    media_list = payload.get("media")
    if not isinstance(media_list, list):
        media_list = []
    # simple local ID within payload
    media_id = len(media_list) + 1

    media_item = {
        "id": media_id,
        "uri": uri,
        "media_type": media_type,
        "note": note,
        "created_at": now_iso(),
    }
    media_list.append(media_item)
    payload["media"] = media_list

    save_payload(db, row, payload)

    # reload row for fresh updated_at
    cur = db.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,))
    updated = cur.fetchone()

    return jsonify(
        {
            "session": session_to_public(updated),
            "media_item": media_item,
            "status": "ok",
        }
    )


# ---------- Session detail (session + media + analysis) ----------

@app.route("/api/sessions/<session_id>/detail", methods=["GET"])
def get_session_detail(session_id: str):
    """
    Return a richer view: session, meta, media, and analysis placeholders.
    Everything lives inside payload for now.
    """
    db = get_db()

    cur = db.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,))
    row = cur.fetchone()
    if not row:
        return jsonify({"error": "Session not found"}), 404

    payload = load_payload(row)

    meta = payload.get("meta") or {}
    media = payload.get("media") or []
    analysis = payload.get("analysis") or None

    return jsonify(
        {
            "session": session_to_public(row),
            "meta": meta,
            "media": media,
            "analysis": analysis,
            "status": "ok",
        }
    )


# ---------- Simple AI placeholder (hook for later) ----------

@app.route("/api/sessions/<session_id>/analyze", methods=["POST"])
def analyze_session_placeholder(session_id: str):
    """
    Placeholder endpoint so we have a stable URL for future AI.

    For now, it just stores a static 'analysis' block in payload.
    """
    db = get_db()

    cur = db.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,))
    row = cur.fetchone()
    if not row:
        return jsonify({"error": "Session not found"}), 404

    payload = load_payload(row)
    media = payload.get("media") or []
    meta = payload.get("meta") or {}

    analysis = {
        "summary": "Placeholder analysis – AI not wired yet.",
        "total_media_items": len(media),
        "meta_snapshot": meta,
        "generated_at": now_iso(),
    }

    payload["analysis"] = analysis
    save_payload(db, row, payload)

    cur = db.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,))
    updated = cur.fetchone()

    return jsonify(
        {
            "session": session_to_public(updated),
            "analysis": analysis,
            "status": "ok",
        }
    )


# -------------------------------------------------
# Main (for local dev)
# -------------------------------------------------

if __name__ == "__main__":
    # Local dev only; Render uses gunicorn via jobflow_api:app
    app.run(host="0.0.0.0", port=10000, debug=True)
