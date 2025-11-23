import os
import uuid
from datetime import datetime

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from dotenv import load_dotenv
from sqlalchemy import text

# =========================
# Environment & App Setup
# =========================

load_dotenv()

app = Flask(__name__)
CORS(app)

database_url = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL")

if not database_url:
    database_url = "sqlite:///jobflow_local.db"

# Render sometimes gives postgres://; SQLAlchemy 2 expects postgresql://
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = database_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

APP_ROOT = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(APP_ROOT, "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

IS_POSTGRES = database_url.startswith("postgresql://")

# =========================
# Optional OpenAI client
# =========================

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

try:
    from openai import OpenAI
    openai_client = OpenAI()
except Exception:
    openai_client = None


# =========================
# Database Models
# =========================

class Session(db.Model):
    """
    Existing sessions table in Render, extended for JobFlow:

      id          INTEGER PRIMARY KEY      (existing)
      user_id     INTEGER NOT NULL         (legacy; we set to 0 for all new rows)
      session_id  VARCHAR(128) UNIQUE      (string external ID, e.g. 'jobflow_123')
      payload     JSON, nullable           (bag for AI / metadata)
      created_at  TIMESTAMP
      updated_at  TIMESTAMP
    """
    __tablename__ = "sessions"

    id = db.Column(db.Integer, primary_key=True)

    # Legacy column – keep it, always default to 0 so NOT NULL is satisfied
    user_id = db.Column(db.Integer, nullable=False, default=0)

    # External/friendly ID used by API paths
    session_id = db.Column(db.String(128), unique=True, index=True, nullable=True)

    # Everything else we care about goes in here
    payload = db.Column(db.JSON, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )


class Media(db.Model):
    """
    Media attached to a session (images, audio, etc.).
    """
    __tablename__ = "media"

    id = db.Column(db.Integer, primary_key=True)

    # FK to numeric Session.id
    session_id = db.Column(
        db.Integer,
        db.ForeignKey("sessions.id"),
        index=True,
        nullable=False,
    )

    media_type = db.Column(db.String(32), nullable=False)  # "image" or "audio"
    filename = db.Column(db.String(512), nullable=False)
    filepath = db.Column(db.String(1024), nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class Analysis(db.Model):
    """
    Stored AI analysis for a session.
    """
    __tablename__ = "analysis"

    id = db.Column(db.Integer, primary_key=True)

    # FK to numeric Session.id
    session_id = db.Column(
        db.Integer,
        db.ForeignKey("sessions.id"),
        index=True,
        nullable=False,
    )

    summary = db.Column(db.Text, nullable=True)
    raw = db.Column(db.JSON, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


# =========================
# Schema Init + Lightweight Migration
# =========================

def init_db() -> None:
    """
    Ensure tables exist and align base schema when running on Postgres.

    We do NOT try to rewrite existing columns (like user_id); we just
    add new ones if missing and relax constraints that break inserts.
    """
    with app.app_context():
        try:
            db.create_all()
            print("[INIT_DB] Database schema ensured.", flush=True)

            if IS_POSTGRES:
                stmts = [
                    # External ID column for sessions
                    "ALTER TABLE sessions "
                    "ADD COLUMN IF NOT EXISTS session_id VARCHAR(128)",

                    # JSON payload column
                    "ALTER TABLE sessions "
                    "ADD COLUMN IF NOT EXISTS payload JSON",

                    # Index for fast lookup by session_id
                    "CREATE INDEX IF NOT EXISTS ix_sessions_session_id "
                    "ON sessions (session_id)",

                    # Legacy column on old schema; drop NOT NULL so we can ignore it
                    "ALTER TABLE sessions "
                    "ALTER COLUMN external_id DROP NOT NULL",
                ]
                for sql in stmts:
                    try:
                        db.session.execute(text(sql))
                        db.session.commit()
                        print(f"[INIT_DB] Ran migration: {sql}", flush=True)
                    except Exception as e:
                        db.session.rollback()
                        print(f"[INIT_DB] Migration error for '{sql}': {e}", flush=True)

        except Exception as e:
            print(f"[INIT_DB] Error creating tables: {e}", flush=True)


init_db()


# =========================
# Helper Functions
# =========================

def _meta_from_payload(payload: dict | None) -> dict:
    if not payload or not isinstance(payload, dict):
        return {}
    meta = payload.get("meta")
    return meta if isinstance(meta, dict) else {}


def session_to_dict(session: Session) -> dict:
    payload = session.payload or {}
    meta = _meta_from_payload(payload)

    return {
        "id": session.id,  # numeric internal id
        "session_id": session.session_id,  # external string id
        "title": meta.get("title"),
        "client_name": meta.get("client_name"),
        "source": meta.get("source"),
        "status": meta.get("status"),
        "payload": payload,
        "created_at": session.created_at.isoformat() if session.created_at else None,
        "updated_at": session.updated_at.isoformat() if session.updated_at else None,
    }


def media_to_dict(media: Media) -> dict:
    return {
        "id": media.id,
        "session_row_id": media.session_id,
        "media_type": media.media_type,
        "filename": media.filename,
        "filepath": media.filepath,
        "created_at": media.created_at.isoformat() if media.created_at else None,
    }


def analysis_to_dict(analysis: Analysis) -> dict:
    return {
        "id": analysis.id,
        "session_row_id": analysis.session_id,
        "summary": analysis.summary,
        "raw": analysis.raw or {},
        "created_at": analysis.created_at.isoformat() if analysis.created_at else None,
    }


def ensure_session(sid: str) -> Session:
    """
    Get or create a Session row using the string session_id.

    - Looks up by session_id
    - If missing, creates a new row with user_id=0 and empty payload
    """
    session = Session.query.filter_by(session_id=sid).first()
    if session:
        # Safety: old rows might have NULL user_id; normalize to 0
        if session.user_id is None:
            session.user_id = 0
            db.session.commit()
        return session

    session = Session(session_id=sid, payload=None, user_id=0)
    db.session.add(session)
    db.session.commit()
    return session


def save_uploaded_file(file_storage, prefix: str) -> tuple[str, str]:
    ext = os.path.splitext(file_storage.filename)[1]
    unique_name = f"{prefix}_{uuid.uuid4().hex}{ext}"
    filepath = os.path.join(UPLOAD_FOLDER, unique_name)
    file_storage.save(filepath)
    return unique_name, filepath


def run_placeholder_ai(prompt: str) -> str:
    """
    Very simple AI call – just to prove /analyze works.
    Replace this later with the full JobFlow analysis pipeline.
    """
    if openai_client is None:
        return prompt
    try:
        resp = openai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": "You summarize contractor job sessions in 2–3 sentences.",
                },
                {"role": "user", "content": prompt},
            ],
            max_tokens=120,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        print(f"[ANALYZE] OpenAI error: {e}", flush=True)
        return prompt


def _merge_meta_into_payload(payload: dict | None, data: dict) -> dict:
    """
    Merge top-level fields + payload into a single JSON bag.
    """
    payload = payload or {}
    if not isinstance(payload, dict):
        payload = {}

    meta = _meta_from_payload(payload)

    for key in ["title", "client_name", "source", "status"]:
        if key in data and data[key] is not None:
            meta[key] = data[key]

    if meta:
        payload["meta"] = meta

    body_payload = data.get("payload")
    if isinstance(body_payload, dict):
        payload.update(body_payload)

    return payload


# =========================
# Routes
# =========================

@app.route("/")
def index():
    return jsonify({"status": "ok", "message": "JobFlow Cloud Mini root"}), 200


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


# ---- 1. GET /api/sessions ----
@app.route("/api/sessions", methods=["GET"])
def list_sessions():
    try:
        limit = int(request.args.get("limit", "50"))
    except ValueError:
        limit = 50

    q = Session.query.order_by(Session.created_at.desc())
    if limit > 0:
        q = q.limit(limit)

    sessions = q.all()
    return jsonify({
        "status": "ok",
        "count": len(sessions),
        "sessions": [session_to_dict(s) for s in sessions],
    }), 200


# ---- 2. POST/PUT /api/sessions/<sid>/upsert ----
@app.route("/api/sessions/<string:sid>/upsert", methods=["POST", "PUT"])
def upsert_session(sid):
    """
    Upsert by external session_id (string).

    Body JSON (all optional):

    {
      "title": "...",
      "client_name": "...",
      "source": "local_bot | bulk_uploader | mobile_app",
      "status": "new | analyzed | ...",
      "payload": {...}
    }
    """
    data = request.get_json(silent=True) or {}

    session = Session.query.filter_by(session_id=sid).first()
    created = False

    if not session:
        session = Session(session_id=sid, user_id=0)
        created = True
    elif session.user_id is None:
        session.user_id = 0

    session.payload = _merge_meta_into_payload(session.payload, data)

    if created:
        db.session.add(session)

    db.session.commit()

    return jsonify({
        "status": "ok",
        "created": created,
        "session": session_to_dict(session),
    }), 200


# Alias: POST /api/sessions/<sid> (for simpler clients)
@app.route("/api/sessions/<string:sid>", methods=["POST"])
def upsert_session_alias(sid):
    return upsert_session(sid)


# ---- 3. POST /api/sessions/<sid>/image ----
@app.route("/api/sessions/<string:sid>/image", methods=["POST"])
def upload_image(sid):
    session = ensure_session(sid)

    if "file" not in request.files and "image" not in request.files:
        return jsonify({"status": "error", "message": "No image file uploaded"}), 400

    file_storage = request.files.get("file") or request.files.get("image")

    filename, filepath = save_uploaded_file(file_storage, prefix=f"{sid}_img")

    media = Media(
        session_id=session.id,
        media_type="image",
        filename=filename,
        filepath=filepath,
    )
    db.session.add(media)
    db.session.commit()

    return jsonify({
        "status": "ok",
        "session_id": sid,
        "media": media_to_dict(media),
    }), 200


# ---- 4. POST /api/sessions/<sid>/audio ----
@app.route("/api/sessions/<string:sid>/audio", methods=["POST"])
def upload_audio(sid):
    session = ensure_session(sid)

    if "file" not in request.files and "audio" not in request.files:
        return jsonify({"status": "error", "message": "No audio file uploaded"}), 400

    file_storage = request.files.get("file") or request.files.get("audio")

    filename, filepath = save_uploaded_file(file_storage, prefix=f"{sid}_aud")

    media = Media(
        session_id=session.id,
        media_type="audio",
        filename=filename,
        filepath=filepath,
    )
    db.session.add(media)
    db.session.commit()

    return jsonify({
        "status": "ok",
        "session_id": sid,
        "media": media_to_dict(media),
    }), 200


# ---- Serve uploads (debug only) ----
@app.route("/uploads/<path:filename>", methods=["GET"])
def serve_upload(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)


# ---- 5. POST /api/sessions/<sid>/analyze ----
@app.route("/api/sessions/<string:sid>/analyze", methods=["POST"])
def analyze_session(sid):
    """
    Placeholder AI pipeline:
      - Count media for this session
      - Generate a short summary
      - Store Analysis row + write into session.payload
    """
    session = ensure_session(sid)
    media_items = Media.query.filter_by(session_id=session.id).all()

    image_count = sum(1 for m in media_items if m.media_type == "image")
    audio_count = sum(1 for m in media_items if m.media_type == "audio")

    prompt = (
        f"Job session id: {sid}. There are {image_count} photo(s) and "
        f"{audio_count} audio note(s) attached. "
        "Generate a short 2–3 sentence summary of what this job session might represent "
        "for a home-services contractor (estimate, inspection, etc.)."
    )

    summary_text = run_placeholder_ai(prompt)

    raw_payload = {
        "image_count": image_count,
        "audio_count": audio_count,
        "media_row_ids": [m.id for m in media_items],
        "note": "placeholder analysis – replace with real pipeline later",
    }

    analysis = Analysis(
        session_id=session.id,
        summary=summary_text,
        raw=raw_payload,
    )
    db.session.add(analysis)

    session.payload = session.payload or {}
    if not isinstance(session.payload, dict):
        session.payload = {}

    session.payload["analysis_summary"] = summary_text
    session.payload["analysis_meta"] = raw_payload

    db.session.commit()

    return jsonify({
        "status": "ok",
        "session": session_to_dict(session),
        "analysis": analysis_to_dict(analysis),
    }), 200


# =========================
# Local Entrypoint
# =========================

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
