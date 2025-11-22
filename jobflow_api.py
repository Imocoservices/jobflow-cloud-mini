# jobflow_api.py
#
# JobFlow Cloud Mini – simple cloud API for sessions + media + AI analysis.

import os
import uuid
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    String,
    DateTime,
    Text,
    ForeignKey,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

# Optional: OpenAI for analysis
try:
    from openai import OpenAI
except ImportError:  # safety on local without openai installed
    OpenAI = None

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

BRAND_NAME = os.environ.get("BRAND_NAME", "JobFlow Cloud Mini")
UPLOAD_ROOT = Path(os.environ.get("UPLOAD_ROOT", "uploads")).resolve()

UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)

# -----------------------------------------------------------------------------
# SQLAlchemy setup (2.x compatible)
# -----------------------------------------------------------------------------

engine = create_engine(DATABASE_URL, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()


class Session(Base):
    __tablename__ = "sessions"

    id = Column(Integer, primary_key=True)
    sid = Column(String(255), unique=True, nullable=False)
    payload = Column(JSONB)
    created_at = Column(
        DateTime(timezone=True), server_default=text("NOW()"), nullable=False
    )
    updated_at = Column(
        DateTime(timezone=True), server_default=text("NOW()"), nullable=False
    )

    media = relationship(
        "Media", back_populates="session", cascade="all, delete-orphan"
    )
    analyses = relationship(
        "Analysis", back_populates="session", cascade="all, delete-orphan"
    )

    def to_dict(
        self,
        include_payload: bool = True,
        include_media: bool = False,
        include_analysis: bool = False,
    ):
        data = {
            "id": self.id,
            "sid": self.sid,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

        payload = self.payload or {}

        if include_payload:
            data["payload"] = payload
        else:
            # Surface some useful fields from payload
            data["client_name"] = payload.get("client_name")
            data["job_type"] = payload.get("job_type")
            data["source"] = payload.get("source")
            data["external_id"] = payload.get("external_id")

        if include_media:
            data["media"] = [m.to_dict() for m in self.media]

        if include_analysis:
            latest = self.analyses[-1] if self.analyses else None
            if latest:
                data["analysis"] = latest.to_dict()

        return data


class Media(Base):
    __tablename__ = "media"

    id = Column(Integer, primary_key=True)
    session_id = Column(
        Integer, ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    sid = Column(String(255))  # duplicate of session SID for convenience
    kind = Column(String(32), nullable=False)  # "image" | "audio" | "video"
    filename = Column(Text, nullable=False)
    mime_type = Column(Text, nullable=False)
    created_at = Column(
        DateTime(timezone=True), server_default=text("NOW()"), nullable=False
    )

    session = relationship("Session", back_populates="media")

    def to_dict(self):
        return {
            "id": self.id,
            "session_id": self.session_id,
            "sid": self.sid,
            "kind": self.kind,
            "filename": self.filename,
            "mime_type": self.mime_type,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class Analysis(Base):
    __tablename__ = "analysis"

    id = Column(Integer, primary_key=True)
    session_id = Column(
        Integer, ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    sid = Column(String(255))
    summary = Column(Text)
    quote = Column(JSONB)
    raw = Column(JSONB)
    created_at = Column(
        DateTime(timezone=True), server_default=text("NOW()"), nullable=False
    )

    session = relationship("Session", back_populates="analyses")

    def to_dict(self):
        return {
            "id": self.id,
            "session_id": self.session_id,
            "sid": self.sid,
            "summary": self.summary,
            "quote": self.quote,
            "raw": self.raw,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


def ensure_schema():
    """
    Ensure core tables exist using SQLAlchemy 2.x-safe engine.begin().

    This is idempotent and safe to call at import time.
    """
    with engine.begin() as conn:
        # sessions table
        conn.execute(
            text(
                """
            CREATE TABLE IF NOT EXISTS sessions (
                id SERIAL PRIMARY KEY,
                sid VARCHAR(255) UNIQUE NOT NULL,
                payload JSONB,
                created_at TIMESTAMPTZ DEFAULT NOW() NOT NULL,
                updated_at TIMESTAMPTZ DEFAULT NOW() NOT NULL
            );
            """
            )
        )

        # media table
        conn.execute(
            text(
                """
            CREATE TABLE IF NOT EXISTS media (
                id SERIAL PRIMARY KEY,
                session_id INTEGER REFERENCES sessions(id) ON DELETE CASCADE,
                sid VARCHAR(255),
                kind VARCHAR(32) NOT NULL,
                filename TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW() NOT NULL
            );
            """
            )
        )

        # analysis table
        conn.execute(
            text(
                """
            CREATE TABLE IF NOT EXISTS analysis (
                id SERIAL PRIMARY KEY,
                session_id INTEGER REFERENCES sessions(id) ON DELETE CASCADE,
                sid VARCHAR(255),
                summary TEXT,
                quote JSONB,
                raw JSONB,
                created_at TIMESTAMPTZ DEFAULT NOW() NOT NULL
            );
            """
            )
        )


# Run schema creation at import time (how Render loads it)
ensure_schema()

# -----------------------------------------------------------------------------
# Flask app
# -----------------------------------------------------------------------------

app = Flask(__name__)
CORS(app)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _db_session():
    """Small helper so we don't repeat SessionLocal() / close() everywhere."""
    db = SessionLocal()
    return db


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def _session_or_404(db, sid: str) -> Session:
    session_obj = db.query(Session).filter_by(sid=sid).first()
    if not session_obj:
        raise KeyError(f"Session {sid} not found")
    return session_obj


def _store_uploaded_file(file_storage, sid: str, kind: str) -> str:
    """
    Save an uploaded file under UPLOAD_ROOT/<sid>/<kind>/filename
    and return the stored filename (relative).
    """
    safe_name = file_storage.filename or f"{kind}-{uuid.uuid4().hex}"
    prefix = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    stored_name = f"{prefix}_{uuid.uuid4().hex[:8]}_{safe_name}"

    folder = UPLOAD_ROOT / sid / kind
    folder.mkdir(parents=True, exist_ok=True)

    path = folder / stored_name
    file_storage.save(str(path))

    # Return relative filename path from uploads root (for your own reference)
    rel = path.relative_to(UPLOAD_ROOT)
    return str(rel)


def _openai_client():
    if not OPENAI_API_KEY or OpenAI is None:
        return None
    return OpenAI(api_key=OPENAI_API_KEY)


# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify(
        {
            "ok": True,
            "service": "jobflow-cloud-mini",
            "brand": BRAND_NAME,
            "time": datetime.utcnow().isoformat() + "Z",
        }
    )


# ---- Sessions ----------------------------------------------------------------


@app.route("/api/sessions", methods=["GET"])
def list_sessions():
    """List sessions with lightweight data (for dashboard)."""
    limit = int(request.args.get("limit", 20))
    offset = int(request.args.get("offset", 0))

    db = _db_session()
    try:
        query = (
            db.query(Session)
            .order_by(Session.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        items = [s.to_dict(include_payload=False) for s in query]
        total = db.query(Session).count()
    finally:
        db.close()

    return jsonify(
        {"ok": True, "sessions": items, "total": total, "limit": limit, "offset": offset}
    )


@app.route("/api/sessions/<sid>", methods=["GET"])
def get_session_detail(sid):
    """Full detail for a single session."""
    db = _db_session()
    try:
        session_obj = _session_or_404(db, sid)
        data = session_obj.to_dict(
            include_payload=True, include_media=True, include_analysis=True
        )
    except KeyError:
        db.close()
        return jsonify({"ok": False, "error": "Session not found"}), 404
    finally:
        db.close()

    return jsonify({"ok": True, "session": data})


@app.route("/api/sessions/<sid>/upsert", methods=["POST"])
@app.route("/api/sessions/<sid>", methods=["POST"])  # fallback
def upsert_session(sid):
    """
    Create/update a session from JSON payload.

    Your PowerShell bulk uploader posts here with fields like:
      client_name, job_type, external_id, source, etc.
    We store the entire body as `payload`.
    """
    payload = request.get_json(force=True, silent=True) or {}

    db = _db_session()
    try:
        session_obj = db.query(Session).filter_by(sid=sid).first()
        now = datetime.utcnow()

        if session_obj is None:
            session_obj = Session(sid=sid, payload=payload)
            db.add(session_obj)
        else:
            session_obj.payload = payload
            session_obj.updated_at = now

        db.commit()
        db.refresh(session_obj)

        data = session_obj.to_dict(
            include_payload=True, include_media=True, include_analysis=True
        )
    finally:
        db.close()

    return jsonify({"ok": True, "session": data})


# ---- Media upload / listing --------------------------------------------------


@app.route("/api/sessions/<sid>/media", methods=["GET"])
def list_media(sid):
    db = _db_session()
    try:
        session_obj = _session_or_404(db, sid)
        media_list = [m.to_dict() for m in session_obj.media]
    except KeyError:
        db.close()
        return jsonify({"ok": False, "error": "Session not found"}), 404
    finally:
        db.close()

    return jsonify({"ok": True, "media": media_list})


@app.route("/api/sessions/<sid>/image", methods=["POST"])
def upload_image(sid):
    file = request.files.get("file")
    if not file:
        return jsonify({"ok": False, "error": "No file uploaded"}), 400

    db = _db_session()
    try:
        session_obj = _session_or_404(db, sid)
    except KeyError:
        db.close()
        return jsonify({"ok": False, "error": "Session not found"}), 404

    stored_rel = _store_uploaded_file(file, sid=sid, kind="image")

    media_obj = Media(
        session_id=session_obj.id,
        sid=sid,
        kind="image",
        filename=stored_rel,
        mime_type=file.mimetype or "image/jpeg",
    )
    db.add(media_obj)
    db.commit()
    db.refresh(media_obj)
    db.close()

    return jsonify({"ok": True, "media": media_obj.to_dict()})


@app.route("/api/sessions/<sid>/audio", methods=["POST"])
def upload_audio(sid):
    file = request.files.get("file")
    if not file:
        return jsonify({"ok": False, "error": "No file uploaded"}), 400

    db = _db_session()
    try:
        session_obj = _session_or_404(db, sid)
    except KeyError:
        db.close()
        return jsonify({"ok": False, "error": "Session not found"}), 404

    stored_rel = _store_uploaded_file(file, sid=sid, kind="audio")

    media_obj = Media(
        session_id=session_obj.id,
        sid=sid,
        kind="audio",
        filename=stored_rel,
        mime_type=file.mimetype or "audio/m4a",
    )
    db.add(media_obj)
    db.commit()
    db.refresh(media_obj)
    db.close()

    return jsonify({"ok": True, "media": media_obj.to_dict()})


# Optional: serve raw uploaded files (for debugging / internal use only)
@app.route("/uploads/<path:relpath>", methods=["GET"])
def serve_upload(relpath):
    path = UPLOAD_ROOT / relpath
    if not path.exists():
        return jsonify({"ok": False, "error": "File not found"}), 404
    return send_from_directory(UPLOAD_ROOT, relpath)


# ---- AI analysis -------------------------------------------------------------


@app.route("/api/sessions/<sid>/analyze", methods=["POST"])
def analyze_session(sid):
    """
    Trigger AI analysis for a given session.

    Your bulk script calls this after uploading media.
    If OPENAI_API_KEY is missing, we still create a simple placeholder analysis.
    """
    db = _db_session()
    try:
        session_obj = _session_or_404(db, sid)
    except KeyError:
        db.close()
        return jsonify({"ok": False, "error": "Session not found"}), 404

    media_items = session_obj.media[:]
    payload = session_obj.payload or {}

    summary_text = None
    raw_response = None
    quote_data = None

    client = _openai_client()

    if client is not None:
        try:
            # Build a compact description for the model
            media_desc = [
                f"{m.kind}:{m.filename.split('/')[-1]}" for m in media_items
            ]
            prompt = (
                "You are helping a home-services contractor summarize a job site.\n"
                "You will receive:\n"
                "- Basic session metadata\n"
                "- A list of uploaded media filenames (photos, audio notes)\n\n"
                "Return a short, clear summary (3–6 sentences) of what this job appears "
                "to involve: location, type of work, main issues, and any measurements "
                "or materials implied by the filenames.\n\n"
                f"Session payload: {payload}\n"
                f"Media list: {media_desc}\n"
            )

            resp = client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": "You are a concise estimator assistant for a contractor.",
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
            )

            summary_text = resp.choices[0].message.content.strip()
            raw_response = {
                "id": resp.id,
                "model": resp.model,
                "usage": resp.usage.to_dict() if hasattr(resp.usage, "to_dict") else None,
            }

        except Exception as e:  # don't kill the API if OpenAI is unhappy
            summary_text = (
                f"Auto-analysis failed ({e}). "
                f"Session has {len(media_items)} media items."
            )
            raw_response = {"error": str(e)}
    else:
        summary_text = (
            f"AI disabled (no OPENAI_API_KEY). "
            f"Session has {len(media_items)} media items."
        )
        raw_response = {"notice": "OPENAI_API_KEY not configured"}

    analysis_obj = Analysis(
        session_id=session_obj.id,
        sid=sid,
        summary=summary_text,
        quote=quote_data,
        raw=raw_response,
    )

    db.add(analysis_obj)
    db.commit()
    db.refresh(analysis_obj)
    db.close()

    return jsonify({"ok": True, "analysis": analysis_obj.to_dict()})


# -----------------------------------------------------------------------------
# Main (local dev)
# -----------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5065"))
    app.run(host="0.0.0.0", port=port, debug=True)
