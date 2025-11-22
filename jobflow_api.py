import os
import uuid
import datetime as dt
from urllib.parse import urljoin

from flask import Flask, jsonify, request, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func
from werkzeug.utils import secure_filename

# ============================================================
# Basic Flask + DB setup
# ============================================================

app = Flask(__name__)

# DATABASE_URL from Render; fall back to local sqlite for dev
DATABASE_URL = os.getenv("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    # Render's Postgres URLs are postgres://; SQLAlchemy wants postgresql://
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

if not DATABASE_URL:
    DATABASE_URL = "sqlite:///jobflow_local.db"

app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# Where to store uploaded media (ephemeral on Render, fine for now)
MEDIA_ROOT = os.getenv("MEDIA_ROOT", "/opt/render/project/src/media")
os.makedirs(MEDIA_ROOT, exist_ok=True)

db = SQLAlchemy(app)


# ============================================================
# Models (v2 tables so we don't clash with old schema)
# ============================================================

class Session(db.Model):
    __tablename__ = "sessions_v2"

    id = db.Column(db.Integer, primary_key=True)
    sid = db.Column(db.String(255), unique=True, nullable=False, index=True)
    payload = db.Column(db.JSON, nullable=True)

    created_at = db.Column(
        db.DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    def to_dict(self, include_payload: bool = True) -> dict:
        base = {
            "id": self.id,
            "sid": self.sid,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_payload:
            base["payload"] = self.payload
        else:
            payload = self.payload or {}
            base["client_name"] = payload.get("client_name")
            base["job_type"] = payload.get("job_type")
            base["summary"] = payload.get("summary") or payload.get("title")
        return base


class Media(db.Model):
    __tablename__ = "media_v2"

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(
        db.Integer,
        db.ForeignKey("sessions_v2.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sid = db.Column(db.String(255), index=True, nullable=False)

    kind = db.Column(db.String(50), nullable=False)  # "image" or "audio"
    url = db.Column(db.String(1024), nullable=False)
    filename = db.Column(db.String(255), nullable=True)
    mime_type = db.Column(db.String(255), nullable=True)

    created_at = db.Column(
        db.DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    session = db.relationship("Session", backref="media_items")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "sid": self.sid,
            "session_id": self.session_id,
            "kind": self.kind,
            "url": self.url,
            "filename": self.filename,
            "mime_type": self.mime_type,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


# ============================================================
# Create tables if they don't exist
# ============================================================

with app.app_context():
    db.create_all()


# ============================================================
# Helper functions
# ============================================================

def get_or_create_session(sid: str, payload: dict | None = None) -> Session:
    """Fetch a Session by sid, or create it. Optionally merge payload."""
    sess = Session.query.filter_by(sid=sid).one_or_none()
    if sess is None:
        sess = Session(sid=sid, payload=payload or {})
        db.session.add(sess)
    else:
        if payload:
            base = sess.payload or {}
            base.update(payload)
            sess.payload = base
    return sess


def build_media_url(rel_path: str) -> str:
    """Construct a public-ish URL path for media (served by /media/<path>)."""
    return urljoin("/media/", rel_path.replace("\\", "/"))


def save_uploaded_file(file_storage, sid: str, kind: str) -> Media:
    """Save an uploaded file under MEDIA_ROOT/<sid>/ and create a Media row."""
    safe_sid = secure_filename(sid)
    session_dir = os.path.join(MEDIA_ROOT, safe_sid)
    os.makedirs(session_dir, exist_ok=True)

    original_name = secure_filename(file_storage.filename or f"{kind}.bin")
    unique_suffix = uuid.uuid4().hex[:8]
    filename = f"{dt.datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{unique_suffix}_{original_name}"
    filepath = os.path.join(session_dir, filename)

    file_storage.save(filepath)

    rel_path = f"{safe_sid}/{filename}"
    url = build_media_url(rel_path)

    sess = get_or_create_session(sid)
    db.session.flush()  # ensure sess.id is available

    media = Media(
        session_id=sess.id,
        sid=sid,
        kind=kind,
        url=url,
        filename=filename,
        mime_type=file_storage.mimetype,
    )
    db.session.add(media)
    db.session.commit()

    return media


def _get_upload_file():
    """Support multiple field names: file, image, audio."""
    if "file" in request.files:
        return request.files["file"]
    if "image" in request.files:
        return request.files["image"]
    if "audio" in request.files:
        return request.files["audio"]
    return None


# ============================================================
# Root + Health
# ============================================================

@app.get("/")
def root():
    return jsonify({"ok": True, "message": "JobFlow Cloud Mini API root"})


@app.get("/api/health")
def health():
    return jsonify(
        {
            "ok": True,
            "service": "jobflow-cloud-mini",
            "time": dt.datetime.utcnow().isoformat() + "Z",
        }
    )


# ============================================================
# Session upsert endpoints
# ============================================================

@app.post("/api/sessions/<sid>/upsert")
def upsert_session(sid):
    if not sid:
        return jsonify({"ok": False, "error": "Missing sid"}), 400

    payload = request.get_json(silent=True) or {}
    sess = get_or_create_session(sid, payload=payload)
    db.session.commit()

    return jsonify({"ok": True, "session": sess.to_dict(include_payload=True)}), 200


@app.post("/api/sessions/<sid>")
def upsert_session_legacy(sid):
    # Backwards-compatible with older clients
    return upsert_session(sid)


# ============================================================
# Media upload endpoints
# ============================================================

@app.post("/api/sessions/<sid>/image")
def upload_image(sid):
    file = _get_upload_file()
    if not file:
        return jsonify({"ok": False, "error": "No image file provided"}), 400

    media = save_uploaded_file(file, sid, kind="image")
    return jsonify({"ok": True, "media": media.to_dict()}), 200


@app.post("/api/sessions/<sid>/audio")
def upload_audio(sid):
    file = _get_upload_file()
    if not file:
        return jsonify({"ok": False, "error": "No audio file provided"}), 400

    media = save_uploaded_file(file, sid, kind="audio")
    return jsonify({"ok": True, "media": media.to_dict()}), 200


@app.get("/media/<path:subpath>")
def serve_media(subpath):
    # Note: this is fine for admin/dev. For production, you’d typically use S3.
    return send_from_directory(MEDIA_ROOT, subpath)


# ============================================================
# Phase 1 – Read endpoints
# ============================================================

@app.get("/api/sessions")
def list_sessions():
    """Return a paginated list of sessions (summary only)."""
    try:
        limit = int(request.args.get("limit", "50"))
    except ValueError:
        limit = 50
    limit = max(1, min(limit, 200))

    try:
        offset = int(request.args.get("offset", "0"))
    except ValueError:
        offset = 0
    offset = max(0, offset)

    q = (
        Session.query
        .order_by(Session.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    items = [s.to_dict(include_payload=False) for s in q]

    total = db.session.query(func.count(Session.id)).scalar() or 0

    return jsonify(
        {
            "ok": True,
            "total": total,
            "limit": limit,
            "offset": offset,
            "sessions": items,
        }
    )


@app.get("/api/sessions/<sid>")
def get_session_detail(sid):
    """Return full session payload plus media count."""
    sess = Session.query.filter_by(sid=sid).one_or_none()
    if not sess:
        return jsonify({"ok": False, "error": "Session not found"}), 404

    media_q = Media.query.filter_by(session_id=sess.id)
    media_count = media_q.count()

    data = sess.to_dict(include_payload=True)
    data["media_count"] = media_count

    return jsonify({"ok": True, "session": data})


@app.get("/api/sessions/<sid>/media")
def get_session_media(sid):
    """Return all media rows for a given session sid."""
    sess = Session.query.filter_by(sid=sid).one_or_none()
    if not sess:
        return jsonify({"ok": False, "error": "Session not found"}), 404

    media_q = Media.query.filter_by(session_id=sess.id).order_by(Media.created_at.asc())
    media_items = [m.to_dict() for m in media_q]

    return jsonify(
        {
            "ok": True,
            "sid": sid,
            "session_id": sess.id,
            "media": media_items,
        }
    )


# ============================================================
# Main (local dev)
# ============================================================

if __name__ == "__main__":
    # Local dev: python jobflow_api.py
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")), debug=True)
