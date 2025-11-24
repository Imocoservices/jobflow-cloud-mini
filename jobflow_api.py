import os
import uuid
from datetime import datetime

from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.dialects.postgresql import JSONB
from dotenv import load_dotenv

# -------------------------------------------------
# Environment / App setup
# -------------------------------------------------
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL") or os.getenv("EXTERNAL_DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL or EXTERNAL_DATABASE_URL must be set")

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

CORS(app, resources={r"/api/*": {"origins": "*"}})

db = SQLAlchemy(app)


# -------------------------------------------------
# Models (aligned to jf_sessions / jf_media tables)
# -------------------------------------------------
class JFSession(db.Model):
    __tablename__ = "jf_sessions"

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.String(128), unique=True, index=True, nullable=False)

    title = db.Column(db.String(255))
    client_name = db.Column(db.String(255))
    source = db.Column(db.String(64))
    status = db.Column(db.String(64))

    payload = db.Column(JSONB)  # full JSON payload (meta, analysis, etc.)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    media = db.relationship(
        "JFMedia",
        primaryjoin="JFSession.session_id==JFMedia.session_id",
        backref="session",
        lazy="select",
    )


class JFMedia(db.Model):
    __tablename__ = "jf_media"

    id = db.Column(db.Integer, primary_key=True)

    # NOTE: this uses session_id (string FK), matching how we upsert sessions
    session_id = db.Column(
        db.String(128), db.ForeignKey("jf_sessions.session_id"), index=True, nullable=False
    )

    media_type = db.Column(db.String(32))  # "image", "audio", "video", etc.
    uri = db.Column(db.String(512))       # path or URL to file
    note = db.Column(db.Text)
    extra = db.Column(JSONB)              # optional extra metadata

    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# -------------------------------------------------
# Serialization helpers
# -------------------------------------------------
def serialize_media(m: JFMedia) -> dict:
    return {
        "id": m.id,
        "session_id": m.session_id,
        "media_type": m.media_type,
        "file_path": m.uri,        # expose as file_path to clients
        "note": m.note,
        "extra": m.extra or {},
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }


def serialize_session(s: JFSession, include_media: bool = False) -> dict:
    data = {
        "id": s.id,
        "session_id": s.session_id,
        "title": s.title,
        "client_name": s.client_name,
        "source": s.source,
        "status": s.status,
        "payload": s.payload or {},
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
    }

    # keep existing "meta" behaviour from your upsert body
    meta = (s.payload or {}).get("meta") if s.payload else None
    if meta:
        data.setdefault("title", meta.get("title") or data.get("title"))
        data.setdefault("client_name", meta.get("client_name") or data.get("client_name"))
        data.setdefault("source", meta.get("source") or data.get("source"))
        data.setdefault("status", meta.get("status") or data.get("status"))

    if include_media:
        data["media"] = [serialize_media(m) for m in s.media]

    return data


# -------------------------------------------------
# Core API
# -------------------------------------------------
@app.get("/api/health")
def health():
    return jsonify({"status": "ok"})


# ---------- Sessions list ----------
@app.get("/api/sessions")
def list_sessions():
    try:
        limit = int(request.args.get("limit", 20))
    except ValueError:
        limit = 20

    q = JFSession.query.order_by(JFSession.created_at.desc()).limit(limit)
    sessions = [serialize_session(s) for s in q.all()]

    return jsonify(
        {
            "status": "ok",
            "count": len(sessions),
            "sessions": sessions,
        }
    )


# ---------- Session upsert ----------
@app.post("/api/sessions/<sid>/upsert")
def upsert_session(sid):
    """
    Body you’re already using:

    {
      "meta": {
        "title": "Test Job",
        "client_name": "Cloud",
        "source": "bulk",
        "status": "new"
      }
    }
    """
    payload = request.get_json(silent=True) or {}
    meta = payload.get("meta") or {}

    title = meta.get("title")
    client_name = meta.get("client_name")
    source = meta.get("source")
    status = meta.get("status")

    session = JFSession.query.filter_by(session_id=sid).first()

    if session is None:
        session = JFSession(
            session_id=sid,
            title=title,
            client_name=client_name,
            source=source,
            status=status,
            payload=payload,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.session.add(session)
    else:
        # update basic fields but do NOT blow away existing payload unless new payload provided
        if title:
            session.title = title
        if client_name:
            session.client_name = client_name
        if source:
            session.source = source
        if status:
            session.status = status

        # keep last payload version
        if payload:
            session.payload = payload

        session.updated_at = datetime.utcnow()

    db.session.commit()

    return jsonify(
        {
            "status": "ok",
            "session": serialize_session(session),
        }
    )


# ---------- Session detail (with media) ----------
@app.get("/api/sessions/<sid>")
def get_session_detail(sid):
    session = JFSession.query.filter_by(session_id=sid).first()
    if not session:
        return jsonify({"status": "error", "message": "Session not found"}), 404

    return jsonify(
        {
            "status": "ok",
            "session": serialize_session(session, include_media=True),
        }
    )


# ---------- Media upload ----------
@app.post("/api/sessions/<sid>/media")
def upload_media(sid):
    """
    Expects a multipart/form-data request like:

    file       = (binary)
    media_type = "image" | "audio" | ...
    note       = "before picture"
    """
    session = JFSession.query.filter_by(session_id=sid).first()
    if not session:
        return jsonify({"status": "error", "message": "Session not found"}), 404

    if "file" not in request.files:
        return jsonify({"status": "error", "message": "No file provided"}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"status": "error", "message": "Empty filename"}), 400

    from werkzeug.utils import secure_filename

    media_type = request.form.get("media_type", "image")
    note = request.form.get("note", "")

    filename = secure_filename(file.filename)
    ext = os.path.splitext(filename)[1]  # includes dot
    unique_name = f"{uuid.uuid4()}{ext}"

    # save under ./media/<session_id>/
    base_dir = os.path.join(os.path.dirname(__file__), "media", sid)
    os.makedirs(base_dir, exist_ok=True)

    save_path = os.path.join(base_dir, unique_name)
    file.save(save_path)

    rel_path = os.path.relpath(save_path, os.path.dirname(__file__))

    media = JFMedia(
        session_id=sid,
        media_type=media_type,
        uri=rel_path,  # store relative path in DB
        note=note,
        extra={"original_filename": filename},
        created_at=datetime.utcnow(),
    )

    db.session.add(media)
    db.session.commit()

    return jsonify(
        {
            "status": "ok",
            "media": serialize_media(media),
        }
    )


# ---------- Optional: media list for a session ----------
@app.get("/api/sessions/<sid>/media")
def list_media(sid):
    session = JFSession.query.filter_by(session_id=sid).first()
    if not session:
        return jsonify({"status": "error", "message": "Session not found"}), 404

    media_items = [serialize_media(m) for m in session.media]

    return jsonify(
        {
            "status": "ok",
            "count": len(media_items),
            "media": media_items,
        }
    )


# -------------------------------------------------
# Root helper
# -------------------------------------------------
@app.get("/")
def root():
    return jsonify({"status": "ok", "message": "JobFlow Cloud Mini API"})


if __name__ == "__main__":
    # Local dev
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=True)
