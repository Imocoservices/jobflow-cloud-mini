import os
from datetime import datetime
from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text
from dotenv import load_dotenv
import openai

load_dotenv()

# ======================
# Flask + Database Setup
# ======================
app = Flask(__name__)
CORS(app)

DATABASE_URL = os.getenv("DATABASE_URL")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not DATABASE_URL:
    raise Exception("DATABASE_URL not set")

if not OPENAI_API_KEY:
    raise Exception("OPENAI_API_KEY not set")

app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)
engine = db.engine


# ======================
# Models
# ======================
class Session(db.Model):
    __tablename__ = "sessions"

    id = db.Column(db.Integer, primary_key=True)
    sid = db.Column(db.String, unique=True, nullable=False)
    job_type = db.Column(db.String)
    external_id = db.Column(db.String)
    source = db.Column(db.String)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)
    client_name = db.Column(db.String)
    payload = db.Column(db.JSON)


class Media(db.Model):
    __tablename__ = "media"

    id = db.Column(db.Integer, primary_key=True)
    sid = db.Column(db.String, nullable=False)
    session_id = db.Column(db.Integer, db.ForeignKey("sessions.id"))
    filename = db.Column(db.String)
    kind = db.Column(db.String)
    mime_type = db.Column(db.String)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# ============================
# Modern SQLAlchemy 2.0 Schema
# ============================
def ensure_schema():
    """Create tables if they do not exist."""
    with engine.begin() as conn:
        conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS sessions (
                id SERIAL PRIMARY KEY,
                sid TEXT UNIQUE NOT NULL,
                job_type TEXT,
                external_id TEXT,
                source TEXT,
                client_name TEXT,
                payload JSONB,
                created_at TIMESTAMP,
                updated_at TIMESTAMP
            );
        """)

        conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS media (
                id SERIAL PRIMARY KEY,
                sid TEXT NOT NULL,
                session_id INTEGER REFERENCES sessions(id),
                filename TEXT,
                kind TEXT,
                mime_type TEXT,
                created_at TIMESTAMP
            );
        """)


ensure_schema()


# ======================
# Health Check
# ======================
@app.get("/api/health")
def health():
    return jsonify({
        "ok": True,
        "service": "jobflow-cloud-mini",
        "time": datetime.utcnow().isoformat()
    })


# ======================
# Upsert Session
# ======================
@app.post("/api/sessions/<sid>/upsert")
def upsert_session(sid):
    try:
        data = request.get_json() or {}
        payload = data.get("payload", {})
        job_type = data.get("job_type")
        source = data.get("source")
        external_id = data.get("external_id")

        session = Session.query.filter_by(sid=sid).first()

        if session:
            session.updated_at = datetime.utcnow()
            session.job_type = job_type or session.job_type
            session.source = source or session.source
            session.external_id = external_id or session.external_id
            if payload:
                session.payload = payload
        else:
            session = Session(
                sid=sid,
                job_type=job_type,
                source=source,
                external_id=external_id,
                payload=payload,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow()
            )
            db.session.add(session)

        db.session.commit()

        return jsonify({"ok": True, "session": {"sid": sid}})

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ======================
# Upload Media
# ======================
@app.post("/api/sessions/<sid>/image")
def upload_image(sid):
    return handle_media_upload(sid, "image")


@app.post("/api/sessions/<sid>/audio")
def upload_audio(sid):
    return handle_media_upload(sid, "audio")


def handle_media_upload(sid, kind):
    try:
        file = request.files.get("file")
        if not file:
            return jsonify({"ok": False, "error": "No file"}), 400

        session = Session.query.filter_by(sid=sid).first()
        if not session:
            return jsonify({"ok": False, "error": "Session not found"}), 404

        filename = file.filename
        content_type = file.mimetype

        media = Media(
            sid=sid,
            session_id=session.id,
            filename=filename,
            kind=kind,
            mime_type=content_type,
            created_at=datetime.utcnow()
        )
        db.session.add(media)
        db.session.commit()

        return jsonify({"ok": True, "filename": filename})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ======================
# List Sessions
# ======================
@app.get("/api/sessions")
def list_sessions():
    try:
        sessions = Session.query.order_by(Session.id.desc()).limit(50).all()
        return jsonify({
            "ok": True,
            "sessions": [
                {
                    "sid": s.sid,
                    "id": s.id,
                    "created_at": s.created_at,
                    "updated_at": s.updated_at,
                    "job_type": s.job_type,
                }
                for s in sessions
            ]
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ======================
# Get Media
# ======================
@app.get("/api/sessions/<sid>/media")
def list_media(sid):
    try:
        media = Media.query.filter_by(sid=sid).order_by(Media.id).all()
        return jsonify({
            "ok": True,
            "media": [
                {
                    "id": m.id,
                    "filename": m.filename,
                    "kind": m.kind,
                    "mime_type": m.mime_type,
                    "created_at": m.created_at,
                    "sid": m.sid,
                    "session_id": m.session_id,
                }
                for m in media
            ]
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ======================
# Run App
# ======================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
