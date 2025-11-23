import os
import uuid
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text

# =============================================================================
# App + DB setup
# =============================================================================

app = Flask(__name__)

# --- Database URL ---
# Prefer DATABASE_URL (Render-style), fall back to SQLite for local dev
db_url = os.getenv("DATABASE_URL") or "sqlite:///jobflow_cloud.db"

# Render sometimes uses postgres://; SQLAlchemy wants postgresql://
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)
CORS(app, resources={r"/api/*": {"origins": "*"}})

# Where to store uploaded media files (works on Render + local)
BASE_DIR = Path(__file__).resolve().parent
UPLOAD_ROOT = BASE_DIR / "uploads"
UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)


# =============================================================================
# Models  (NEW TABLE NAMES: jf_sessions / jf_media)
# We completely ignore the old "sessions" table that had bad constraints.
# =============================================================================

class JFSession(db.Model):
    __tablename__ = "jf_sessions"

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.String(128), unique=True, nullable=False, index=True)

    # Simple metadata fields for dashboard
    title = db.Column(db.String(255))
    client_name = db.Column(db.String(255))
    source = db.Column(db.String(64))
    status = db.Column(db.String(64))

    # Arbitrary JSON payload (meta, analysis, quote, etc.)
    payload = db.Column(db.JSON, nullable=False, default=dict)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )


class JFMedia(db.Model):
    __tablename__ = "jf_media"

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(
        db.Integer,
        db.ForeignKey("jf_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    media_type = db.Column(db.String(16), nullable=False)  # "image" or "audio"
    filename = db.Column(db.String(512), nullable=False)
    content_type = db.Column(db.String(128))
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    session = db.relationship("JFSession", backref=db.backref("media", lazy=True))


# =============================================================================
# Schema init
# =============================================================================

def ensure_schema():
    """Create our new tables jf_sessions/jf_media if they don't exist."""
    with app.app_context():
        db.create_all()
        try:
            # optional: index on created_at for faster listing
            db.session.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_jf_sessions_created_at "
                    "ON jf_sessions (created_at DESC)"
                )
            )
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            print(f"[INIT_DB] Index creation skipped: {e}")
        print("[INIT_DB] Schema ready for jf_sessions / jf_media")


ensure_schema()


# =============================================================================
# Helpers
# =============================================================================

def session_to_dict(s: JFSession):
    return {
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


def get_or_create_session_by_sid(session_id: str, meta: dict | None = None) -> JFSession:
    """Find a session by session_id, or create a new one with optional meta."""
    if meta is None:
        meta = {}

    s = JFSession.query.filter_by(session_id=session_id).first()
    if not s:
        s = JFSession(
            session_id=session_id,
            title=meta.get("title"),
            client_name=meta.get("client_name"),
            source=meta.get("source", "cloud"),
            status=meta.get("status", "new"),
            payload={"meta": meta} if meta else {},
        )
        db.session.add(s)
    else:
        # update lightweight metadata if given
        if meta:
            s.title = meta.get("title", s.title)
            s.client_name = meta.get("client_name", s.client_name)
            s.source = meta.get("source", s.source)
            s.status = meta.get("status", s.status)

            payload = s.payload or {}
            payload_meta = payload.get("meta", {})
            payload_meta.update(meta)
            payload["meta"] = payload_meta
            s.payload = payload

    return s


def save_upload_file(file_storage, subfolder: str) -> str:
    """Save an uploaded file into uploads/<subfolder>/ and return relative path."""
    safe_folder = UPLOAD_ROOT / subfolder
    safe_folder.mkdir(parents=True, exist_ok=True)

    # Generate a random filename while keeping original extension
    original_name = file_storage.filename or "upload"
    ext = ""
    if "." in original_name:
        ext = "." + original_name.rsplit(".", 1)[-1]

    new_name = f"{uuid.uuid4().hex}{ext}"
    dest = safe_folder / new_name
    file_storage.save(dest)

    rel_path = str(dest.relative_to(UPLOAD_ROOT))
    return rel_path


# =============================================================================
# Routes
# =============================================================================

@app.route("/")
def index():
    return "JobFlow Cloud Mini API", 200


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


# ----------------- Sessions list -----------------

@app.route("/api/sessions", methods=["GET"])
def list_sessions():
    try:
        limit = int(request.args.get("limit", 20))
    except ValueError:
        limit = 20

    q = JFSession.query.order_by(JFSession.created_at.desc())
    sessions = q.limit(limit).all()
    data = [session_to_dict(s) for s in sessions]

    return jsonify({"status": "ok", "count": len(data), "sessions": data})


# ----------------- Session upsert -----------------

@app.route("/api/sessions/<string:sid>/upsert", methods=["POST", "PUT"])
def upsert_session(sid: str):
    """
    Create or update a session by session_id.

    Body can be:
      {
        "meta": { "title": "...", "client_name": "...", "source": "...", "status": "..." },
        "payload": { ... }   # optional extra data
      }
    or flat:
      {
        "title": "...",
        "client_name": "...",
        "source": "...",
        "status": "...",
        "payload": { ... }
      }
    """
    body = request.get_json(silent=True) or {}

    meta = body.get("meta", {})
    # allow flat style
    for key in ("title", "client_name", "source", "status"):
        if key in body and key not in meta:
            meta[key] = body[key]

    s = get_or_create_session_by_sid(sid, meta=meta)

    extra_payload = body.get("payload")
    if isinstance(extra_payload, dict):
        payload = s.payload or {}
        payload.update(extra_payload)
        s.payload = payload

    db.session.add(s)
    db.session.commit()

    return jsonify({"status": "ok", "session": session_to_dict(s)})


# ----------------- Media upload (image) -----------------

@app.route("/api/sessions/<string:sid>/image", methods=["POST"])
def upload_image(sid: str):
    if "file" not in request.files:
        return jsonify({"status": "error", "error": "No file field 'file'"}), 400

    file = request.files["file"]
    if not file.filename:
        return jsonify({"status": "error", "error": "Empty filename"}), 400

    # Ensure session exists
    s = get_or_create_session_by_sid(sid)
    db.session.add(s)
    db.session.flush()  # assign s.id

    rel_path = save_upload_file(file, subfolder=sid)

    media = JFMedia(
        session_id=s.id,
        media_type="image",
        filename=rel_path,
        content_type=file.mimetype,
    )
    db.session.add(media)
    db.session.commit()

    return jsonify(
        {
            "status": "ok",
            "session": session_to_dict(s),
            "media": {
                "id": media.id,
                "media_type": media.media_type,
                "filename": media.filename,
                "content_type": media.content_type,
            },
        }
    )


# ----------------- Media upload (audio) -----------------

@app.route("/api/sessions/<string:sid>/audio", methods=["POST"])
def upload_audio(sid: str):
    if "file" not in request.files:
        return jsonify({"status": "error", "error": "No file field 'file'"}), 400

    file = request.files["file"]
    if not file.filename:
        return jsonify({"status": "error", "error": "Empty filename"}), 400

    s = get_or_create_session_by_sid(sid)
    db.session.add(s)
    db.session.flush()

    rel_path = save_upload_file(file, subfolder=sid)

    media = JFMedia(
        session_id=s.id,
        media_type="audio",
        filename=rel_path,
        content_type=file.mimetype,
    )
    db.session.add(media)
    db.session.commit()

    return jsonify(
        {
            "status": "ok",
            "session": session_to_dict(s),
            "media": {
                "id": media.id,
                "media_type": media.media_type,
                "filename": media.filename,
                "content_type": media.content_type,
            },
        }
    )


# ----------------- Serve uploaded files (optional helper) -----------------

@app.route("/uploads/<path:path>", methods=["GET"])
def serve_upload(path: str):
    """Debug helper to view raw files."""
    return send_from_directory(UPLOAD_ROOT, path)


# ----------------- Analyze (placeholder AI) -----------------

@app.route("/api/sessions/<string:sid>/analyze", methods=["POST"])
def analyze_session(sid: str):
    """
    Placeholder AI endpoint.

    For now it just writes a dummy analysis block into payload["analysis"].
    Later we can wire up OpenAI / Whisper here.
    """
    s = JFSession.query.filter_by(session_id=sid).first()
    if not s:
        return jsonify({"status": "error", "error": "Session not found"}), 404

    payload = s.payload or {}
    payload["analysis"] = {
        "summary": f"Placeholder analysis for session '{sid}'.",
        "notes": "AI pipeline not wired yet – this is a stub.",
        "updated_at": datetime.utcnow().isoformat(),
    }
    s.payload = payload
    s.status = s.status or "analyzed"

    db.session.add(s)
    db.session.commit()

    return jsonify({"status": "ok", "session": session_to_dict(s)})


# =============================================================================
# Main (local dev)
# =============================================================================

if __name__ == "__main__":
    # Local dev server
    print(f"[LOCAL] Using DB: {db_url}")
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
