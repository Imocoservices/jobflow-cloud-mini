import os
import uuid
from datetime import datetime

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from dotenv import load_dotenv

# =========================
# Environment & App Setup
# =========================

load_dotenv()

app = Flask(__name__)
CORS(app)

# ---- Database URL handling (Render + local) ----
database_url = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL")

if not database_url:
    # safe local fallback
    database_url = "sqlite:///jobflow_local.db"

# SQLAlchemy 2 expects postgresql:// not postgres://
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = database_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

APP_ROOT = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(APP_ROOT, "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# =========================
# Optional OpenAI client (for placeholder AI)
# =========================

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

try:
    from openai import OpenAI  # modern OpenAI SDK (>=1.x)
    openai_client = OpenAI()
except Exception:
    openai_client = None


# =========================
# Database Models
# =========================

class Session(db.Model):
    """
    Cloud session representing a single job / estimate.

    IMPORTANT: this model matches the existing Postgres schema:

      id          (string PK)
      payload     (JSON, nullable)
      created_at  (datetime)
      updated_at  (datetime)

    Any metadata like title / client_name / source / status
    is stored inside payload["meta"] instead of separate columns.
    """
    __tablename__ = "sessions"

    id = db.Column(db.String(128), primary_key=True)
    payload = db.Column(db.JSON, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )


class Media(db.Model):
    __tablename__ = "media"

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(
        db.String(128),
        db.ForeignKey("sessions.id"),
        index=True,
        nullable=False,
    )

    media_type = db.Column(db.String(32), nullable=False)  # "image" or "audio"
    filename = db.Column(db.String(512), nullable=False)
    filepath = db.Column(db.String(1024), nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class Analysis(db.Model):
    __tablename__ = "analysis"

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(
        db.String(128),
        db.ForeignKey("sessions.id"),
        index=True,
        nullable=False,
    )

    summary = db.Column(db.Text, nullable=True)
    raw = db.Column(db.JSON, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


# =========================
# Schema Init (SQLAlchemy 2 safe)
# =========================

def init_db() -> None:
    """
    Initialize all tables inside a real Flask app context.
    This avoids 'Working outside of application context' errors.
    """
    with app.app_context():
        try:
            db.create_all()
            print("[INIT_DB] Database schema ensured.", flush=True)
        except Exception as e:
            # Log but don't crash the app
            print(f"[INIT_DB] Error creating tables: {e}", flush=True)


init_db()


# =========================
# Helper Functions
# =========================

def _meta_from_payload(payload: dict | None) -> dict:
    """Extract meta dict from payload JSON (if present)."""
    if not payload or not isinstance(payload, dict):
        return {}
    meta = payload.get("meta")
    if isinstance(meta, dict):
        return meta
    return {}


def session_to_dict(session: Session) -> dict:
    payload = session.payload or {}
    meta = _meta_from_payload(payload)

    return {
        "id": session.id,
        # flattened meta for dashboard convenience
        "title": meta.get("title"),
        "client_name": meta.get("client_name"),
        "source": meta.get("source"),
        "status": meta.get("status"),
        # full payload
        "payload": payload,
        "created_at": session.created_at.isoformat() if session.created_at else None,
        "updated_at": session.updated_at.isoformat() if session.updated_at else None,
    }


def media_to_dict(media: Media) -> dict:
    return {
        "id": media.id,
        "session_id": media.session_id,
        "media_type": media.media_type,
        "filename": media.filename,
        "filepath": media.filepath,
        "created_at": media.created_at.isoformat() if media.created_at else None,
    }


def analysis_to_dict(analysis: Analysis) -> dict:
    return {
        "id": analysis.id,
        "session_id": analysis.session_id,
        "summary": analysis.summary,
        "raw": analysis.raw or {},
        "created_at": analysis.created_at.isoformat() if analysis.created_at else None,
    }


def ensure_session(sid: str) -> Session:
    """
    Get or create a Session row using the string id as primary key.
    """
    session = Session.query.get(sid)
    if session:
        return session

    session = Session(id=sid, payload=None)
    db.session.add(session)
    db.session.commit()
    return session


def save_uploaded_file(file_storage, prefix: str) -> tuple[str, str]:
    """
    Save uploaded file to UPLOAD_FOLDER and return (filename, filepath).
    """
    ext = os.path.splitext(file_storage.filename)[1]
    unique_name = f"{prefix}_{uuid.uuid4().hex}{ext}"
    filepath = os.path.join(UPLOAD_FOLDER, unique_name)
    file_storage.save(filepath)
    return unique_name, filepath


def run_placeholder_ai(summary_prompt: str) -> str:
    """
    Very small OpenAI call used by /analyze.
    If OpenAI is not configured or fails, falls back to the prompt itself.
    """
    if openai_client is None:
        return summary_prompt

    try:
        resp = openai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": "You summarize contractor job sessions in 2–3 sentences.",
                },
                {"role": "user", "content": summary_prompt},
            ],
            max_tokens=120,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        print(f"[ANALYZE] OpenAI error: {e}", flush=True)
        return summary_prompt


def _merge_meta_into_payload(payload: dict | None, data: dict) -> dict:
    """
    Take an existing payload JSON and merge title/client_name/source/status
    from the incoming data into payload["meta"].
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

    # If caller also passed their own payload, merge it shallowly
    body_payload = data.get("payload")
    if isinstance(body_payload, dict):
        # This is a shallow update: body_payload wins when overlapping
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
    """
    Return list of sessions from DB.
    Optional query param: ?limit=20
    """
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
    Create or update a session.

    Body JSON (all optional):
    {
      "title": "...",
      "client_name": "...",
      "source": "local_bot | bulk_uploader | mobile_app",
      "status": "new | analyzed | ...",
      "payload": {...}      # arbitrary JSON, merged into session.payload
    }
    """
    data = request.get_json(silent=True) or {}

    session = Session.query.get(sid)
    created = False

    if not session:
        session = Session(id=sid)
        created = True

    # Merge meta + payload into JSON payload field
    session.payload = _merge_meta_into_payload(session.payload, data)

    if created:
        db.session.add(session)

    db.session.commit()

    return jsonify({
        "status": "ok",
        "created": created,
        "session": session_to_dict(session),
    }), 200


# Backwards-compat alias: POST /api/sessions/<sid>
@app.route("/api/sessions/<string:sid>", methods=["POST"])
def upsert_session_alias(sid):
    return upsert_session(sid)


# ---- 3. POST /api/sessions/<sid>/image ----
@app.route("/api/sessions/<string:sid>/image", methods=["POST"])
def upload_image(sid):
    """
    Accept image upload and attach to session.
    Form field name: file  (or image)
    """
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
        "session_id": session.id,
        "media": media_to_dict(media),
    }), 200


# ---- 4. POST /api/sessions/<sid>/audio ----
@app.route("/api/sessions/<string:sid>/audio", methods=["POST"])
def upload_audio(sid):
    """
    Accept audio upload and attach to session.
    Form field name: file  (or audio)
    """
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
        "session_id": session.id,
        "media": media_to_dict(media),
    }), 200


# ---- Serve uploaded files (debug only) ----
@app.route("/uploads/<path:filename>", methods=["GET"])
def serve_upload(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)


# ---- 5. POST /api/sessions/<sid>/analyze ----
@app.route("/api/sessions/<string:sid>/analyze", methods=["POST"])
def analyze_session(sid):
    """
    Placeholder AI analysis endpoint.

    For now:
      - Ensures session exists
      - Counts media items
      - Calls OpenAI for a tiny summary (if configured)
      - Stores Analysis row and updates session.payload["analysis_*"]
    """
    session = ensure_session(sid)
    media_items = Media.query.filter_by(session_id=session.id).all()

    image_count = sum(1 for m in media_items if m.media_type == "image")
    audio_count = sum(1 for m in media_items if m.media_type == "audio")

    base_prompt = (
        f"Job session id: {sid}. There are {image_count} photo(s) and "
        f"{audio_count} audio note(s) attached. "
        "Generate a short 2–3 sentence summary of what this job session might represent "
        "for a home-services contractor (estimate, inspection, etc.)."
    )

    summary_text = run_placeholder_ai(base_prompt)

    raw_payload = {
        "image_count": image_count,
        "audio_count": audio_count,
        "media_ids": [m.id for m in media_items],
        "note": "placeholder analysis – replace with real pipeline later",
    }

    analysis = Analysis(
        session_id=session.id,
        summary=summary_text,
        raw=raw_payload,
    )
    db.session.add(analysis)

    # Store analysis inside JSON payload (no dedicated columns)
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
    port = int(os.getenv("PORT", "10000"))  # Render uses 10000 internally
    app.run(host="0.0.0.0", port=port)
