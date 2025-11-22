import os
import datetime as dt

from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy import func

# --- App & config ---

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

db_url = os.getenv("DATABASE_URL")
if db_url and db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

app.config.update(
    SQLALCHEMY_DATABASE_URI=db_url,
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    JSON_SORT_KEYS=False,
    SECRET_KEY=os.getenv("SECRET_KEY", "jobflow-dev-secret"),
)

db = SQLAlchemy(app)


# --- Models ---

class Session(db.Model):
    __tablename__ = "sessions"
    id = db.Column(db.Integer, primary_key=True)
    sid = db.Column(db.String(64), unique=True, nullable=False, index=True)

    external_id = db.Column(db.String(128), nullable=True)
    client_name = db.Column(db.String(255), nullable=True)
    job_type = db.Column(db.String(255), nullable=True)
    summary = db.Column(db.Text, nullable=True)

    source = db.Column(db.String(64), nullable=True, default="bulk_folder")
    payload = db.Column(JSONB, nullable=True)

    created_at = db.Column(db.DateTime, default=func.now())
    updated_at = db.Column(db.DateTime, default=func.now(), onupdate=func.now())

    media = db.relationship(
        "Media", backref="session", lazy=True, cascade="all, delete-orphan"
    )
    analyses = db.relationship(
        "Analysis", backref="session", lazy=True, cascade="all, delete-orphan"
    )
    tasks = db.relationship(
        "AnalyzeTask", backref="session", lazy=True, cascade="all, delete-orphan"
    )

    def to_dict(self, include_payload: bool = True):
        data = {
            "id": self.id,
            "sid": self.sid,
            "external_id": self.external_id,
            "client_name": self.client_name,
            "job_type": self.job_type,
            "summary": self.summary,
            "source": self.source,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_payload:
            data["payload"] = self.payload or {}
        return data


class Media(db.Model):
    __tablename__ = "media"
    id = db.Column(db.Integer, primary_key=True)

    session_id = db.Column(
        db.Integer, db.ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    sid = db.Column(db.String(64), nullable=False, index=True)

    kind = db.Column(db.String(32), nullable=False)  # "image" or "audio"
    filename = db.Column(db.String(255), nullable=False)
    mime_type = db.Column(db.String(128), nullable=True)
    url = db.Column(db.String(1024), nullable=True)

    created_at = db.Column(db.DateTime, default=func.now())

    def to_dict(self):
        return {
            "id": self.id,
            "session_id": self.session_id,
            "sid": self.sid,
            "kind": self.kind,
            "filename": self.filename,
            "mime_type": self.mime_type,
            "url": self.url,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class Analysis(db.Model):
    __tablename__ = "analysis"
    id = db.Column(db.Integer, primary_key=True)

    session_id = db.Column(
        db.Integer, db.ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    sid = db.Column(db.String(64), nullable=False, index=True)

    result = db.Column(JSONB, nullable=True)
    created_at = db.Column(db.DateTime, default=func.now())

    def to_dict(self):
        return {
            "id": self.id,
            "session_id": self.session_id,
            "sid": self.sid,
            "result": self.result or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class AnalyzeTask(db.Model):
    __tablename__ = "analyze_tasks"
    id = db.Column(db.Integer, primary_key=True)

    session_id = db.Column(
        db.Integer, db.ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    sid = db.Column(db.String(64), nullable=False, index=True)

    status = db.Column(
        db.String(32), nullable=False, default="queued"
    )  # queued, running, done, error
    error_message = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, default=func.now())
    updated_at = db.Column(db.DateTime, default=func.now(), onupdate=func.now())

    def to_dict(self):
        return {
            "id": self.id,
            "session_id": self.session_id,
            "sid": self.sid,
            "status": self.status,
            "error_message": self.error_message,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


# --- DEV SCHEMA RESET (avoids migration pain right now) ---

with app.app_context():
    # For now, always reset our four tables so schema matches this file.
    # We're not storing production data yet, so this is safe.
    db.drop_all()
    db.create_all()


# --- Helpers ---

def get_or_create_session_by_sid(sid: str, payload: dict | None = None) -> Session:
    sess = Session.query.filter_by(sid=sid).first()
    if sess:
        if payload:
            merged = dict(sess.payload or {})
            merged.update(payload)
            sess.payload = merged
        return sess

    payload = payload or {}
    sess = Session(
        sid=sid,
        external_id=payload.get("external_id"),
        client_name=payload.get("client_name"),
        job_type=payload.get("job_type"),
        source=payload.get("source") or "bulk_folder",
        payload=payload,
    )
    db.session.add(sess)
    return sess


def _ok(**extra):
    return jsonify({"ok": True, **extra})


def _error(message: str, status: int = 400, **extra):
    body = {"ok": False, "error": message}
    body.update(extra)
    return jsonify(body), status


# --- Routes ---

@app.route("/api/health", methods=["GET"])
def health():
    return _ok(
        service="jobflow-cloud-mini",
        time=dt.datetime.utcnow().isoformat() + "Z",
    )


@app.route("/api/sessions", methods=["GET"])
def list_sessions():
    try:
        limit = int(request.args.get("limit", 20))
        offset = int(request.args.get("offset", 0))
    except ValueError:
        return _error("invalid limit/offset", 400)

    q = Session.query.order_by(Session.created_at.desc())
    total = q.count()
    items = [s.to_dict(include_payload=False) for s in q.limit(limit).offset(offset)]

    return _ok(sessions=items, total=total, limit=limit, offset=offset)


@app.route("/api/sessions/<sid>", methods=["GET"])
def get_session(sid):
    sess = Session.query.filter_by(sid=sid).first()
    if not sess:
        return _error("session not found", 404)

    latest_analysis = (
        Analysis.query.filter_by(session_id=sess.id)
        .order_by(Analysis.created_at.desc())
        .first()
    )

    data = sess.to_dict(include_payload=True)
    if latest_analysis:
        data["analysis"] = latest_analysis.to_dict()

    return _ok(session=data)


@app.route("/api/sessions/<sid>/media", methods=["GET"])
def list_media(sid):
    sess = Session.query.filter_by(sid=sid).first()
    if not sess:
        return _error("session not found", 404)

    media = (
        Media.query.filter_by(session_id=sess.id)
        .order_by(Media.created_at.asc())
        .all()
    )
    return _ok(media=[m.to_dict() for m in media])


@app.route("/api/sessions/<sid>/upsert", methods=["POST"])
def upsert_session(sid):
    """
    Called from bulk_upload scripts before media upload.

    Accepts JSON payload with optional fields:
      - client_name
      - job_type
      - external_id
      - source
      - any other metadata (stored in payload JSON)
    """
    payload = request.get_json(silent=True) or {}

    try:
        sess = get_or_create_session_by_sid(sid, payload=payload)
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        return _error("failed to upsert session", 500, detail=str(exc))

    return _ok(session=sess.to_dict())


def _save_uploaded_file(file_storage, dest_folder: str, sid: str) -> str:
    os.makedirs(dest_folder, exist_ok=True)
    ts = dt.datetime.utcnow().strftime("%Y%m%d%H%M%S")
    safe_name = file_storage.filename.replace(" ", "_")
    fname = f"{ts}_{sid}_{safe_name}"
    path = os.path.join(dest_folder, fname)
    file_storage.save(path)
    return path


@app.route("/api/sessions/<sid>/image", methods=["POST"])
def upload_image(sid):
    sess = Session.query.filter_by(sid=sid).first()
    if not sess:
        return _error("session not found", 404)

    if "file" not in request.files:
        return _error("missing file", 400)

    f = request.files["file"]
    save_path = _save_uploaded_file(
        f, os.getenv("UPLOAD_ROOT", "uploads/images"), sid
    )

    media = Media(
        session_id=sess.id,
        sid=sid,
        kind="image",
        filename=os.path.basename(save_path),
        mime_type=f.mimetype,
        url=None,
    )
    db.session.add(media)
    db.session.commit()

    return _ok(media=media.to_dict())


@app.route("/api/sessions/<sid>/audio", methods=["POST"])
def upload_audio(sid):
    sess = Session.query.filter_by(sid=sid).first()
    if not sess:
        return _error("session not found", 404)

    if "file" not in request.files:
        return _error("missing file", 400)

    f = request.files["file"]
    save_path = _save_uploaded_file(
        f, os.getenv("UPLOAD_ROOT", "uploads/audio"), sid
    )

    media = Media(
        session_id=sess.id,
        sid=sid,
        kind="audio",
        filename=os.path.basename(save_path),
        mime_type=f.mimetype,
        url=None,
    )
    db.session.add(media)
    db.session.commit()

    return _ok(media=media.to_dict())


@app.route("/api/sessions/<sid>/analyze", methods=["POST"])
def analyze_session(sid):
    """
    For now this just enqueues a lightweight AnalyzeTask row.
    Your local bot or a future worker can poll these tasks and run full AI.
    """
    sess = Session.query.filter_by(sid=sid).first()
    if not sess:
        return _error("session not found", 404)

    payload = request.get_json(silent=True) or {}
    hint = payload.get("hint")

    task = AnalyzeTask(session_id=sess.id, sid=sid, status="queued")
    if hint:
        # stash the hint in error_message for now so it's visible
        task.error_message = f"hint: {hint}"

    db.session.add(task)
    db.session.commit()

    return _ok(message="analysis queued", task=task.to_dict())


if __name__ == "__main__":
    # Local dev runner (Render uses gunicorn)
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5065")), debug=True)
