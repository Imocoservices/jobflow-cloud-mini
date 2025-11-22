import os
import json
import logging
from datetime import datetime
from typing import Any, Dict, Optional, List

from flask import Flask, jsonify, request
from flask_cors import CORS

from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    String,
    DateTime,
    JSON,
    ForeignKey,
    func,
)
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import declarative_base, sessionmaker, relationship, Session as OrmSession

# Optional OpenAI import for /analyze (guarded so we don't crash if missing)
try:
    from openai import OpenAI
except Exception:  # pragma: no cover
    OpenAI = None  # type: ignore


# ------------------------------------------------------------------------------
# Config & DB setup
# ------------------------------------------------------------------------------

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///jobflow_local.db")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("jobflow_api")

engine = create_engine(DATABASE_URL, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()


# ------------------------------------------------------------------------------
# Models
# ------------------------------------------------------------------------------

class Session(Base):
    """
    WARNING: This model MUST match the existing Postgres table schema.
    We know the live table has these columns only:
      id, sid, payload, created_at, updated_at
    Do NOT add new physical columns here without a migration.
    """

    __tablename__ = "sessions"

    id = Column(Integer, primary_key=True)
    sid = Column(String(64), unique=True, nullable=False, index=True)
    payload = Column(JSON, default=dict)  # all flexible fields live inside here
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    media = relationship("Media", back_populates="session", lazy="selectin")

    # -------- helper methods --------

    def to_dict(self, include_payload: bool = True) -> Dict[str, Any]:
        data = {
            "id": self.id,
            "sid": self.sid,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

        p = self.payload or {}

        # These are *virtual* fields derived from payload only.
        # No columns are added to the DB.
        data.update(
            {
                "client_name": p.get("client_name"),
                "job_type": p.get("job_type"),
                "source": p.get("source"),
                "external_id": p.get("external_id"),
                "summary": p.get("summary"),
                "ai_notes": p.get("ai_notes"),
                "quote_items": p.get("quote_items"),
            }
        )

        if include_payload:
            data["payload"] = p

        return data


class Media(Base):
    """
    Media table keeps things simple and safe.
    If the actual DB has extra columns, that's fine;
    we just won't reference them.
    """

    __tablename__ = "media"

    id = Column(Integer, primary_key=True)
    session_id = Column(Integer, ForeignKey("sessions.id"), nullable=False)
    sid = Column(String(64), nullable=False, index=True)  # same as Session.sid for convenience
    kind = Column(String(16), nullable=False)  # "image" or "audio"
    filename = Column(String(255), nullable=False)
    mime_type = Column(String(64), nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    session = relationship("Session", back_populates="media")

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


# Create tables that don't exist yet (won't modify existing columns)
Base.metadata.create_all(bind=engine)


# ------------------------------------------------------------------------------
# Flask app
# ------------------------------------------------------------------------------

app = Flask(__name__)
CORS(app)


def get_db() -> OrmSession:
    return SessionLocal()


def get_or_create_session(db: OrmSession, sid: str) -> Session:
    """
    Fetch a session by SID; if missing, create a new one with an empty payload.
    """
    instance = db.query(Session).filter_by(sid=sid).one_or_none()
    if instance is None:
        instance = Session(sid=sid, payload={})
        db.add(instance)
        db.flush()
    return instance


# ------------------------------------------------------------------------------
# Routes: health
# ------------------------------------------------------------------------------

@app.get("/api/health")
def health() -> Any:
    return jsonify(
        {
            "ok": True,
            "service": "jobflow-cloud-mini",
            "time": datetime.utcnow().isoformat() + "Z",
        }
    )


# ------------------------------------------------------------------------------
# Routes: sessions list + detail
# ------------------------------------------------------------------------------

@app.get("/api/sessions")
def list_sessions() -> Any:
    db = get_db()
    try:
        limit = min(int(request.args.get("limit", 20)), 100)
        offset = int(request.args.get("offset", 0))

        q = (
            db.query(Session)
            .order_by(Session.created_at.desc())
            .offset(offset)
            .limit(limit)
        )

        items = [s.to_dict(include_payload=False) for s in q.all()]
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
    except SQLAlchemyError as e:
        logger.exception("Error listing sessions")
        return jsonify({"ok": False, "error": "db_error", "detail": str(e)}), 500
    finally:
        db.close()


@app.get("/api/sessions/<sid>")
def get_session(sid: str) -> Any:
    db = get_db()
    try:
        s = db.query(Session).filter_by(sid=sid).one_or_none()
        if not s:
            return jsonify({"ok": False, "error": "not_found"}), 404
        return jsonify({"ok": True, "session": s.to_dict(include_payload=True)})
    except SQLAlchemyError as e:
        logger.exception("Error fetching session")
        return jsonify({"ok": False, "error": "db_error", "detail": str(e)}), 500
    finally:
        db.close()


# ------------------------------------------------------------------------------
# Routes: upsert session (what your bulk uploader calls)
# ------------------------------------------------------------------------------

@app.post("/api/sessions/<sid>/upsert")
@app.put("/api/sessions/<sid>/upsert")
@app.post("/api/sessions/<sid>")  # backwards compatibility
def upsert_session(sid: str) -> Any:
    """
    Merge incoming payload into Session.payload without touching schema.
    """
    db = get_db()
    try:
        body = request.get_json(silent=True) or {}
        payload_in = body.get("payload") or body

        if not isinstance(payload_in, dict):
            return jsonify({"ok": False, "error": "invalid_payload"}), 400

        s = get_or_create_session(db, sid)

        # Merge into existing JSON payload
        current = s.payload or {}
        current.update(payload_in)
        s.payload = current
        s.updated_at = datetime.utcnow()

        db.add(s)
        db.commit()
        db.refresh(s)

        return jsonify({"ok": True, "session": s.to_dict(include_payload=True)})
    except SQLAlchemyError as e:
        db.rollback()
        logger.exception("Error upserting session")
        return jsonify({"ok": False, "error": "db_error", "detail": str(e)}), 500
    finally:
        db.close()


# ------------------------------------------------------------------------------
# Routes: media upload + listing
# ------------------------------------------------------------------------------

def _save_media(
    sid: str,
    kind: str,
    file_storage,
    mime_type: Optional[str],
) -> Dict[str, Any]:
    """
    Helper to save a single uploaded media file.
    """
    db = get_db()
    try:
        s = get_or_create_session(db, sid)

        # For now we just record metadata; actual file is stored in Postgres large object
        # or in future could be S3 / filesystem; here we store filename only.
        media = Media(
            session_id=s.id,
            sid=sid,
            kind=kind,
            filename=file_storage.filename,
            mime_type=mime_type,
        )
        db.add(media)
        db.commit()
        db.refresh(media)

        return media.to_dict()
    except SQLAlchemyError as e:
        db.rollback()
        logger.exception("Error saving media")
        raise
    finally:
        db.close()


@app.post("/api/sessions/<sid>/image")
def upload_image(sid: str) -> Any:
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "file_required"}), 400

    file = request.files["file"]
    try:
        media_dict = _save_media(sid, "image", file, file.mimetype)
        return jsonify({"ok": True, "media": media_dict})
    except SQLAlchemyError as e:
        return jsonify({"ok": False, "error": "db_error", "detail": str(e)}), 500


@app.post("/api/sessions/<sid>/audio")
def upload_audio(sid: str) -> Any:
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "file_required"}), 400

    file = request.files["file"]
    try:
        media_dict = _save_media(sid, "audio", file, file.mimetype)
        return jsonify({"ok": True, "media": media_dict})
    except SQLAlchemyError as e:
        return jsonify({"ok": False, "error": "db_error", "detail": str(e)}), 500


@app.get("/api/sessions/<sid>/media")
def list_media(sid: str) -> Any:
    db = get_db()
    try:
        s = db.query(Session).filter_by(sid=sid).one_or_none()
        if not s:
            return jsonify({"ok": False, "error": "not_found"}), 404
        items = [m.to_dict() for m in s.media]
        return jsonify({"ok": True, "sid": sid, "media": items})
    except SQLAlchemyError as e:
        logger.exception("Error listing media")
        return jsonify({"ok": False, "error": "db_error", "detail": str(e)}), 500
    finally:
        db.close()


# ------------------------------------------------------------------------------
# Route: AI analyze
# ------------------------------------------------------------------------------

def _openai_client() -> Optional[OpenAI]:
    if not OPENAI_API_KEY or not OpenAI:
        return None
    return OpenAI(api_key=OPENAI_API_KEY)


def _simple_analyze(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Very simple analysis using OpenAI if available.
    We keep it defensive; any error falls back to stub.
    """
    client = _openai_client()
    if client is None:
        # stub analysis if no key / library
        return {
            "summary": "AI analysis unavailable (missing OPENAI_API_KEY).",
            "ai_notes": None,
            "quote_items": [],
        }

    try:
        # Compose a basic prompt from payload fields we expect
        client_name = payload.get("client_name", "Unknown client")
        job_type = payload.get("job_type", "general work")
        notes = payload.get("notes") or payload.get("summary") or ""

        prompt = (
            "You are helping a home-services contractor summarize a job.\n"
            f"Client name: {client_name}\n"
            f"Job type: {job_type}\n"
            f"Notes / context:\n{notes}\n\n"
            "1) Give a 2–3 sentence summary of the job.\n"
            "2) Suggest 3–8 line items for a quote with description only "
            "(no prices, no totals).\n"
            "Return JSON with keys: summary (string), ai_notes (string), "
            "quote_items (list of strings)."
        )

        chat = client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
        )

        raw = chat.choices[0].message.content or "{}"
        data = json.loads(raw)

        return {
            "summary": data.get("summary"),
            "ai_notes": data.get("ai_notes"),
            "quote_items": data.get("quote_items") or [],
        }
    except Exception as e:  # pragma: no cover
        logger.exception("AI analysis failed")
        return {
            "summary": "AI analysis failed.",
            "ai_notes": str(e),
            "quote_items": [],
        }


@app.post("/api/sessions/<sid>/analyze")
def analyze_session(sid: str) -> Any:
    """
    Trigger AI analysis for a given session. Safe even if no OPENAI_API_KEY:
    - Never throws a 500 from OpenAI failure.
    - Always returns ok=True plus whatever we could compute.
    """
    db = get_db()
    try:
        s = db.query(Session).filter_by(sid=sid).one_or_none()
        if not s:
            return jsonify({"ok": False, "error": "not_found"}), 404

        payload = s.payload or {}

        ai_result = _simple_analyze(payload)

        # Merge AI results back into payload JSON
        payload.update(ai_result)
        s.payload = payload
        s.updated_at = datetime.utcnow()

        db.add(s)
        db.commit()
        db.refresh(s)

        return jsonify(
            {
                "ok": True,
                "session": s.to_dict(include_payload=True),
                "analysis": ai_result,
            }
        )
    except SQLAlchemyError as e:
        db.rollback()
        logger.exception("Error during analyze")
        return jsonify({"ok": False, "error": "db_error", "detail": str(e)}), 500
    finally:
        db.close()


# ------------------------------------------------------------------------------
# Main entry (for local debugging)
# ------------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5065"))
    app.run(host="0.0.0.0", port=port, debug=True)
