import os
from datetime import datetime

from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.dialects.postgresql import JSONB


# ---------------------------------------------------------------------------
# App + DB setup
# ---------------------------------------------------------------------------

app = Flask(__name__)

# DATABASE_URL should be set in Render (Postgres URL).
# Locally we fall back to a sqlite file so you can still run smoke tests.
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
if not DATABASE_URL:
    DATABASE_URL = "sqlite:///jobflow_local.db"

app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)
CORS(app, resources={r"/api/*": {"origins": "*"}})


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class JFSession(db.Model):
    """
    Cloud session record.

    We keep "nice" top-level columns for quick filtering (client_name, title,
    source, status) and also store the full blob coming from the bot in
    `payload` (JSON).
    """
    __tablename__ = "jf_sessions"

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.String(128), index=True, nullable=False, unique=True)

    # Convenience fields (duplicated from payload.meta when present)
    client_name = db.Column(db.String(255), nullable=True)
    title = db.Column(db.String(255), nullable=True)
    source = db.Column(db.String(64), nullable=True)
    status = db.Column(db.String(64), nullable=True)

    payload = db.Column(JSONB if DATABASE_URL.startswith("postgresql") else db.JSON, nullable=False, default=dict)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class JFMedia(db.Model):
    """
    Media linked to a session (photos, audio, etc.) – future use.

    We’re not wiring endpoints in this file yet, but the schema is ready.
    """
    __tablename__ = "jf_media"

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.String(128), index=True, nullable=False)
    media_type = db.Column(db.String(32), nullable=False)   # "image" / "audio" / etc.
    url = db.Column(db.String(1024), nullable=True)         # public or signed URL
    filename = db.Column(db.String(255), nullable=True)
    metadata = db.Column(JSONB if DATABASE_URL.startswith("postgresql") else db.JSON, nullable=True)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


# ---------------------------------------------------------------------------
# Schema bootstrap
# ---------------------------------------------------------------------------

with app.app_context():
    # This will create jf_sessions and jf_media if they don't exist.
    # It will NOT drop any existing tables.
    db.create_all()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now():
    return datetime.utcnow()


def serialize_session(row: JFSession) -> dict:
    """
    Convert a JFSession row into the JSON shape used by the API.

    Matches what you saw from /api/sessions:
    {
      "id": 1,
      "session_id": "jobflow_test_1",
      "client_name": "Cloud",
      "title": "Test Job",
      "source": "bulk",
      "status": "new",
      "payload": { "meta": { ... } },
      "created_at": "...",
      "updated_at": "..."
    }
    """
    if not row:
        return None

    payload = row.payload or {}
    meta = payload.get("meta") if isinstance(payload, dict) else {}

    def pick(attr, key):
        return getattr(row, attr, None) or meta.get(key)

    def dt(value):
        return value.isoformat() if value is not None else None

    return {
        "id": row.id,
        "session_id": row.session_id,
        "client_name": pick("client_name", "client_name"),
        "title": pick("title", "title"),
        "source": pick("source", "source"),
        "status": pick("status", "status"),
        "payload": payload,
        "created_at": dt(row.created_at),
        "updated_at": dt(row.updated_at),
    }


def upsert_session_record(session_id: str, body: dict) -> JFSession:
    """
    Core upsert logic used by /api/sessions/<session_id>/upsert.

    body is whatever JSON your bot sends. We expect either:
      { "meta": { title, client_name, source, status, ... }, ... }
    or:
      { "title": ..., "client_name": ..., "source": ..., "status": ... }

    We store the full body in payload, and mirror some keys onto top-level cols.
    """
    if body is None:
        body = {}

    if not isinstance(body, dict):
        raise ValueError("Body must be a JSON object")

    meta = body.get("meta")
    if not isinstance(meta, dict):
        # Fall back: treat the whole body as meta if no nested meta block.
        meta = body

    title = meta.get("title")
    client_name = meta.get("client_name")
    source = meta.get("source") or "bulk"
    status = meta.get("status") or "new"

    # Look up existing session by session_id
    session = JFSession.query.filter_by(session_id=session_id).first()

    now = _now()

    if session is None:
        session = JFSession(
            session_id=session_id,
            title=title,
            client_name=client_name,
            source=source,
            status=status,
            payload=body,
            created_at=now,
            updated_at=now,
        )
        db.session.add(session)
    else:
        session.title = title or session.title
        session.client_name = client_name or session.client_name
        session.source = source or session.source
        session.status = status or session.status
        session.payload = body
        session.updated_at = now

    db.session.commit()
    return session


# ---------------------------------------------------------------------------
# Basic routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return "JobFlow Cloud Mini API", 200


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


# ---------------------------------------------------------------------------
# Sessions API
# ---------------------------------------------------------------------------

@app.route("/api/sessions", methods=["GET"])
def list_sessions():
    """
    List sessions, newest first.

    GET /api/sessions?limit=20
    """
    try:
        limit = request.args.get("limit", default=20, type=int)
        if limit <= 0:
            limit = 20
    except Exception:
        limit = 20

    query = JFSession.query.order_by(JFSession.created_at.desc()).limit(limit)
    rows = query.all()

    sessions = [serialize_session(r) for r in rows]

    return jsonify(
        {
            "status": "ok",
            "count": len(sessions),
            "sessions": sessions,
        }
    )


@app.route("/api/sessions/<session_id>", methods=["GET"])
def get_session_detail(session_id):
    """
    Get a single session by its session_id.

    Example:
      GET /api/sessions/jobflow_test_1
    """
    session = (
        JFSession.query.filter_by(session_id=session_id)
        .order_by(JFSession.id.desc())
        .first()
    )

    if not session:
        return (
            jsonify(
                {
                    "status": "error",
                    "error": "not_found",
                    "message": f"Session '{session_id}' not found",
                }
            ),
            404,
        )

    return jsonify({"status": "ok", "session": serialize_session(session)}), 200


@app.route("/api/sessions/<session_id>/upsert", methods=["POST"])
def upsert_session(session_id):
    """
    Create or update a session.

    POST /api/sessions/jobflow_test_1/upsert
    Body (example):
    {
      "meta": {
        "title": "Test Job",
        "client_name": "Cloud",
        "source": "bulk",
        "status": "new"
      },
      "notes": "anything else your bot wants to store"
    }
    """
    try:
        body = request.get_json(silent=True)
        session = upsert_session_record(session_id, body)
        return jsonify({"status": "ok", "session": serialize_session(session)}), 200
    except Exception as exc:
        # Basic error surface – good enough for now
        app.logger.exception("Upsert failed for session_id=%s", session_id)
        return (
            jsonify(
                {
                    "status": "error",
                    "error": "upsert_failed",
                    "message": str(exc),
                }
            ),
            500,
        )


# ---------------------------------------------------------------------------
# Entrypoint for local debugging
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Local dev server (Render will use gunicorn jobflow_api:app)
    app.run(host="0.0.0.0", port=10000, debug=True)
