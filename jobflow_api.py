import os
import io
from datetime import datetime, timezone

from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text, inspect

# -----------------------------------
# Setup
# -----------------------------------

db = SQLAlchemy()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# -----------------------------------
# Database Models
# -----------------------------------

class Session(db.Model):
    __tablename__ = "sessions"

    id = db.Column(db.Integer, primary_key=True)
    # May be NULL for very old rows created before we added this column
    sid = db.Column(db.String(128), unique=True, index=True, nullable=True)

    # Arbitrary JSON payload – we’ll store client_name, job_type, analysis, etc.
    payload = db.Column(db.JSON, nullable=True)

    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    def safe_sid(self) -> str:
        """Return a usable SID even if the DB column is NULL."""
        if self.sid:
            return self.sid
        # Fallback so old rows don’t break anything
        return f"jobflow-{self.id}"

    def to_dict(self, include_payload: bool = True):
        payload = self.payload or {}
        client_name = (
            payload.get("client_name")
            or payload.get("client")
            or payload.get("name")
        )
        job_type = payload.get("job_type") or payload.get("type")
        summary = payload.get("summary") or payload.get("analysis", {}).get("summary")

        data = {
            "id": self.id,
            "sid": self.safe_sid(),
            "client_name": client_name,
            "job_type": job_type,
            "summary": summary,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_payload:
            data["payload"] = payload
        return data


class Media(db.Model):
    __tablename__ = "media"

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(
        db.Integer, db.ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    kind = db.Column(db.String(16), nullable=False)  # "image" or "audio"
    filename = db.Column(db.String(255), nullable=False)
    mime_type = db.Column(db.String(128), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    session = db.relationship(
        "Session", backref=db.backref("media", lazy=True, cascade="all, delete-orphan")
    )

    def to_dict(self):
        return {
            "id": self.id,
            "session_id": self.session_id,
            "sid": self.session.safe_sid() if self.session else None,
            "kind": self.kind,
            "filename": self.filename,
            "mime_type": self.mime_type,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


# -----------------------------------
# Schema bootstrap / migration
# -----------------------------------

def ensure_schema():
    """
    Make sure the DB has the tables/columns we expect.

    - If tables don't exist, create them.
    - If "sessions" exists but has no "sid" column (your old schema),
      add it via ALTER TABLE so SQLAlchemy stops throwing
      'column sessions.sid does not exist'.
    """
    engine = db.engine
    inspector = inspect(engine)
    tables = inspector.get_table_names()

    # If nothing exists yet, just create everything
    if "sessions" not in tables and "media" not in tables:
        db.create_all()
        return

    # Ensure sessions table exists
    if "sessions" not in tables:
        Session.__table__.create(engine)

    # Ensure media table exists
    if "media" not in tables:
        Media.__table__.create(engine)

    # Ensure sessions.sid column exists (this is what was blowing up before)
    session_columns = [col["name"] for col in inspector.get_columns("sessions")]
    if "sid" not in session_columns:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE sessions ADD COLUMN sid VARCHAR(128);"))


# -----------------------------------
# App factory
# -----------------------------------

def create_app():
    app = Flask(__name__)

    db_url = os.environ.get("DATABASE_URL", "")
    # Render sometimes gives postgres://, SQLAlchemy wants postgresql://
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)

    app.config["SQLALCHEMY_DATABASE_URI"] = db_url
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    CORS(app, resources={r"/api/*": {"origins": "*"}})

    db.init_app(app)

    with app.app_context():
        ensure_schema()

    # -----------------------------
    # Routes
    # -----------------------------

    @app.route("/api/health", methods=["GET"])
    def health():
        return jsonify(
            {"ok": True, "service": "jobflow-cloud-mini", "time": utcnow().isoformat()}
        )

    # ---- Sessions list ----

    @app.route("/api/sessions", methods=["GET"])
    def list_sessions():
        try:
            limit = int(request.args.get("limit", 20))
            offset = int(request.args.get("offset", 0))

            q = (
                Session.query.order_by(Session.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
            items = [s.to_dict(include_payload=False) for s in q]
            total = Session.query.count()

            return jsonify(
                {
                    "ok": True,
                    "sessions": items,
                    "total": total,
                    "limit": limit,
                    "offset": offset,
                }
            )
        except Exception as exc:
            app.logger.exception("Error listing sessions")
            return jsonify({"ok": False, "error": str(exc)}), 500

    # ---- Helpers for sid <-> model ----

    def get_or_create_session_by_sid(sid: str, payload: dict | None = None) -> Session:
        """
        Upsert helper used by /upsert and bulk uploader.

        - If a Session with this sid exists, update its payload.
        - Otherwise create one.
        """
        if not sid:
            raise ValueError("sid is required")

        sess = Session.query.filter_by(sid=sid).first()
        now = utcnow()

        if sess is None:
            sess = Session(sid=sid, payload=payload or {}, created_at=now, updated_at=now)
            db.session.add(sess)
        else:
            base_payload = sess.payload or {}
            if payload:
                base_payload.update(payload)
            sess.payload = base_payload
            sess.updated_at = now

        db.session.commit()
        return sess

    # ---- Session upsert ----

    @app.route("/api/sessions/<sid>/upsert", methods=["POST"])
    def upsert_session(sid):
        """
        JSON body from local bot / bulk uploader.
        We'll just store it in payload and later overlay AI analysis.
        """
        body = request.get_json(silent=True) or {}
        try:
            sess = get_or_create_session_by_sid(sid, body)
            return jsonify({"ok": True, "session": sess.to_dict()})
        except Exception as exc:
            app.logger.exception("Error upserting session")
            db.session.rollback()
            return jsonify({"ok": False, "error": str(exc)}), 500

    # Backwards-compat: POST /api/sessions/<sid>
    @app.route("/api/sessions/<sid>", methods=["POST"])
    def upsert_session_alias(sid):
        return upsert_session(sid)

    # ---- Session detail ----

    @app.route("/api/sessions/<sid>", methods=["GET"])
    def get_session(sid):
        sess = Session.query.filter_by(sid=sid).first()
        if not sess:
            return jsonify({"ok": False, "error": "Session not found"}), 404

        data = sess.to_dict(include_payload=True)
        data["media"] = [m.to_dict() for m in sess.media]
        return jsonify({"ok": True, "session": data})

    # ---- Media uploads ----

    def _ensure_upload_dir() -> str:
        """
        Physical storage path for uploaded media.
        Right now just /tmp/jobflow_uploads on Render.
        """
        root = os.path.join("/tmp", "jobflow_uploads")
        os.makedirs(root, exist_ok=True)
        return root

    def _save_media_file(sess: Session, kind: str, file_storage):
        if not file_storage:
            raise ValueError("file field is required")

        upload_root = _ensure_upload_dir()
        sid = sess.safe_sid()
        ext = os.path.splitext(file_storage.filename or "")[1] or ""
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
        safe_name = f"{sid}_{kind}_{timestamp}{ext}"

        sess_dir = os.path.join(upload_root, sid)
        os.makedirs(sess_dir, exist_ok=True)
        path = os.path.join(sess_dir, safe_name)
        file_storage.save(path)

        media = Media(
            session_id=sess.id,
            kind=kind,
            filename=safe_name,
            mime_type=file_storage.mimetype,
        )
        db.session.add(media)
        db.session.commit()

        return media

    @app.route("/api/sessions/<sid>/image", methods=["POST"])
    def upload_image(sid):
        try:
            file = request.files.get("file")
            if not file:
                return jsonify({"ok": False, "error": "file is required"}), 400

            sess = Session.query.filter_by(sid=sid).first()
            if not sess:
                # auto-create an empty session if needed
                sess = get_or_create_session_by_sid(sid, {})

            media = _save_media_file(sess, "image", file)
            return jsonify({"ok": True, "media": media.to_dict()})
        except Exception as exc:
            app.logger.exception("Error uploading image")
            db.session.rollback()
            return jsonify({"ok": False, "error": str(exc)}), 500

    @app.route("/api/sessions/<sid>/audio", methods=["POST"])
    def upload_audio(sid):
        try:
            file = request.files.get("file")
            if not file:
                return jsonify({"ok": False, "error": "file is required"}), 400

            sess = Session.query.filter_by(sid=sid).first()
            if not sess:
                sess = get_or_create_session_by_sid(sid, {})

            media = _save_media_file(sess, "audio", file)
            return jsonify({"ok": True, "media": media.to_dict()})
        except Exception as exc:
            app.logger.exception("Error uploading audio")
            db.session.rollback()
            return jsonify({"ok": False, "error": str(exc)}), 500

    @app.route("/api/sessions/<sid>/media", methods=["GET"])
    def list_media(sid):
        sess = Session.query.filter_by(sid=sid).first()
        if not sess:
            return jsonify({"ok": False, "error": "Session not found"}), 404

        items = [m.to_dict() for m in sess.media]
        return jsonify({"ok": True, "media": items})

    # ---- Auto-analyze stub ----
    # This does NOT call OpenAI yet – just records basic stats so
    # your bulk script can prove everything is wired correctly.

    @app.route("/api/sessions/<sid>/analyze", methods=["POST"])
    def analyze_session(sid):
        try:
            sess = Session.query.filter_by(sid=sid).first()
            if not sess:
                return jsonify({"ok": False, "error": "Session not found"}), 404

            images = [m for m in sess.media if m.kind == "image"]
            audio = [m for m in sess.media if m.kind == "audio"]

            payload = sess.payload or {}
            analysis = {
                "summary": "Auto-analysis stub completed.",
                "details": {
                    "num_media": len(sess.media),
                    "num_images": len(images),
                    "num_audio": len(audio),
                },
                "ran_at": utcnow().isoformat(),
                "engine": "stub-local",  # later: 'openai-gpt-4.1' etc.
            }
            payload["analysis"] = analysis
            sess.payload = payload
            sess.updated_at = utcnow()
            db.session.commit()

            return jsonify({"ok": True, "analysis": analysis, "session": sess.to_dict()})
        except Exception as exc:
            app.logger.exception("Error in analyze endpoint")
            db.session.rollback()
            return jsonify({"ok": False, "error": str(exc)}), 500

    return app


# Gunicorn entrypoint
app = create_app()
