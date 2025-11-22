import os
import uuid
from datetime import datetime

from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy import text

# ---------------------------------------------------------------------
# App / DB setup
# ---------------------------------------------------------------------

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")

app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)


# ---------------------------------------------------------------------
# Models – keep schema *simple* and stable
# ---------------------------------------------------------------------

class Session(db.Model):
    __tablename__ = "sessions"

    id = db.Column(db.Integer, primary_key=True)
    # External session ID like "jobflow-20251121_211656"
    sid = db.Column(db.String(128), unique=True, nullable=False, index=True)

    # Arbitrary JSON payload (client_name, job_type, notes, analysis, etc.)
    payload = db.Column(JSONB, nullable=True)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow,
                           onupdate=datetime.utcnow)

    media = db.relationship(
        "Media",
        backref="session",
        lazy=True,
        cascade="all, delete-orphan",
    )

    def to_dict(self, include_payload=True, include_media_counts=True):
        data = {
            "id": self.id,
            "sid": self.sid,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_payload:
            data["payload"] = self.payload or {}
        if include_media_counts:
            images = sum(1 for m in self.media if m.kind == "image")
            audio = sum(1 for m in self.media if m.kind == "audio")
            data["media_counts"] = {
                "total": len(self.media),
                "images": images,
                "audio": audio,
            }
        return data


class Media(db.Model):
    __tablename__ = "media"

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(
        db.Integer,
        db.ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    kind = db.Column(db.String(16), nullable=False)  # "image" or "audio"
    filename = db.Column(db.String(255), nullable=False)
    mime_type = db.Column(db.String(128), nullable=True)
    size_bytes = db.Column(db.Integer, nullable=True)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "session_id": self.session_id,
            "kind": self.kind,
            "filename": self.filename,
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


# ---------------------------------------------------------------------
# One-time schema safety – fix old tables instead of breaking them
# ---------------------------------------------------------------------

def ensure_schema():
    """
    Make sure the minimal tables/columns we rely on actually exist.
    We *don’t* drop anything – only CREATE / ALTER IF NOT EXISTS.
    This avoids the 'column sessions.sid does not exist' nonsense.
    """
    with app.app_context():
        engine = db.engine

        # sessions table
        engine.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id          SERIAL PRIMARY KEY,
                    sid         VARCHAR(128) UNIQUE NOT NULL,
                    payload     JSONB,
                    created_at  TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW(),
                    updated_at  TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW()
                );
                """
            )
        )
        # Add sid column if an old table exists without it
        engine.execute(
            text("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS sid VARCHAR(128);")
        )
        engine.execute(
            text("CREATE UNIQUE INDEX IF NOT EXISTS ix_sessions_sid ON sessions (sid);")
        )

        # media table
        engine.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS media (
                    id          SERIAL PRIMARY KEY,
                    session_id  INTEGER NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
                    kind        VARCHAR(16) NOT NULL,
                    filename    VARCHAR(255) NOT NULL,
                    mime_type   VARCHAR(128),
                    size_bytes  INTEGER,
                    created_at  TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW()
                );
                """
            )
        )
        engine.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_media_session_id ON media (session_id);"
            )
        )


ensure_schema()


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def get_or_create_session(sid: str) -> Session:
    s = Session.query.filter_by(sid=sid).first()
    if s:
        return s

    s = Session(sid=sid, payload={"sid": sid, "external_id": sid})
    db.session.add(s)
    db.session.commit()
    return s


def save_uploaded_file(file_storage, subdir: str) -> str:
    """
    Save an uploaded file under ./uploads/<subdir>/<random>_<filename>
    and return just the relative path we store in DB.
    """
    upload_root = os.path.join(os.path.dirname(__file__), "uploads")
    target_dir = os.path.join(upload_root, subdir)
    os.makedirs(target_dir, exist_ok=True)

    original = file_storage.filename or "file.bin"
    safe_name = f"{uuid.uuid4().hex}_{os.path.basename(original)}"
    full_path = os.path.join(target_dir, safe_name)

    file_storage.save(full_path)

    # what we store in DB
    rel_path = os.path.join(subdir, safe_name).replace("\\", "/")
    return rel_path


# ---------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify(
        {
            "ok": True,
            "service": "jobflow-cloud-mini",
            "time": datetime.utcnow().isoformat() + "Z",
        }
    )


@app.route("/api/sessions", methods=["GET"])
def list_sessions():
    try:
        limit = int(request.args.get("limit", 20))
    except ValueError:
        limit = 20
    limit = max(1, min(limit, 100))

    try:
        offset = int(request.args.get("offset", 0))
    except ValueError:
        offset = 0
    offset = max(0, offset)

    q = Session.query.order_by(Session.created_at.desc())
    total = q.count()
    items = q.offset(offset).limit(limit).all()

    return jsonify(
        {
            "ok": True,
            "limit": limit,
            "offset": offset,
            "total": total,
            "sessions": [s.to_dict(include_payload=False) for s in items],
        }
    )


@app.route("/api/sessions/<sid>", methods=["GET"])
def get_session(sid):
    s = Session.query.filter_by(sid=sid).first()
    if not s:
        return jsonify({"ok": False, "error": "session_not_found"}), 404
    return jsonify({"ok": True, "session": s.to_dict()})


@app.route("/api/sessions/<sid>/media", methods=["GET"])
def list_media(sid):
    s = Session.query.filter_by(sid=sid).first()
    if not s:
        return jsonify({"ok": False, "error": "session_not_found"}), 404

    return jsonify({"ok": True, "media": [m.to_dict() for m in s.media]})


@app.route("/api/sessions/<sid>/upsert", methods=["POST"])
def upsert_session(sid):
    """
    Upsert session metadata. Body is JSON and gets merged into payload.
    """
    data = request.get_json(silent=True) or {}

    s = get_or_create_session(sid)

    payload = s.payload or {}
    payload.update(data)
    payload.setdefault("sid", sid)
    payload.setdefault("external_id", sid)

    s.payload = payload
    s.updated_at = datetime.utcnow()

    db.session.add(s)
    db.session.commit()

    return jsonify({"ok": True, "session": s.to_dict()})


@app.route("/api/sessions/<sid>/image", methods=["POST"])
def upload_image(sid):
    s = get_or_create_session(sid)

    if "file" not in request.files:
        return jsonify({"ok": False, "error": "missing_file"}), 400

    f = request.files["file"]
    rel_path = save_uploaded_file(f, sid)

    media = Media(
        session_id=s.id,
        kind="image",
        filename=rel_path,
        mime_type=f.mimetype,
        size_bytes=len(f.read()) if hasattr(f, "read") else None,
    )
    # If we read(), file pointer is at end – rewind for consistency
    try:
        f.seek(0)
    except Exception:
        pass

    db.session.add(media)
    db.session.commit()

    return jsonify({"ok": True, "media": media.to_dict()})


@app.route("/api/sessions/<sid>/audio", methods=["POST"])
def upload_audio(sid):
    s = get_or_create_session(sid)

    if "file" not in request.files:
        return jsonify({"ok": False, "error": "missing_file"}), 400

    f = request.files["file"]
    rel_path = save_uploaded_file(f, sid)

    media = Media(
        session_id=s.id,
        kind="audio",
        filename=rel_path,
        mime_type=f.mimetype,
        size_bytes=len(f.read()) if hasattr(f, "read") else None,
    )
    try:
        f.seek(0)
    except Exception:
        pass

    db.session.add(media)
    db.session.commit()

    return jsonify({"ok": True, "media": media.to_dict()})


@app.route("/api/sessions/<sid>/analyze", methods=["POST"])
def analyze_session(sid):
    """
    Lightweight "auto analyze" so your bulk_upload_with_analyze.ps1 succeeds.

    For now this does NOT call OpenAI – it just writes a structured
    placeholder into payload["analysis"] so the cloud side is stable.
    We’ll wire this into your real AI pipeline later.
    """
    s = Session.query.filter_by(sid=sid).first()
    if not s:
        return jsonify({"ok": False, "error": "session_not_found"}), 404

    payload = s.payload or {}
    images = [m for m in s.media if m.kind == "image"]
    audio = [m for m in s.media if m.kind == "audio"]

    analysis = {
        "status": "done",
        "source": "cloud_placeholder",
        "summary": (
            "Session is ready for JobFlow AI. "
            f"{len(images)} photos and {len(audio)} audio files are attached. "
            "Your local bot can pull this session, transcribe audio, "
            "and build a detailed quote."
        ),
        "meta": {
            "num_media": len(s.media),
            "num_images": len(images),
            "num_audio": len(audio),
        },
        "updated_at": datetime.utcnow().isoformat() + "Z",
    }

    payload["analysis"] = analysis
    s.payload = payload
    s.updated_at = datetime.utcnow()

    db.session.add(s)
    db.session.commit()

    return jsonify({"ok": True, "analysis": analysis})


# ---------------------------------------------------------------------
# WSGI entrypoint
# ---------------------------------------------------------------------

if __name__ == "__main__":
    # Local debug only – Render runs gunicorn with jobflow_api:app
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
