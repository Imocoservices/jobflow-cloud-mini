import os
import json
from datetime import datetime
from typing import List, Optional

from flask import Flask, jsonify, request
from flask_cors import CORS

from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    String,
    Text,
    DateTime,
    ForeignKey,
    JSON as SAJSON,
)
from sqlalchemy.orm import sessionmaker, scoped_session, declarative_base, relationship

from openai import OpenAI

# -------------------------------------------------------------------
# Config & setup
# -------------------------------------------------------------------

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is not set")

client = OpenAI(api_key=OPENAI_API_KEY)

MEDIA_ROOT = os.environ.get(
    "MEDIA_ROOT",
    os.path.join(os.path.dirname(__file__), "media")
)
os.makedirs(MEDIA_ROOT, exist_ok=True)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=engine))
Base = declarative_base()

app = Flask(__name__)
CORS(app)


# -------------------------------------------------------------------
# Models
# -------------------------------------------------------------------


class Session(Base):
    __tablename__ = "sessions"

    id = Column(Integer, primary_key=True)
    sid = Column(String(64), unique=True, nullable=False, index=True)
    external_id = Column(String(128))
    client_name = Column(String(255))
    job_type = Column(String(255))
    source = Column(String(64))
    summary = Column(Text)
    payload = Column(SAJSON)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    media = relationship(
        "Media",
        back_populates="session",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    analyses = relationship(
        "Analysis",
        back_populates="session",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    def to_dict(
        self,
        include_media: bool = False,
        include_analyses: bool = False,
        include_payload: bool = False,
    ):
        data = {
            "id": self.id,
            "sid": self.sid,
            "external_id": self.external_id,
            "client_name": self.client_name,
            "job_type": self.job_type,
            "source": self.source,
            "summary": self.summary,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_payload:
            data["payload"] = self.payload
        if include_media:
            data["media"] = [m.to_dict() for m in self.media]
        if include_analyses:
            data["analyses"] = [a.to_dict() for a in self.analyses]
        return data


class Media(Base):
    __tablename__ = "media"

    id = Column(Integer, primary_key=True)
    session_id = Column(Integer, ForeignKey("sessions.id"), nullable=False)
    kind = Column(String(16), nullable=False)  # "image" or "audio"
    filename = Column(String(512), nullable=False)
    mime_type = Column(String(128))
    meta = Column(SAJSON)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    session = relationship("Session", back_populates="media")

    def to_dict(self):
        return {
            "id": self.id,
            "session_id": self.session_id,
            "kind": self.kind,
            "filename": self.filename,
            "mime_type": self.mime_type,
            "meta": self.meta,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class Analysis(Base):
    __tablename__ = "analysis"

    id = Column(Integer, primary_key=True)
    session_id = Column(Integer, ForeignKey("sessions.id"), nullable=False)
    text = Column(Text, nullable=False)
    meta = Column(SAJSON)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    session = relationship("Session", back_populates="analyses")

    def to_dict(self):
        return {
            "id": self.id,
            "session_id": self.session_id,
            "text": self.text,
            "meta": self.meta,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


# Create tables if they don't exist (safe on existing DB)
Base.metadata.create_all(bind=engine)


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _db():
    # simple helper instead of dependency injection
    return SessionLocal()


def _get_session_by_sid(db, sid: str) -> Optional[Session]:
    return db.query(Session).filter(Session.sid == sid).first()


def _save_uploaded_file(file_storage, prefix: str) -> str:
    # file_storage is werkzeug FileStorage
    # returns filename (not full path)
    ext = os.path.splitext(file_storage.filename or "")[1] or ""
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
    safe_name = f"{prefix}_{timestamp}{ext}"
    full_path = os.path.join(MEDIA_ROOT, safe_name)
    file_storage.save(full_path)
    return safe_name


# -------------------------------------------------------------------
# Routes
# -------------------------------------------------------------------


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
    db = _db()
    try:
        limit = int(request.args.get("limit", 20))
        offset = int(request.args.get("offset", 0))
        q = (
            db.query(Session)
            .order_by(Session.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        items = [s.to_dict(include_media=False, include_analyses=False) for s in q]
        total = db.query(Session).count()
        return jsonify(
            {
                "ok": True,
                "limit": limit,
                "offset": offset,
                "total": total,
                "sessions": items,
            }
        )
    finally:
        db.close()


@app.route("/api/sessions/<sid>", methods=["GET"])
def get_session(sid):
    db = _db()
    try:
        s = _get_session_by_sid(db, sid)
        if not s:
            return jsonify({"ok": False, "error": "Session not found"}), 404

        return jsonify(
            {
                "ok": True,
                "session": s.to_dict(
                    include_media=True,
                    include_analyses=True,
                    include_payload=True,
                ),
            }
        )
    finally:
        db.close()


@app.route("/api/sessions/<sid>/upsert", methods=["POST"])
def upsert_session(sid):
    db = _db()
    try:
        payload = request.get_json(force=True) or {}
        client_name = payload.get("client_name")
        job_type = payload.get("job_type")
        source = payload.get("source") or "bulk_folder"
        external_id = payload.get("external_id")

        s = _get_session_by_sid(db, sid)
        now = datetime.utcnow()

        if not s:
            s = Session(
                sid=sid,
                external_id=external_id,
                client_name=client_name,
                job_type=job_type,
                source=source,
                payload=payload,
                created_at=now,
                updated_at=now,
            )
            db.add(s)
        else:
            s.external_id = external_id or s.external_id
            s.client_name = client_name or s.client_name
            s.job_type = job_type or s.job_type
            s.source = source or s.source
            s.payload = payload or s.payload
            s.updated_at = now

        db.commit()
        db.refresh(s)

        return jsonify({"ok": True, "session": s.to_dict(include_payload=True)})
    finally:
        db.close()


@app.route("/api/sessions/<sid>/image", methods=["POST"])
def upload_image(sid):
    db = _db()
    try:
        s = _get_session_by_sid(db, sid)
        if not s:
            return jsonify({"ok": False, "error": "Session not found"}), 404

        if "file" not in request.files:
            return jsonify({"ok": False, "error": "No file field in form-data"}), 400

        f = request.files["file"]
        if not f.filename:
            return jsonify({"ok": False, "error": "Empty filename"}), 400

        filename = _save_uploaded_file(f, prefix=sid + "_img")

        m = Media(
            session_id=s.id,
            kind="image",
            filename=filename,
            mime_type=f.mimetype,
            meta={},
        )
        db.add(m)
        db.commit()
        db.refresh(m)

        return jsonify({"ok": True, "media": m.to_dict()})
    finally:
        db.close()


@app.route("/api/sessions/<sid>/audio", methods=["POST"])
def upload_audio(sid):
    db = _db()
    try:
        s = _get_session_by_sid(db, sid)
        if not s:
            return jsonify({"ok": False, "error": "Session not found"}), 404

        if "file" not in request.files:
            return jsonify({"ok": False, "error": "No file field in form-data"}), 400

        f = request.files["file"]
        if not f.filename:
            return jsonify({"ok": False, "error": "Empty filename"}), 400

        filename = _save_uploaded_file(f, prefix=sid + "_audio")

        m = Media(
            session_id=s.id,
            kind="audio",
            filename=filename,
            mime_type=f.mimetype,
            meta={},
        )
        db.add(m)
        db.commit()
        db.refresh(m)

        return jsonify({"ok": True, "media": m.to_dict()})
    finally:
        db.close()


@app.route("/api/sessions/<sid>/media", methods=["GET"])
def list_media(sid):
    db = _db()
    try:
        s = _get_session_by_sid(db, sid)
        if not s:
            return jsonify({"ok": False, "error": "Session not found"}), 404

        items = [m.to_dict() for m in s.media]
        return jsonify({"ok": True, "sid": sid, "media": items})
    finally:
        db.close()


# -------------------------------------------------------------------
# NEW: AI analysis endpoint
# -------------------------------------------------------------------


@app.route("/api/sessions/<sid>/analyze", methods=["POST"])
def analyze_session(sid):
    """
    Run full AI pipeline on a session:

    - Collect audio + images from MEDIA_ROOT
    - Transcribe audio with Whisper
    - Build a contractor-style analysis with GPT-4o-mini
    - Store analysis in the `analysis` table and update session.summary
    """
    db = _db()
    try:
        s = _get_session_by_sid(db, sid)
        if not s:
            return jsonify({"ok": False, "error": "Session not found"}), 404

        if not s.media:
            return jsonify({"ok": False, "error": "No media for this session"}), 400

        audio_files: List[str] = []
        image_files: List[str] = []

        for m in s.media:
            full_path = os.path.join(MEDIA_ROOT, m.filename)
            if not os.path.exists(full_path):
                continue
            if m.kind == "audio":
                audio_files.append(full_path)
            elif m.kind == "image":
                image_files.append(full_path)

        if not audio_files and not image_files:
            return jsonify({"ok": False, "error": "No media files found on disk"}), 400

        # ---- 1) Transcribe audio ----
        transcript_parts: List[str] = []
        for af in audio_files:
            with open(af, "rb") as f:
                tr = client.audio.transcriptions.create(
                    model="whisper-1",
                    file=f,
                )
                text = getattr(tr, "text", "") or ""
                if text.strip():
                    transcript_parts.append(text.strip())

        transcript = "\n\n".join(transcript_parts) if transcript_parts else ""

        # ---- 2) Build simple image context (no vision yet) ----
        # (We’ll upgrade later to real vision; for now, filenames are context.)
        image_context = ""
        if image_files:
            image_context = "Photos attached:\n" + "\n".join(
                f"- {os.path.basename(p)}" for p in image_files
            )

        # ---- 3) Call GPT for final analysis ----
        session_meta_text = json.dumps(
            s.to_dict(include_payload=True, include_media=False),
            indent=2,
            default=str,
        )

        user_prompt = f"""
You are an expert home-improvement contractor and estimator.

We have a new job session from the JobFlow app.

SESSION META:
{session_meta_text}

VOICE TRANSCRIPT:
{transcript or "(no audio transcript)"}

IMAGE NOTES:
{image_context or "(no images)"}

Using this information, produce a structured contractor-ready analysis in plain English:

1. Short title for this job
2. One-paragraph summary of the problem
3. Bullet list of key observations
4. Recommended scope of work (ordered steps)
5. Materials list (with rough quantities where possible)
6. Labor tasks grouped by area/room
7. Potential red flags or unknowns the contractor should confirm on site
8. Suggested quote line items (description only, no prices yet)

Keep it concise but detailed enough that I can turn it into a quote.
"""

        chat = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a contractor estimator assistant."},
                {"role": "user", "content": user_prompt},
            ],
        )

        analysis_text = chat.choices[0].message.content

        # ---- 4) Store analysis in DB ----
        analysis = Analysis(
            session_id=s.id,
            text=analysis_text,
            meta={
                "audio_files": [os.path.basename(p) for p in audio_files],
                "image_files": [os.path.basename(p) for p in image_files],
            },
            created_at=datetime.utcnow(),
        )
        db.add(analysis)

        # Update session summary with a short snippet
        s.summary = analysis_text[:500]
        s.updated_at = datetime.utcnow()

        db.commit()
        db.refresh(analysis)
        db.refresh(s)

        return jsonify(
            {
                "ok": True,
                "sid": sid,
                "session": s.to_dict(
                    include_media=True,
                    include_analyses=True,
                    include_payload=True,
                ),
                "analysis": analysis.to_dict(),
            }
        )

    except Exception as e:
        db.rollback()
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        db.close()


# -------------------------------------------------------------------
# Main (local dev)
# -------------------------------------------------------------------

if __name__ == "__main__":
    # Local test run:
    app.run(host="0.0.0.0", port=5065, debug=True)
