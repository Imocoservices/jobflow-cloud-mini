import os
import uuid
from datetime import datetime

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.dialects.postgresql import JSONB

# =========================================================
# App + DB setup
# =========================================================

app = Flask(__name__)

# Database URL (Render will inject DATABASE_URL)
DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    # Local fallback for dev only
    DATABASE_URL = "sqlite:///jobflow_local.db"

app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_pre_ping": True,
}

db = SQLAlchemy(app)
CORS(app, resources={r"/api/*": {"origins": "*"}})

# Directory for uploaded files (ephemeral on Render, fine for demo)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)


# =========================================================
# Models
# =========================================================

class JFSession(db.Model):
    """
    High-level session record for a job.
    Mirrors the JSON you already saw in /api/sessions list.
    """
    __tablename__ = "jf_sessions"

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.String(255), unique=True, nullable=False, index=True)

    # Convenience fields for filtering and display
    client_name = db.Column(db.String(255))
    title = db.Column(db.String(255))
    source = db.Column(db.String(64))
    status = db.Column(db.String(64))

    # Raw JSON payload (we store your "meta" object in here)
    payload = db.Column(JSONB, nullable=False, default=dict)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    def to_dict(self):
        return {
            "id": self.id,
            "session_id": self.session_id,
            "client_name": self.client_name,
            "title": self.title,
            "source": self.source,
            "status": self.status,
            "payload": self.payload or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class JFMedia(db.Model):
    """
    Media attached to a session: images, audio, video, etc.
    We link by session_id (string) to stay simple and migration-safe.
    """
    __tablename__ = "jf_media"

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.String(255), nullable=False, index=True)

    media_type = db.Column(db.String(32), nullable=False)  # image / audio / video / other
    uri = db.Column(db.String(1024), nullable=False)       # local path or URL

    mime_type = db.Column(db.String(128))
    label = db.Column(db.String(255))
    note = db.Column(db.String(1024))

    extra = db.Column(JSONB)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "session_id": self.session_id,
            "media_type": self.media_type,
            "uri": self.uri,
            "mime_type": self.mime_type,
            "label": self.label,
            "note": self.note,
            "extra": self.extra or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


# =========================================================
# DB init
# =========================================================

def ensure_schema():
    """Create tables if they don't exist."""
    with app.app_context():
        db.create_all()


ensure_schema()


# =========================================================
# Helpers
# =========================================================

def api_error(message, status_code=400, details=None):
    payload = {"status": "error", "error": message}
    if details is not None:
        payload["details"] = details
    return jsonify(payload), status_code


# =========================================================
# Routes
# =========================================================

@app.route("/")
def index():
    return "JobFlow Cloud Mini API", 200


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


# ---------------------------
# Sessions
# ---------------------------

@app.route("/api/sessions", methods=["GET"])
def list_sessions():
    """
    GET /api/sessions?limit=20
    Returns:
    {
      "status": "ok",
      "count": N,
      "sessions": [ ... ]
    }
    """
    try:
        limit = request.args.get("limit", default=20, type=int)
        if limit <= 0 or limit > 100:
            limit = 20

        rows = (
            JFSession.query
            .order_by(JFSession.created_at.desc())
            .limit(limit)
            .all()
        )

        return jsonify({
            "status": "ok",
            "count": len(rows),
            "sessions": [s.to_dict() for s in rows],
        })
    except Exception as exc:
        return api_error("failed_to_list_sessions", 500, str(exc))


@app.route("/api/sessions/<session_id>/upsert", methods=["POST"])
def upsert_session(session_id):
    """
    POST /api/sessions/<sid>/upsert
    Body (JSON):
      { "meta": { "title": "...", "client_name": "...", "source": "...", "status": "..." } }

    This is the same structure you've already been calling from PowerShell.
    """
    try:
        data = request.get_json(silent=True) or {}
        meta = data.get("meta") or data  # allow them to send meta or flat

        title = meta.get("title")
        client_name = meta.get("client_name")
        source = meta.get("source") or "bulk"
        status = meta.get("status") or "new"

        # Look up existing session
        session = JFSession.query.filter_by(session_id=session_id).first()
        if not session:
            session = JFSession(
                session_id=session_id,
                title=title,
                client_name=client_name,
                source=source,
                status=status,
                payload={"meta": meta},
            )
            db.session.add(session)
        else:
            session.title = title or session.title
            session.client_name = client_name or session.client_name
            session.source = source or session.source
            session.status = status or session.status

            # keep full meta in payload
            session.payload = session.payload or {}
            session.payload["meta"] = meta

        db.session.commit()

        return jsonify({
            "status": "ok",
            "session": session.to_dict(),
        })
    except Exception as exc:
        db.session.rollback()
        return api_error("failed_to_upsert_session", 500, str(exc))


@app.route("/api/sessions/<session_id>", methods=["GET"])
def get_session_detail(session_id):
    """
    GET /api/sessions/<sid>
    Returns one session plus its media array.
    {
      "status": "ok",
      "session": {
        ...,
        "media": [ ... ]
      }
    }
    """
    try:
        session = JFSession.query.filter_by(session_id=session_id).first()
        if not session:
            return api_error("session_not_found", 404)

        media_rows = (
            JFMedia.query
            .filter_by(session_id=session_id)
            .order_by(JFMedia.created_at.asc())
            .all()
        )

        data = session.to_dict()
        data["media"] = [m.to_dict() for m in media_rows]

        return jsonify({"status": "ok", "session": data})
    except Exception as exc:
        return api_error("failed_to_fetch_session", 500, str(exc))


# ---------------------------
# Media
# ---------------------------

@app.route("/api/sessions/<session_id>/media", methods=["GET", "POST"])
def session_media(session_id):
    """
    POST /api/sessions/<sid>/media

    Two modes:

    1) JSON (metadata only)
       Content-Type: application/json
       {
         "media_type": "image" | "audio" | "video" | "other",
         "uri": "https://... or local path",
         "extra": { "note": "before picture", ... }
       }

    2) Multipart with file upload
       Content-Type: multipart/form-data
       Fields:
         file: (binary)
         media_type: image/audio/video/other (optional, default "image")
         note: optional text
         label: optional label

       -> Saved under /uploads/<generated_name>
       -> uri stored as "/uploads/<generated_name>"

    GET /api/sessions/<sid>/media
      Returns:
      {
        "status": "ok",
        "count": N,
        "media": [ ... ]
      }
    """
    # Ensure session exists (soft check)
    session = JFSession.query.filter_by(session_id=session_id).first()
    if not session:
        return api_error("session_not_found", 404)

    if request.method == "GET":
        try:
            rows = (
                JFMedia.query
                .filter_by(session_id=session_id)
                .order_by(JFMedia.created_at.asc())
                .all()
            )
            return jsonify({
                "status": "ok",
                "count": len(rows),
                "media": [m.to_dict() for m in rows],
            })
        except Exception as exc:
            return api_error("failed_to_list_media", 500, str(exc))

    # POST: create media
    try:
        content_type = request.content_type or ""

        # --- JSON MODE (metadata-only) ---
        if content_type.startswith("application/json"):
            body = request.get_json(silent=True) or {}
            media_type = body.get("media_type") or "other"
            uri = body.get("uri")
            extra = body.get("extra") or {}

            if not uri:
                return api_error("uri_required_for_json_media", 400)

            note = extra.get("note")

            media = JFMedia(
                session_id=session_id,
                media_type=media_type,
                uri=uri,
                mime_type=None,
                label=extra.get("label"),
                note=note,
                extra=extra,
            )
            db.session.add(media)
            db.session.commit()

            return jsonify({"status": "ok", "media": media.to_dict()}), 201

        # --- MULTIPART MODE (file upload) ---
        file = request.files.get("file")
        if not file:
            return api_error("file_required_for_multipart_media", 400)

        media_type = request.form.get("media_type") or "image"
        note = request.form.get("note")
        label = request.form.get("label")

        # Generate safe filename
        original_name = file.filename or ""
        _, ext = os.path.splitext(original_name)
        ext = ext or ".bin"

        unique_name = f"{session_id}_{uuid.uuid4().hex}{ext}"
        save_path = os.path.join(UPLOAD_DIR, unique_name)
        file.save(save_path)

        # URI that clients can hit (served by /uploads/<name> below)
        uri = f"/uploads/{unique_name}"

        media = JFMedia(
            session_id=session_id,
            media_type=media_type,
            uri=uri,
            mime_type=file.mimetype,
            label=label,
            note=note,
            extra={"original_name": original_name},
        )
        db.session.add(media)
        db.session.commit()

        return jsonify({"status": "ok", "media": media.to_dict()}), 201

    except Exception as exc:
        db.session.rollback()
        return api_error("failed_to_create_media", 500, str(exc))


# ---------------------------
# Serve uploaded files
# ---------------------------

@app.route("/uploads/<path:filename>", methods=["GET"])
def serve_upload(filename):
    """
    Simple static file server for uploaded media.
    NOTE: On Render, this is ephemeral storage – good enough for demo/testing.
    Later, move to S3 or similar and just store URLs in JFMedia.uri.
    """
    return send_from_directory(UPLOAD_DIR, filename)


# =========================================================
# Entry point
# =========================================================

if __name__ == "__main__":
    # Local dev only
    port = int(os.getenv("PORT", "5065"))
    app.run(host="0.0.0.0", port=port, debug=True)
