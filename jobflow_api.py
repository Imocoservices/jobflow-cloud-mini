import os
from datetime import datetime

from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text

# ------------------------------------------------------------------------------
# App + DB config
# ------------------------------------------------------------------------------

app = Flask(__name__)

CORS(app, resources={r"/api/*": {"origins": "*"}})

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "sqlite:///jobflow_cloud_mini_local.db",
)

# Render sometimes gives a postgres:// URL, which SQLAlchemy dislikes
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)


# ------------------------------------------------------------------------------
# Models
# ------------------------------------------------------------------------------

class Session(db.Model):
    __tablename__ = "sessions"

    id = db.Column(db.Integer, primary_key=True)

    # Old columns – keep them but make them nullable so they don't block inserts
    user_id = db.Column(db.Integer, nullable=True)
    external_id = db.Column(db.String(128), nullable=True)
    title = db.Column(db.String(255), nullable=True)
    client_name = db.Column(db.String(255), nullable=True)
    source = db.Column(db.String(64), nullable=True)
    status = db.Column(db.String(64), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    # New API-friendly fields
    session_id = db.Column(db.String(128), unique=True, index=True, nullable=True)
    payload = db.Column(db.JSON, nullable=True)

    def to_dict(self):
        meta = (self.payload or {}).get("meta", {}) if self.payload else {}
        return {
            "id": self.id,
            "session_id": self.session_id,
            "title": meta.get("title") or self.title,
            "client_name": meta.get("client_name") or self.client_name,
            "source": meta.get("source") or self.source,
            "status": meta.get("status") or self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "payload": self.payload or {},
        }


# ------------------------------------------------------------------------------
# Schema / migrations
# ------------------------------------------------------------------------------

def ensure_schema():
    """Create tables and patch the existing Render DB schema in-place."""
    with app.app_context():
        db.create_all()

        engine = db.engine
        dialect = engine.dialect.name
        if dialect != "postgresql":
            print(f"[INIT_DB] Non-Postgres dialect ({dialect}) – skipping raw migrations")
            return

        migrations = [
            # New columns for JobFlow Cloud Mini
            "ALTER TABLE sessions "
            "ADD COLUMN IF NOT EXISTS session_id VARCHAR(128)",

            "ALTER TABLE sessions "
            "ADD COLUMN IF NOT EXISTS payload JSON",

            "CREATE INDEX IF NOT EXISTS ix_sessions_session_id "
            "ON sessions (session_id)",

            # Relax old constraints that are breaking inserts
            "ALTER TABLE sessions "
            "ALTER COLUMN user_id DROP NOT NULL",

            "ALTER TABLE sessions "
            "ALTER COLUMN external_id DROP NOT NULL",

            "ALTER TABLE sessions "
            "DROP CONSTRAINT IF EXISTS sessions_user_id_fkey",
        ]

        with engine.connect() as conn:
            for stmt in migrations:
                print(f"[INIT_DB] Running migration: {stmt}")
                conn.execute(text(stmt))

        print("[INIT_DB] Database schema ensured.")


# Run schema fix on import (works for gunicorn on Render)
ensure_schema()


# ------------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------------

def normalize_payload(data: dict) -> dict:
    """Ensure everything we care about lives under payload.meta."""
    if not isinstance(data, dict):
        data = {}

    # If client already sent a "meta" block, just keep it
    if "meta" in data and isinstance(data["meta"], dict):
        return data

    meta_keys = ("title", "client_name", "source", "status")
    meta = {k: data.get(k) for k in meta_keys if k in data}

    # Remove meta fields from top level to avoid duplication
    rest = {k: v for k, v in data.items() if k not in meta_keys}

    return {"meta": meta, **rest}


# ------------------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------------------

@app.route("/")
def root():
    return "JobFlow Cloud Mini API", 200


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/api/sessions", methods=["GET"])
def list_sessions():
    try:
        limit = int(request.args.get("limit", 20))
    except ValueError:
        limit = 20

    q = Session.query.order_by(Session.created_at.desc()).limit(limit)
    items = [s.to_dict() for s in q.all()]
    return jsonify({"status": "ok", "count": len(items), "sessions": items})


@app.route("/api/sessions/<string:sid>/upsert", methods=["POST", "PUT"])
def upsert_session(sid: str):
    """Create or update a session by session_id, storing full JSON payload."""
    raw = request.get_json(silent=True) or {}
    payload = normalize_payload(raw)

    now = datetime.utcnow()

    session = Session.query.filter_by(session_id=sid).first()

    if session is None:
        # New session
        session = Session(
            session_id=sid,
            payload=payload,
            created_at=now,
            updated_at=now,
            # Leave user_id / external_id NULL – constraints are relaxed
        )
        db.session.add(session)
    else:
        # Merge into existing payload
        existing = session.payload or {}
        existing.update(payload)
        session.payload = existing
        session.updated_at = now

    db.session.commit()

    return jsonify(
        {
            "status": "ok",
            "session": session.to_dict(),
        }
    )


# ------------------------------------------------------------------------------
# WSGI entrypoint
# ------------------------------------------------------------------------------

# For gunicorn on Render: `gunicorn jobflow_api:app`
# For local debugging: `python jobflow_api.py`
if __name__ == "__main__":
    print(f"[LOCAL] Using DATABASE_URL={DATABASE_URL}")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)), debug=True)
