import os
import uuid
import datetime as dt
from pathlib import Path

from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

# -------------------------------------------------------------------
# Basic Flask + DB setup
# -------------------------------------------------------------------

BASE_DIR = Path(__file__).parent

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

# Render gives DATABASE_URL, sometimes as postgres:// – normalize
raw_db_url = os.getenv("DATABASE_URL") or os.getenv("JOBFLOW_DB_URL")
if not raw_db_url:
    raise RuntimeError("DATABASE_URL or JOBFLOW_DB_URL must be set")

if raw_db_url.startswith("postgres://"):
    raw_db_url = raw_db_url.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = raw_db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)


# -------------------------------------------------------------------
# Models
# -------------------------------------------------------------------

class Session(db.Model):
    """
    Cloud session row.

    NOTE:
    - `id` is legacy primary key (serial).
    - `session_id` is the stable external ID we use in APIs.
    - `user_id` has a foreign key to users.id in the DB, BUT we
      deliberately keep it nullable and we DO NOT set it yet.
    """
    __tablename__ = "sessions"

    id = db.Column(db.Integer, primary_key=True)
    # FK exists in DB; leave it nullable so we don't need a real user yet
    user_id = db.Column(db.Integer, nullable=True)

    title = db.Column(db.String(255), nullable=True)
    client_name = db.Column(db.String(255), nullable=True)
    source = db.Column(db.String(64), nullable=True)
    status = db.Column(db.String(64), nullable=True)
    notes = db.Column(db.Text, nullable=True)

    # New-ish columns we actively use
    session_id = db.Column(db.String(128), index=True, nullable=True)
    external_id = db.Column(db.Integer, nullable=True)
    payload = db.Column(db.JSON, nullable=True)

    created_at = db.Column(db.DateTime, default=dt.datetime.utcnow)
    updated_at = db.Column(
        db.DateTime,
        default=dt.datetime.utcnow,
        onupdate=dt.datetime.utcnow,
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "title": self.title,
            "client_name": self.client_name,
            "source": self.source,
            "status": self.status,
            "notes": self.notes,
            "payload": self.payload or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class Media(db.Model):
    """
    Stored media file (image or audio) associated with a session.
    """
    __tablename__ = "media"

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.String(128), index=True, nullable=False)
    media_type = db.Column(db.String(16), nullable=False)  # "image" or "audio"
    filename = db.Column(db.String(512), nullable=False)
    mime_type = db.Column(db.String(128), nullable=True)
    size_bytes = db.Column(db.Integer, nullable=True)

    created_at = db.Column(db.DateTime, default=dt.datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "media_type": self.media_type,
            "filename": self.filename,
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


# -------------------------------------------------------------------
# One-shot schema / migration helper
# -------------------------------------------------------------------

def init_db():
    """
    Ensure tables exist and align legacy schema with the new model.

    We **do not** drop anything – only additive or relaxing changes.
    This is safe to run on every startup.
    """
    with app.app_context():
        engine = db.engine
        with engine.begin() as conn:
            migrations = [
                # Ensure session_id + payload + external_id columns exist
                (
                    "ALTER TABLE sessions "
                    "ADD COLUMN IF NOT EXISTS session_id VARCHAR(128)",
                    "ADD sessions.session_id",
                ),
                (
                    "ALTER TABLE sessions "
                    "ADD COLUMN IF NOT EXISTS payload JSON",
                    "ADD sessions.payload",
                ),
                (
                    "ALTER TABLE sessions "
                    "ADD COLUMN IF NOT EXISTS external_id INTEGER",
                    "ADD sessions.external_id",
                ),
                (
                    "CREATE INDEX IF NOT EXISTS ix_sessions_session_id "
                    "ON sessions (session_id)",
                    "INDEX sessions.session_id",
                ),
                # IMPORTANT: allow user_id to be NULL and no default
                (
                    "ALTER TABLE sessions ALTER COLUMN user_id DROP NOT NULL",
                    "DROP NOT NULL sessions.user_id",
                ),
                (
                    "ALTER TABLE sessions ALTER COLUMN user_id DROP DEFAULT",
                    "DROP DEFAULT sessions.user_id",
                ),
            ]

            for sql, label in migrations:
                try:
                    conn.execute(text(sql))
                    print(f"[INIT_DB] Ran migration: {label}")
                except ProgrammingError as e:
                    # Ignore if it's something like "column does not exist"
                    print(f"[INIT_DB] Migration skipped ({label}): {e}")

        # Let SQLAlchemy create any missing tables (media, etc.)
        db.create_all()
        print("[INIT_DB] Database schema ensured.")


init_db()


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------

def get_json():
    if not request.data:
        return {}
    try:
        return request.get_json(force=True, silent=True) or {}
    except Exception:
        return {}


def ensure_session_dir(session_id: str) -> Path:
    media_root = BASE_DIR / "media"
    media_root.mkdir(exist_ok=True)
    d = media_root / session_id
    d.mkdir(exist_ok=True)
    return d


# -------------------------------------------------------------------
# API routes
# -------------------------------------------------------------------

@app.get("/api/health")
def health():
    return jsonify({"status": "ok"})


@app.get("/api/sessions")
def list_sessions():
    """
    GET /api/sessions?limit=20

    Return recent sessions (sorted by created_at DESC).
    """
    try:
        limit = int(request.args.get("limit", "20"))
    except ValueError:
        limit = 20

    q = Session.query.order_by(Session.created_at.desc())
    sessions = q.limit(limit).all()

    return jsonify(
        {
            "status": "ok",
            "count": len(sessions),
            "sessions": [s.to_dict() for s in sessions],
        }
    )


@app.post("/api/sessions/<session_id>/upsert")
def upsert_session(session_id):
    """
    Create or update a session by its external session_id.

    Body shape we expect (from bulk uploader / local bot):

        {
          "meta": {
            "title": "...",
            "client_name": "...",
            "source": "bulk|local_bot|mobile",
            "status": "new|in_progress|done"
          },
          "payload": { ... anything ... }
        }

    For now, **we do NOT set user_id** at all. That keeps us free
    from the users FK until we build proper auth.
    """
    data = get_json()
    meta = data.get("meta") or {}
    payload = data.get("payload") or {}

    now = dt.datetime.utcnow()

    sess: Session | None = (
        Session.query.filter_by(session_id=session_id).one_or_none()
    )

    created = False
    if not sess:
        sess = Session(session_id=session_id)
        # Do NOT touch user_id here (leave NULL)
        created = True
        db.session.add(sess)

    # Update simple fields
    if "title" in meta:
        sess.title = meta.get("title") or None
    if "client_name" in meta:
        sess.client_name = meta.get("client_name") or None
    if "source" in meta:
        sess.source = meta.get("source") or None
    if "status" in meta:
        sess.status = meta.get("status") or None

    # Keep payload as flexible JSON
    # Merge existing payload with new one to avoid overwriting everything,
    # but you can change this later to hard replace.
    base_payload = sess.payload or {}
    base_payload.update(payload)
    sess.payload = base_payload

    # external_id placeholder (later can be client id, etc.)
    if sess.external_id is None:
        sess.external_id = None  # keep nullable

    sess.updated_at = now
    if created and not sess.created_at:
        sess.created_at = now

    db.session.commit()

    return jsonify(
        {
            "status": "ok",
            "created": created,
            "session": sess.to_dict(),
        }
    )


@app.post("/api/sessions/<session_id>/image")
def upload_image(session_id):
    """
    Upload an image for a session.

    form-data:
      - file: (image file)
    """
    if "file" not in request.files:
        return jsonify({"status": "error", "error": "missing file"}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"status": "error", "error": "empty filename"}), 400

    session_dir = ensure_session_dir(session_id)
    ext = Path(file.filename).suffix or ".jpg"
    fn = f"img_{uuid.uuid4().hex}{ext}"
    full_path = session_dir / fn
    file.save(full_path)

    media = Media(
        session_id=session_id,
        media_type="image",
        filename=str(full_path.relative_to(BASE_DIR)),
        mime_type=file.mimetype,
        size_bytes=full_path.stat().st_size,
    )
    db.session.add(media)
    db.session.commit()

    return jsonify({"status": "ok", "media": media.to_dict()})


@app.post("/api/sessions/<session_id>/audio")
def upload_audio(session_id):
    """
    Upload an audio file for a session.

    form-data:
      - file: (audio file)
    """
    if "file" not in request.files:
        return jsonify({"status": "error", "error": "missing file"}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"status": "error", "error": "empty filename"}), 400

    session_dir = ensure_session_dir(session_id)
    ext = Path(file.filename).suffix or ".m4a"
    fn = f"aud_{uuid.uuid4().hex}{ext}"
    full_path = session_dir / fn
    file.save(full_path)

    media = Media(
        session_id=session_id,
        media_type="audio",
        filename=str(full_path.relative_to(BASE_DIR)),
        mime_type=file.mimetype,
        size_bytes=full_path.stat().st_size,
    )
    db.session.add(media)
    db.session.commit()

    return jsonify({"status": "ok", "media": media.to_dict()})


@app.post("/api/sessions/<session_id>/analyze")
def analyze_session(session_id):
    """
    Placeholder AI pipeline.

    For now: just echo back a fake analysis summary.
    Later we'll plug in OpenAI + your estimator.
    """
    sess: Session | None = (
        Session.query.filter_by(session_id=session_id).one_or_none()
    )
    if not sess:
        return jsonify({"status": "error", "error": "session not found"}), 404

    # Count media
    media_items = Media.query.filter_by(session_id=session_id).all()
    num_images = sum(1 for m in media_items if m.media_type == "image")
    num_audio = sum(1 for m in media_items if m.media_type == "audio")

    analysis = {
        "summary": f"Session {session_id} has {num_images} photo(s) and {num_audio} audio file(s).",
        "recommendation": "Run full AI pipeline locally and sync structured quote.",
    }

    payload = sess.payload or {}
    payload["analysis"] = analysis
    sess.payload = payload
    sess.updated_at = dt.datetime.utcnow()
    db.session.commit()

    return jsonify({"status": "ok", "analysis": analysis, "session": sess.to_dict()})


# -------------------------------------------------------------------
# Default root
# -------------------------------------------------------------------

@app.get("/")
def root():
    return "JobFlow Cloud Mini API", 200


# -------------------------------------------------------------------
# Entry point (for local dev)
# -------------------------------------------------------------------

if __name__ == "__main__":
    # Local dev server
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
