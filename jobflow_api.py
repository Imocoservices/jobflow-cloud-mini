import os
from datetime import datetime

from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy

# -------------------------------------------------------------------
# Database setup
# -------------------------------------------------------------------

db = SQLAlchemy()


def _normalize_db_url(raw: str | None) -> str:
    """Render gives postgres://; SQLAlchemy 2.x wants postgresql://."""
    if not raw:
        # Local fallback if DATABASE_URL is not set
        return "sqlite:///jobflow_local.db"
    if raw.startswith("postgres://"):
        return raw.replace("postgres://", "postgresql://", 1)
    return raw


# -------------------------------------------------------------------
# Application factory
# -------------------------------------------------------------------

def create_app() -> Flask:
    app = Flask(__name__)

    db_url = _normalize_db_url(os.getenv("DATABASE_URL"))
    app.config["SQLALCHEMY_DATABASE_URI"] = db_url
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    CORS(app)
    db.init_app(app)

    # -------------------------------------------------------------------
    # Models
    # -------------------------------------------------------------------

    class Session(db.Model):
        __tablename__ = "sessions"

        id = db.Column(db.Integer, primary_key=True)
        sid = db.Column(db.String(128), unique=True, nullable=False, index=True)

        client_name = db.Column(db.String(255), nullable=True)
        job_type = db.Column(db.String(255), nullable=True)
        source = db.Column(db.String(64), nullable=True)
        external_id = db.Column(db.String(128), nullable=True)

        summary = db.Column(db.Text, nullable=True)

        # Make this nullable + default {} so upserts never crash
        payload = db.Column(db.JSON, nullable=True)

        created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
        updated_at = db.Column(
            db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
        )

        media = db.relationship(
            "Media",
            backref="session",
            lazy=True,
            cascade="all, delete-orphan",
        )

    class Media(db.Model):
        __tablename__ = "media"

        id = db.Column(db.Integer, primary_key=True)
        session_id = db.Column(
            db.Integer, db.ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
        )
        sid = db.Column(db.String(128), nullable=False, index=True)

        kind = db.Column(db.String(16), nullable=False)  # "image" or "audio"
        filename = db.Column(db.String(512), nullable=False)
        mime_type = db.Column(db.String(128), nullable=True)

        created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    # Attach models to app so we can import them elsewhere if needed
    app.Session = Session  # type: ignore[attr-defined]
    app.Media = Media      # type: ignore[attr-defined]

    # -------------------------------------------------------------------
    # One-time schema creation
    # -------------------------------------------------------------------
    with app.app_context():
        db.create_all()

    # -------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------

    def session_to_dict(s: Session) -> dict:
        return {
            "id": s.id,
            "sid": s.sid,
            "client_name": s.client_name,
            "job_type": s.job_type,
            "source": s.source,
            "external_id": s.external_id,
            "summary": s.summary,
            "payload": s.payload or {},
            "created_at": s.created_at.isoformat() + "Z" if s.created_at else None,
            "updated_at": s.updated_at.isoformat() + "Z" if s.updated_at else None,
        }

    def media_to_dict(m: Media) -> dict:
        return {
            "id": m.id,
            "session_id": m.session_id,
            "sid": m.sid,
            "kind": m.kind,
            "filename": m.filename,
            "mime_type": m.mime_type,
            "created_at": m.created_at.isoformat() + "Z" if m.created_at else None,
        }

    def get_or_create_session(sid: str, defaults: dict | None = None) -> Session:
        s = Session.query.filter_by(sid=sid).first()
        if s:
            return s

        defaults = defaults or {}
        s = Session(
            sid=sid,
            client_name=defaults.get("client_name"),
            job_type=defaults.get("job_type"),
            source=defaults.get("source"),
            external_id=defaults.get("external_id"),
            payload=defaults.get("payload") or {},
        )
        db.session.add(s)
        db.session.commit()
        return s

    # -------------------------------------------------------------------
    # Routes
    # -------------------------------------------------------------------

    @app.get("/api/health")
    def health():
        return jsonify(
            {
                "ok": True,
                "service": "jobflow-cloud-mini",
                "time": datetime.utcnow().isoformat() + "Z",
            }
        )

    # ----- Sessions list ------------------------------------------------

    @app.get("/api/sessions")
    def list_sessions():
        limit = min(int(request.args.get("limit", 20)), 100)
        offset = int(request.args.get("offset", 0))

        query = Session.query.order_by(Session.created_at.desc())
        total = query.count()
        rows = query.offset(offset).limit(limit).all()

        return jsonify(
            {
                "ok": True,
                "limit": limit,
                "offset": offset,
                "total": total,
                "sessions": [session_to_dict(s) for s in rows],
            }
        )

    # ----- Single session detail ----------------------------------------

    @app.get("/api/sessions/<sid>")
    def get_session(sid: str):
        s = Session.query.filter_by(sid=sid).first()
        if not s:
            return jsonify({"ok": False, "error": "session_not_found"}), 404
        return jsonify({"ok": True, "session": session_to_dict(s)})

    # ----- Upsert session -----------------------------------------------

    @app.post("/api/sessions/<sid>/upsert")
    def upsert_session(sid: str):
        payload_in = request.get_json(silent=True) or {}

        # payload may either be at top level or under "payload"
        payload = payload_in.get("payload") or {}
        client_name = payload_in.get("client_name") or payload.get("client_name")
        job_type = payload_in.get("job_type") or payload.get("job_type")
        source = payload_in.get("source") or payload.get("source") or "bulk_folder"
        external_id = payload_in.get("external_id") or payload.get("external_id")

        s = Session.query.filter_by(sid=sid).first()
        now = datetime.utcnow()

        if not s:
            s = Session(
                sid=sid,
                client_name=client_name,
                job_type=job_type,
                source=source,
                external_id=external_id,
                payload=payload or {},
                created_at=now,
                updated_at=now,
            )
            db.session.add(s)
        else:
            s.client_name = client_name or s.client_name
            s.job_type = job_type or s.job_type
            s.source = source or s.source
            s.external_id = external_id or s.external_id
            # merge payloads
            merged = dict(s.payload or {})
            merged.update(payload or {})
            s.payload = merged
            s.updated_at = now

        db.session.commit()
        return jsonify({"ok": True, "session": session_to_dict(s)})

    # ----- Upload image -------------------------------------------------

    @app.post("/api/sessions/<sid>/image")
    def upload_image(sid: str):
        file = request.files.get("file")
        if not file:
            return jsonify({"ok": False, "error": "missing_file"}), 400

        s = get_or_create_session(sid)

        media = Media(
            session_id=s.id,
            sid=s.sid,
            kind="image",
            filename=file.filename,
            mime_type=file.mimetype,
        )
        db.session.add(media)
        db.session.commit()

        # We discard the file bytes for now; cloud-mini is metadata-only.
        return jsonify({"ok": True, "media": media_to_dict(media)})

    # ----- Upload audio -------------------------------------------------

    @app.post("/api/sessions/<sid>/audio")
    def upload_audio(sid: str):
        file = request.files.get("file")
        if not file:
            return jsonify({"ok": False, "error": "missing_file"}), 400

        s = get_or_create_session(sid)

        media = Media(
            session_id=s.id,
            sid=s.sid,
            kind="audio",
            filename=file.filename,
            mime_type=file.mimetype,
        )
        db.session.add(media)
        db.session.commit()

        return jsonify({"ok": True, "media": media_to_dict(media)})

    # ----- List media for a session ------------------------------------

    @app.get("/api/sessions/<sid>/media")
    def list_media(sid: str):
        s = Session.query.filter_by(sid=sid).first()
        if not s:
            return jsonify({"ok": False, "error": "session_not_found"}), 404

        items = Media.query.filter_by(session_id=s.id).order_by(Media.id.asc()).all()
        return jsonify({"ok": True, "media": [media_to_dict(m) for m in items]})

    # ----- Analyze stub (always 200 for now) ----------------------------

    @app.post("/api/sessions/<sid>/analyze")
    def analyze_session(sid: str):
        """
        Stub endpoint so your bulk uploader can call /analyze without 404/500.
        Later we’ll plug in real OpenAI analysis here.
        """
        s = Session.query.filter_by(sid=sid).first()
        if not s:
            s = get_or_create_session(sid)

        # For now, just mark a timestamp in payload
        payload = dict(s.payload or {})
        payload.setdefault("analysis", {})
        payload["analysis"]["last_run"] = datetime.utcnow().isoformat() + "Z"
        s.payload = payload
        db.session.commit()

        return jsonify(
            {
                "ok": True,
                "session": session_to_dict(s),
                "analysis": payload.get("analysis"),
            }
        )

    return app


# This is what gunicorn on Render imports
app = create_app()
