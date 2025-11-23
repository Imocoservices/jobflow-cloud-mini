import os
from datetime import datetime

from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy

from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy import JSON as SAJSON


# ----------------------------------------------------------------------
# App + DB setup
# ----------------------------------------------------------------------

app = Flask(__name__)

# Render / local secret
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")

# Database URL – prefer DATABASE_URL (Render) but allow local override
db_url = (
    os.environ.get("DATABASE_URL")
    or os.environ.get("SQLALCHEMY_DATABASE_URI")
    or "sqlite:///jobflow_cloud.db"
)

# Render sometimes provides postgres://, SQLAlchemy wants postgresql://
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)
CORS(app, resources={r"/api/*": {"origins": "*"}})

# Choose JSON type that works both locally & on Render
db_uri_lower = db_url.lower()
JSONType = JSONB if db_uri_lower.startswith("postgresql") else SAJSON


# ----------------------------------------------------------------------
# Models
# ----------------------------------------------------------------------


class JFSession(db.Model):
    """
    Core JobFlow session record.

    One row per job/session. Identified by a string session_id that your
    local bot uses (e.g. "jobflow_test_1", "2025-11-23_seagrass").
    """

    __tablename__ = "jf_sessions"

    id = db.Column(db.Integer, primary_key=True)

    # External session key from your local bot / tools, unique per job.
    session_id = db.Column(db.String(128), unique=True, index=True, nullable=False)

    title = db.Column(db.String(255), nullable=True)
    client_name = db.Column(db.String(255), nullable=True)
    source = db.Column(db.String(64), nullable=True)  # e.g. "bulk", "whatsapp", "phone"
    status = db.Column(db.String(64), nullable=True)  # e.g. "new", "quoted", "closed"

    # Raw JSON payload for anything extra (AI analysis, notes, etc.)
    payload = db.Column(JSONType, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )


class JFMedia(db.Model):
    """
    Media files attached to a session (images, audio, etc.).
    """

    __tablename__ = "jf_media"

    id = db.Column(db.Integer, primary_key=True)

    session_id = db.Column(
        db.String(128),
        db.ForeignKey("jf_sessions.session_id"),
        index=True,
        nullable=False,
    )

    # "image" | "audio" | "other"
    media_type = db.Column(db.String(32), nullable=False)

    # For now we just store a URL/path – your local bot or future upload
    # service will control where the actual file lives.
    file_url = db.Column(db.String(512), nullable=False)

    # IMPORTANT:
    # - Python attribute is media_metadata (OK for SQLAlchemy)
    # - DB column name is "metadata" so it matches the existing table
    media_metadata = db.Column("metadata", JSONType, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


# ----------------------------------------------------------------------
# DB init
# ----------------------------------------------------------------------


def init_db():
    """
    Ensure tables exist. Safe to call on every startup.
    """
    with app.app_context():
        db.create_all()
        print("[INIT_DB] jf_sessions / jf_media schema ensured.")


init_db()


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def session_to_dict(s: JFSession):
    payload = s.payload or {}
    return {
        "id": s.id,
        "session_id": s.session_id,
        "title": s.title,
        "client_name": s.client_name,
        "source": s.source,
        "status": s.status,
        "payload": payload,
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
    }


def media_to_dict(m: JFMedia):
    return {
        "id": m.id,
        "session_id": m.session_id,
        "media_type": m.media_type,
        "file_url": m.file_url,
        "metadata": m.media_metadata or {},
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }


# ----------------------------------------------------------------------
# Basic routes
# ----------------------------------------------------------------------


@app.route("/")
def index():
    # Simple landing text – avoids template issues on Render
    return "JobFlow Cloud Mini API is running.", 200


@app.route("/api/health")
def api_health():
    return jsonify({"status": "ok"})


# ----------------------------------------------------------------------
# Sessions API
# ----------------------------------------------------------------------


@app.route("/api/sessions", methods=["GET"])
def list_sessions():
    """
    GET /api/sessions?limit=20

    Returns latest sessions ordered by created_at DESC.
    """
    try:
        limit = int(request.args.get("limit", "20"))
    except ValueError:
        limit = 20

    limit = max(1, min(limit, 100))

    rows = (
        JFSession.query.order_by(JFSession.created_at.desc())
        .limit(limit)
        .all()
    )

    return jsonify(
        {
            "status": "ok",
            "count": len(rows),
            "sessions": [session_to_dict(s) for s in rows],
        }
    )


@app.route("/api/sessions/<string:session_id>/upsert", methods=["POST"])
def upsert_session(session_id):
    """
    POST /api/sessions/<session_id>/upsert
    Body JSON:
    {
      "meta": {
        "title": "Test Job",
        "client_name": "Cloud",
        "source": "bulk",
        "status": "new",
        ...anything else you like...
      }
    }

    Also accepts a flat body (no "meta") and will wrap it automatically.
    """
    body = request.get_json(silent=True) or {}

    # Accept either {"meta": {...}} or just {...}
    if "meta" in body and isinstance(body["meta"], dict):
        meta = body["meta"]
    else:
        meta = body

    # Upsert by session_id
    sess = JFSession.query.filter_by(session_id=session_id).first()
    is_new = False

    if sess is None:
        sess = JFSession(session_id=session_id)
        is_new = True

    # Map core fields from meta
    sess.title = meta.get("title") or sess.title
    sess.client_name = meta.get("client_name") or sess.client_name
    sess.source = meta.get("source") or sess.source or "bulk"
    sess.status = meta.get("status") or sess.status or "new"

    # Store the whole meta under payload.meta
    payload = sess.payload or {}
    payload["meta"] = meta
    sess.payload = payload

    if is_new:
        db.session.add(sess)

    db.session.commit()

    return jsonify(
        {
            "status": "ok",
            "session": session_to_dict(sess),
        }
    )


@app.route("/api/sessions/<string:session_id>", methods=["GET"])
def get_session_detail(session_id):
    """
    GET /api/sessions/<session_id>

    Returns one session plus its media list.
    """
    sess = JFSession.query.filter_by(session_id=session_id).first()
    if not sess:
        return jsonify({"status": "error", "error": "session_not_found"}), 404

    media_rows = (
        JFMedia.query.filter_by(session_id=session_id)
        .order_by(JFMedia.created_at.asc())
        .all()
    )

    return jsonify(
        {
            "status": "ok",
            "session": session_to_dict(sess),
            "media": [media_to_dict(m) for m in media_rows],
        }
    )


# ----------------------------------------------------------------------
# Media upload stubs (for future local bot / app use)
# ----------------------------------------------------------------------


def _create_media_record(session_id: str, media_type: str):
    """
    Internal helper: expects JSON:

    {
      "file_url": "https://... or local path",
      "metadata": {... optional ...}
    }
    """
    body = request.get_json(silent=True) or {}
    file_url = body.get("file_url")
    metadata = body.get("metadata") or {}

    if not file_url:
        return (
            jsonify({"status": "error", "error": "file_url_required"}),
            400,
        )

    sess = JFSession.query.filter_by(session_id=session_id).first()
    if not sess:
        # Auto-create a bare session if needed – keeps the pipeline simple.
        sess = JFSession(
            session_id=session_id,
            source="media",
            status="new",
        )
        db.session.add(sess)
        db.session.flush()

    media = JFMedia(
        session_id=session_id,
        media_type=media_type,
        file_url=file_url,
        media_metadata=metadata,
    )
    db.session.add(media)
    db.session.commit()

    return jsonify({"status": "ok", "media": media_to_dict(media)})


@app.route("/api/sessions/<string:session_id>/image", methods=["POST"])
def add_image_media(session_id):
    return _create_media_record(session_id, "image")


@app.route("/api/sessions/<string:session_id>/audio", methods=["POST"])
def add_audio_media(session_id):
    return _create_media_record(session_id, "audio")


# ----------------------------------------------------------------------
# Entrypoint for local debug
# ----------------------------------------------------------------------

if __name__ == "__main__":
    # Local dev server – Render will use gunicorn jobflow_api:app
    port = int(os.environ.get("PORT", "10000"))
    print(f"[LOCAL] Starting JobFlow Cloud Mini API on http://127.0.0.1:{port}")
    app.run(host="0.0.0.0", port=port, debug=True)
