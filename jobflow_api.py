import os
import uuid
import datetime as dt
from typing import Optional, Dict, Any, List

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy import func

from openai import OpenAI

# -------------------------------------------------------------------
# App & config
# -------------------------------------------------------------------

app = Flask(__name__)
CORS(app)

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")

app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

# -------------------------------------------------------------------
# Models
# -------------------------------------------------------------------

class Session(db.Model):
    __tablename__ = "sessions"

    id = db.Column(db.Integer, primary_key=True)
    # public session id, what you use from PowerShell / local bot
    sid = db.Column(db.String(128), unique=True, nullable=False, index=True)

    client_name = db.Column(db.String(255), nullable=True)
    job_type = db.Column(db.String(255), nullable=True)
    status = db.Column(db.String(50), nullable=True, default="new")

    summary = db.Column(db.Text, nullable=True)
    predicted_price = db.Column(db.Float, nullable=True)

    payload = db.Column(JSONB, nullable=True)

    created_at = db.Column(db.DateTime, nullable=False, server_default=func.now())
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    media = db.relationship("Media", backref="session", lazy=True)

    def to_dict(self, include_payload: bool = True) -> Dict[str, Any]:
        base = {
            "id": self.id,
            "sid": self.sid,
            "client_name": self.client_name,
            "job_type": self.job_type,
            "status": self.status,
            "summary": self.summary,
            "predicted_price": self.predicted_price,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_payload:
            base["payload"] = self.payload or {}
        return base


class Media(db.Model):
    __tablename__ = "media"

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey("sessions.id"), nullable=False)
    sid = db.Column(db.String(128), nullable=False, index=True)

    kind = db.Column(db.String(32), nullable=False)  # "image" or "audio"
    filename = db.Column(db.String(512), nullable=False)
    mime_type = db.Column(db.String(255), nullable=True)

    created_at = db.Column(db.DateTime, nullable=False, server_default=func.now())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "sid": self.sid,
            "kind": self.kind,
            "filename": self.filename,
            "mime_type": self.mime_type,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


with app.app_context():
    db.create_all()

# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------

def get_openai_client() -> Optional[OpenAI]:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None
    return OpenAI(api_key=api_key)


def get_or_create_session_by_sid(sid: str, payload: Optional[Dict[str, Any]] = None) -> Session:
    """
    Upsert helper – SAFE with whatever JSON you send from PowerShell.
    """
    if not sid:
        raise ValueError("sid is required")

    payload = payload or {}

    sess: Optional[Session] = Session.query.filter_by(sid=sid).first()
    if not sess:
        sess = Session(sid=sid)
        db.session.add(sess)

    # All fields are optional – we only overwrite if present in payload
    client_name = payload.get("client_name")
    job_type = payload.get("job_type")
    status = payload.get("status")
    summary = payload.get("summary")
    predicted_price = payload.get("predicted_price")

    if client_name is not None:
        sess.client_name = client_name
    if job_type is not None:
        sess.job_type = job_type
    if status is not None:
        sess.status = status
    if summary is not None:
        sess.summary = summary
    if predicted_price is not None:
        try:
            sess.predicted_price = float(predicted_price)
        except (TypeError, ValueError):
            pass

    # Always keep raw payload (string keys only)
    if payload:
        existing = sess.payload or {}
        existing.update(payload)
        sess.payload = existing

    db.session.commit()
    return sess


def save_uploaded_file(file_storage, subdir: str) -> str:
    """
    Save an uploaded file under /tmp/storage/<subdir>/unique-name.ext
    Returns the stored filename.
    """
    base_dir = "/tmp/storage"
    os.makedirs(base_dir, exist_ok=True)
    target_dir = os.path.join(base_dir, subdir)
    os.makedirs(target_dir, exist_ok=True)

    ext = ""
    if "." in file_storage.filename:
        ext = "." + file_storage.filename.rsplit(".", 1)[-1]

    unique_name = f"{dt.datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex}{ext}"
    path = os.path.join(target_dir, unique_name)
    file_storage.save(path)
    return unique_name


# -------------------------------------------------------------------
# Routes
# -------------------------------------------------------------------

@app.route("/api/health", methods=["GET"])
def health():
    return jsonify(
        {
            "ok": True,
            "service": "jobflow-cloud-mini",
            "time": dt.datetime.utcnow().isoformat() + "Z",
        }
    )


# --- Sessions list / detail ---------------------------------------------------

@app.route("/api/sessions", methods=["GET"])
def list_sessions():
    try:
        limit = int(request.args.get("limit", 20))
        offset = int(request.args.get("offset", 0))
    except ValueError:
        limit = 20
        offset = 0

    q = (
        Session.query.order_by(Session.created_at.desc())
        .offset(offset)
        .limit(limit)
    )

    items = [s.to_dict(include_payload=False) for s in q]
    total = Session.query.count()
    return jsonify({"ok": True, "sessions": items, "total": total, "limit": limit, "offset": offset})


@app.route("/api/sessions/<sid>", methods=["GET"])
def get_session(sid: str):
    sess = Session.query.filter_by(sid=sid).first()
    if not sess:
        return jsonify({"ok": False, "error": "not_found"}), 404
    return jsonify({"ok": True, "session": sess.to_dict(include_payload=True)})


@app.route("/api/sessions/<sid>/media", methods=["GET"])
def get_session_media(sid: str):
    sess = Session.query.filter_by(sid=sid).first()
    if not sess:
        return jsonify({"ok": False, "error": "not_found"}), 404
    media = Media.query.filter_by(session_id=sess.id).order_by(Media.created_at.asc()).all()
    return jsonify({"ok": True, "media": [m.to_dict() for m in media]})


# --- Session upsert (used by PowerShell & local bot) --------------------------

@app.route("/api/sessions/<sid>/upsert", methods=["POST"])
def upsert_session(sid: str):
    try:
        payload = request.get_json(silent=True) or {}
        sess = get_or_create_session_by_sid(sid, payload)
        return jsonify({"ok": True, "session": sess.to_dict(include_payload=True)})
    except Exception as e:
        app.logger.exception("Error in upsert_session")
        return jsonify({"ok": False, "error": "internal_error", "detail": str(e)}), 500


# --- Media upload (image / audio) --------------------------------------------

@app.route("/api/sessions/<sid>/image", methods=["POST"])
def upload_image(sid: str):
    sess = Session.query.filter_by(sid=sid).first()
    if not sess:
        # auto-create empty session if it somehow doesn't exist yet
        sess = get_or_create_session_by_sid(sid, {"status": "new"})

    if "file" not in request.files:
        return jsonify({"ok": False, "error": "no_file"}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"ok": False, "error": "empty_filename"}), 400

    stored_name = save_uploaded_file(file, sid)

    media = Media(
        session_id=sess.id,
        sid=sid,
        kind="image",
        filename=stored_name,
        mime_type=file.mimetype,
    )
    db.session.add(media)
    db.session.commit()

    return jsonify({"ok": True, "media": media.to_dict()})


@app.route("/api/sessions/<sid>/audio", methods=["POST"])
def upload_audio(sid: str):
    sess = Session.query.filter_by(sid=sid).first()
    if not sess:
        sess = get_or_create_session_by_sid(sid, {"status": "new"})

    if "file" not in request.files:
        return jsonify({"ok": False, "error": "no_file"}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"ok": False, "error": "empty_filename"}), 400

    stored_name = save_uploaded_file(file, sid)

    media = Media(
        session_id=sess.id,
        sid=sid,
        kind="audio",
        filename=stored_name,
        mime_type=file.mimetype,
    )
    db.session.add(media)
    db.session.commit()

    return jsonify({"ok": True, "media": media.to_dict()})


# --- AI analyze endpoint ------------------------------------------------------

@app.route("/api/sessions/<sid>/analyze", methods=["POST"])
def analyze_session(sid: str):
    sess = Session.query.filter_by(sid=sid).first()
    if not sess:
        return jsonify({"ok": False, "error": "not_found"}), 404

    client = get_openai_client()
    if client is None:
        # Don't crash – tell caller AI is unavailable
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "ai_unavailable",
                    "detail": "OPENAI_API_KEY is not set on the server",
                }
            ),
            503,
        )

    media_items: List[Media] = Media.query.filter_by(session_id=sess.id).order_by(Media.created_at.asc()).all()

    # Simple text summary based on filenames + payload for now.
    # You can upgrade this later to real Whisper + image analysis.
    image_files = [m.filename for m in media_items if m.kind == "image"]
    audio_files = [m.filename for m in media_items if m.kind == "audio"]

    prompt_payload = {
        "client_name": sess.client_name,
        "job_type": sess.job_type,
        "images": image_files,
        "audio_files": audio_files,
        "raw_payload": sess.payload or {},
    }

    messages = [
        {
            "role": "system",
            "content": "You are an estimator assistant for a handyman / contractor. "
                       "You will see meta-data about job media and must produce a clean summary "
                       "and a rough price guess in US dollars.",
        },
        {
            "role": "user",
            "content": f"Here is the session info:\n{prompt_payload}\n\n"
                       f"1) Give a short bullet summary of the work.\n"
                       f"2) Suggest a single rough price number.\n"
                       f"3) List any key materials or steps.",
        },
    ]

    try:
        completion = client.chat.completions.create(
            model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
            messages=messages,
        )
        text = completion.choices[0].message.content or ""

        # Crude extraction of price – look for first $number
        price = None
        import re

        m = re.search(r"\$?\s*([\d,]+(\.\d{1,2})?)", text)
        if m:
            try:
                price_str = m.group(1).replace(",", "")
                price = float(price_str)
            except ValueError:
                price = None

        sess.summary = text
        if price is not None:
            sess.predicted_price = price
        sess.status = "analyzed"

        # Also store AI response in payload["ai"]
        payload = sess.payload or {}
        payload.setdefault("ai", {})
        payload["ai"]["summary"] = text
        if price is not None:
            payload["ai"]["predicted_price"] = price
        sess.payload = payload

        db.session.commit()

        return jsonify(
            {
                "ok": True,
                "session": sess.to_dict(include_payload=True),
            }
        )
    except Exception as e:
        app.logger.exception("Error in analyze_session")
        return jsonify({"ok": False, "error": "internal_error", "detail": str(e)}), 500


# -------------------------------------------------------------------
# Root & static helpers (optional)
# -------------------------------------------------------------------

@app.route("/", methods=["GET"])
def root():
    return jsonify(
        {
            "ok": True,
            "service": "jobflow-cloud-mini",
            "message": "JobFlow Cloud Mini API",
        }
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
