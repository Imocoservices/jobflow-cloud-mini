# jobflow_api.py (JobFlow Cloud Mini - v1 "boring reliability" build)
#
# Goals:
# - /api/health always returns honest status
# - /ui always works for demos (even if Postgres is misconfigured on Render)
# - Prefer DATABASE_URL (Postgres) when valid
# - Auto-fallback to SQLite if DATABASE_URL is broken/unreachable
#
# Reliability > intelligence.

import os
import json
import uuid
import time
import datetime as dt
from pathlib import Path

from flask import Flask, request, jsonify, send_from_directory, redirect, render_template_string
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import create_engine, text

APP_NAME = "jobflow_api"

# ----------------------------
# Paths / Config
# ----------------------------
BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = BASE_DIR / "jobflow_local.db"
MEDIA_DIR = BASE_DIR / "media_uploads"
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

APP_START_TS = time.time()
JOBFLOW_VERSION = "v1"

DB_FALLBACK_REASON = ""
DB_SELECTED_URL = ""

def now_iso():
    return dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

def new_session_id():
    return "s_" + uuid.uuid4().hex[:10]

def _normalize_db_url(url: str) -> str:
    # Render/Heroku sometimes use postgres:// which SQLAlchemy wants as postgresql://
    u = (url or "").strip()
    if u.startswith("postgres://"):
        u = "postgresql://" + u[len("postgres://"):]
    return u

def _ensure_sslmode_require(url: str) -> str:
    # Many hosted Postgres require SSL; harmless if already present
    if not url:
        return url
    if "sslmode=" in url:
        return url
    joiner = "&" if "?" in url else "?"
    return url + f"{joiner}sslmode=require"

def _try_connect(db_url: str, timeout_sec: int = 3):
    # Quick connectivity probe so we can honestly choose DB at boot.
    eng = create_engine(
        db_url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": timeout_sec} if db_url.startswith("postgresql://") else {},
    )
    with eng.connect() as con:
        con.execute(text("SELECT 1"))
    eng.dispose()

def select_db_url() -> str:
    """
    Prefer DATABASE_URL when valid. Otherwise fall back to local SQLite
    so /ui and API remain usable for demos.
    """
    global DB_FALLBACK_REASON, DB_SELECTED_URL

    env_url = os.environ.get("DATABASE_URL", "").strip()
    if env_url:
        candidate = _normalize_db_url(env_url)
        candidate = _ensure_sslmode_require(candidate)

        try:
            _try_connect(candidate, timeout_sec=3)
            DB_SELECTED_URL = candidate
            DB_FALLBACK_REASON = ""
            return candidate
        except Exception as e:
            # Fall back to SQLite but preserve the reason
            DB_FALLBACK_REASON = f"DATABASE_URL unusable -> fallback SQLite | {type(e).__name__}: {str(e)}"

    # SQLite fallback (works locally + keeps Render demo alive)
    sqlite_path = Path(os.environ.get("JOBFLOW_DB_PATH", str(DEFAULT_DB_PATH)))
    sqlite_url = "sqlite:///" + str(sqlite_path).replace("\\", "/")
    DB_SELECTED_URL = sqlite_url
    if not DB_FALLBACK_REASON:
        DB_FALLBACK_REASON = "No DATABASE_URL set -> using SQLite"
    return sqlite_url

# ----------------------------
# Flask + SQLAlchemy
# ----------------------------
app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = select_db_url()
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

# ----------------------------
# Models (V1 contract)
# ----------------------------
class Session(db.Model):
    __tablename__ = "sessions"

    id = db.Column(db.String(64), primary_key=True)
    created_at = db.Column(db.String(40), nullable=False)
    updated_at = db.Column(db.String(40), nullable=False)

    payload_json = db.Column(db.Text, nullable=False, default="{}")
    has_analysis = db.Column(db.Integer, nullable=False, default=0)  # 0/1

    def payload(self):
        try:
            return json.loads(self.payload_json or "{}")
        except Exception:
            return {}

    def to_list_item(self):
        p = self.payload()
        quote_total = float(p.get("quote_total") or 0.0)
        return {
            "id": self.id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "name": p.get("name", ""),
            "source": p.get("source", ""),
            "client_name": p.get("client_name", ""),
            "job_type": p.get("job_type", ""),
            "quote_total": quote_total,
            "has_analysis": bool(self.has_analysis),
        }

class Media(db.Model):
    __tablename__ = "media"

    id = db.Column(db.String(64), primary_key=True)
    session_id = db.Column(db.String(64), nullable=False, index=True)
    created_at = db.Column(db.String(40), nullable=False)

    kind = db.Column(db.String(20), nullable=False, default="file")  # image/audio/file
    original_filename = db.Column(db.String(255), nullable=False)
    stored_filename = db.Column(db.String(255), nullable=False)
    size_bytes = db.Column(db.Integer, nullable=False, default=0)

    def to_dict(self):
        return {
            "id": self.id,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "kind": self.kind,
            "original_filename": self.original_filename,
            "stored_filename": self.stored_filename,
            "size_bytes": int(self.size_bytes or 0),
            "url": f"/media/{self.session_id}/{self.stored_filename}",
        }

class Analysis(db.Model):
    __tablename__ = "analysis"

    id = db.Column(db.String(64), primary_key=True)
    session_id = db.Column(db.String(64), nullable=False, index=True)
    created_at = db.Column(db.String(40), nullable=False)
    payload_json = db.Column(db.Text, nullable=False, default="{}")

    def payload(self):
        try:
            return json.loads(self.payload_json or "{}")
        except Exception:
            return {}

    def to_dict(self):
        return {
            "id": self.id,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "payload": self.payload(),
        }

# ----------------------------
# DB init / guard
# ----------------------------
def ensure_db_ok_or_fail():
    """
    Create tables if needed. Return (ok:bool, err:dict|None).
    Never crash the process; always let /api/health tell the truth.
    """
    try:
        db.create_all()
    except Exception as e:
        return False, {
            "ok": False,
            "error": "DB_INIT_FAILED",
            "detail": f"{type(e).__name__}: {str(e)}",
            "db_url_kind": "postgres" if app.config["SQLALCHEMY_DATABASE_URI"].startswith("postgresql://") else "sqlite",
            "selected_db_url": app.config["SQLALCHEMY_DATABASE_URI"],
            "fallback_reason": DB_FALLBACK_REASON,
            "action": "Fix DATABASE_URL on Render OR let SQLite be used for demo.",
        }

    # Hard guard: verify v1 column exists
    try:
        with db.engine.connect() as con:
            con.execute(text("SELECT payload_json FROM sessions LIMIT 1"))
    except Exception as e:
        return False, {
            "ok": False,
            "error": "STALE_DB_SCHEMA",
            "detail": f"{type(e).__name__}: {str(e)}",
            "action": "Wipe the DB and restart (local: delete jobflow_local.db).",
        }

    return True, None

def db_health_summary():
    return {
        "app": APP_NAME,
        "time": now_iso(),
        "version": JOBFLOW_VERSION,
        "uptime_sec": int(time.time() - APP_START_TS),
        "db_url_kind": "postgres" if app.config["SQLALCHEMY_DATABASE_URI"].startswith("postgresql://") else "sqlite",
        "selected_db_url": app.config["SQLALCHEMY_DATABASE_URI"],
        "fallback_reason": DB_FALLBACK_REASON,
    }

# ----------------------------
# Debug endpoints (prove what Render is running)
# ----------------------------
@app.route("/_whoami", methods=["GET"])
def whoami():
    return jsonify({
        "entrypoint": os.environ.get("JOBFLOW_ENTRYPOINT", "jobflow_api.py"),
        "file": __file__,
        "import_name": app.import_name,
        "db_url_kind": "postgres" if app.config["SQLALCHEMY_DATABASE_URI"].startswith("postgresql://") else "sqlite",
        "fallback_reason": DB_FALLBACK_REASON,
    })

@app.route("/_routes", methods=["GET"])
def routes():
    rules = []
    for r in app.url_map.iter_rules():
        rules.append({
            "rule": str(r),
            "endpoint": r.endpoint,
            "methods": sorted([m for m in r.methods if m not in ("HEAD", "OPTIONS")]),
        })
    rules = sorted(rules, key=lambda x: x["rule"])
    return jsonify(rules)

# ----------------------------
# API (canonical under /api/*)
# ----------------------------
@app.route("/api/health", methods=["GET"])
def api_health():
    payload = db_health_summary()

    ok, err = ensure_db_ok_or_fail()
    if not ok:
        payload.update({
            "ok": False,
            "status": "attention",
            "error": err.get("error", "DB_ERROR"),
            "detail": err.get("detail", ""),
            "action": err.get("action", ""),
        })
        return jsonify(payload), 200

    payload.update({"ok": True, "status": "ok"})
    return jsonify(payload), 200

@app.route("/api/sessions", methods=["GET", "POST"])
def api_sessions():
    ok, err = ensure_db_ok_or_fail()
    if not ok:
        return jsonify(err), 500

    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        name = (data.get("name") or "").strip()
        source = (data.get("source") or "").strip() or "manual"

        sid = new_session_id()
        t = now_iso()

        payload = {
            "name": name,
            "source": source,
            "client_name": data.get("client_name", ""),
            "job_type": data.get("job_type", ""),
            "quote_total": float(data.get("quote_total") or 0.0),
            "notes": data.get("notes", ""),
        }

        s = Session(
            id=sid,
            created_at=t,
            updated_at=t,
            payload_json=json.dumps(payload, ensure_ascii=False),
            has_analysis=0,
        )
        db.session.add(s)
        db.session.commit()
        return jsonify({"ok": True, "created": True, "session": s.to_list_item()}), 201

    # GET
    limit = int(request.args.get("limit", 50))
    offset = int(request.args.get("offset", 0))

    q = Session.query.order_by(Session.updated_at.desc())
    total = q.count()
    rows = q.offset(offset).limit(limit).all()

    items = []
    for r in rows:
        item = r.to_list_item()
        # cheap count (demo-scale)
        item["media_count"] = Media.query.filter_by(session_id=r.id).count()
        items.append(item)

    return jsonify({
        "ok": True,
        "total": total,
        "count": len(items),
        "limit": limit,
        "offset": offset,
        "sessions": items,
    })

@app.route("/api/sessions/<session_id>", methods=["GET"])
def api_session_detail(session_id):
    ok, err = ensure_db_ok_or_fail()
    if not ok:
        return jsonify(err), 500

    s = Session.query.filter_by(id=session_id).first()
    if not s:
        return jsonify({"ok": False, "error": "NOT_FOUND", "detail": f"session_id={session_id}"}), 404

    media = Media.query.filter_by(session_id=session_id).order_by(Media.created_at.desc()).all()
    a = Analysis.query.filter_by(session_id=session_id).order_by(Analysis.created_at.desc()).first()

    return jsonify({
        "ok": True,
        "session": {
            "id": s.id,
            "created_at": s.created_at,
            "updated_at": s.updated_at,
            "payload": s.payload(),
            "has_analysis": bool(s.has_analysis),
        },
        "media": [m.to_dict() for m in media],
        "analysis": a.to_dict() if a else None,
    })

@app.route("/api/sessions/<session_id>/upload", methods=["POST"])
def api_upload_media(session_id):
    ok, err = ensure_db_ok_or_fail()
    if not ok:
        return jsonify(err), 500

    s = Session.query.filter_by(id=session_id).first()
    if not s:
        return jsonify({"ok": False, "error": "NOT_FOUND", "detail": f"session_id={session_id}"}), 404

    if "file" not in request.files:
        return jsonify({"ok": False, "error": "NO_FILE", "detail": "Expected multipart form-data with field 'file'"}), 400

    f = request.files["file"]
    if not f or not f.filename:
        return jsonify({"ok": False, "error": "BAD_FILE", "detail": "Empty upload"}), 400

    sid_dir = MEDIA_DIR / session_id
    sid_dir.mkdir(parents=True, exist_ok=True)

    original = f.filename
    ext = Path(original).suffix.lower()
    mid = "m_" + uuid.uuid4().hex[:10]
    stored = f"{mid}{ext}"

    out_path = sid_dir / stored
    f.save(str(out_path))

    size_bytes = out_path.stat().st_size if out_path.exists() else 0

    kind = "file"
    if ext in [".jpg", ".jpeg", ".png", ".webp", ".gif"]:
        kind = "image"
    elif ext in [".m4a", ".mp3", ".wav", ".ogg", ".webm"]:
        kind = "audio"

    m = Media(
        id=mid,
        session_id=session_id,
        created_at=now_iso(),
        kind=kind,
        original_filename=original,
        stored_filename=stored,
        size_bytes=int(size_bytes),
    )
    db.session.add(m)

    s.updated_at = now_iso()
    db.session.commit()

    return jsonify({"ok": True, "uploaded": True, "media": m.to_dict()}), 201

@app.route("/api/sessions/<session_id>/analyze", methods=["POST"])
def api_analyze(session_id):
    ok, err = ensure_db_ok_or_fail()
    if not ok:
        return jsonify(err), 500

    s = Session.query.filter_by(id=session_id).first()
    if not s:
        return jsonify({"ok": False, "error": "NOT_FOUND", "detail": f"session_id={session_id}"}), 404

    payload = {
        "status": "demo_stub",
        "message": "Analysis stub created (v1 reliability build).",
        "created_at": now_iso(),
        "media_count": Media.query.filter_by(session_id=session_id).count(),
        "notes": "Replace with Whisper+GPT later AFTER v1 stability is locked.",
    }

    aid = "a_" + uuid.uuid4().hex[:10]
    a = Analysis(
        id=aid,
        session_id=session_id,
        created_at=now_iso(),
        payload_json=json.dumps(payload, ensure_ascii=False),
    )
    db.session.add(a)

    s.has_analysis = 1
    s.updated_at = now_iso()
    db.session.commit()

    return jsonify({"ok": True, "analysis": a.to_dict()}), 200

# Convenience aliases (keep old links working)
@app.route("/health", methods=["GET"])
def health_alias():
    return api_health()

@app.route("/sessions", methods=["GET", "POST"])
def sessions_alias():
    return api_sessions()

@app.route("/sessions/<session_id>", methods=["GET"])
def session_alias(session_id):
    return api_session_detail(session_id)

@app.route("/sessions/<session_id>/upload", methods=["POST"])
def upload_alias(session_id):
    return api_upload_media(session_id)

@app.route("/sessions/<session_id>/analyze", methods=["POST"])
def analyze_alias(session_id):
    return api_analyze(session_id)

# ----------------------------
# Media serving
# ----------------------------
@app.route("/media/<session_id>/<filename>", methods=["GET"])
def serve_media(session_id, filename):
    sid_dir = MEDIA_DIR / session_id
    if not sid_dir.exists():
        return jsonify({"ok": False, "error": "NOT_FOUND"}), 404
    return send_from_directory(str(sid_dir), filename)

# ----------------------------
# Minimal UI (demo)
# ----------------------------
UI_BASE = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>JobFlow Mini (v1)</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 24px; }
    .row { display:flex; gap:24px; align-items:flex-start; flex-wrap: wrap; }
    .card { border:1px solid #ddd; border-radius:10px; padding:16px; width: 460px; }
    input, button { padding:10px; font-size:14px; }
    input { width: 100%; margin:6px 0 10px; }
    table { width:100%; border-collapse: collapse; }
    td, th { border-bottom: 1px solid #eee; padding: 8px; text-align:left; font-size: 13px; }
    a { text-decoration:none; }
    .muted { color:#666; font-size: 12px; }
    .pill { display:inline-block; padding:3px 8px; border-radius:999px; border:1px solid #ddd; font-size: 12px; }
    pre { background:#fafafa; border:1px solid #eee; padding:10px; border-radius:10px; overflow:auto; }
  </style>
</head>
<body>
  <h1>JobFlow Mini <span class="pill">v1</span></h1>
  <div class="muted">
    Reliability build. No magic. No lies.<br/>
    Try: <a href="/_whoami">/_whoami</a> |
         <a href="/_routes">/_routes</a> |
         <a href="/api/health">/api/health</a>
  </div>
  <hr/>
  {{content}}
</body>
</html>
"""

@app.route("/", methods=["GET"])
def root():
    return redirect("/ui")

@app.route("/ui", methods=["GET"])
def ui_home():
    ok, err = ensure_db_ok_or_fail()
    if not ok:
        content = f"""
          <div class="card">
            <h2>DB ERROR</h2>
            <pre>{json.dumps(err, indent=2)}</pre>
          </div>
        """
        return render_template_string(UI_BASE, content=content), 200

    rows = Session.query.order_by(Session.updated_at.desc()).limit(50).all()
    items = []
    for r in rows:
        item = r.to_list_item()
        item["media_count"] = Media.query.filter_by(session_id=r.id).count()
        items.append(item)

    content = """
    <div class="row">
      <div class="card">
        <h2>Create Session</h2>
        <form method="post" action="/ui/create">
          <label>Name</label>
          <input name="name" placeholder="Demo Session v1" />
          <label>Source</label>
          <input name="source" placeholder="manual" />
          <button type="submit">Create</button>
        </form>
      </div>

      <div class="card">
        <h2>Sessions</h2>
        <table>
          <tr><th>ID</th><th>Updated</th><th>Media</th><th>Analysis</th></tr>
          {% for s in items %}
            <tr>
              <td><a href="/ui/sessions/{{s['id']}}">{{s['id']}}</a></td>
              <td>{{s['updated_at']}}</td>
              <td>{{s.get('media_count',0)}}</td>
              <td>{{'yes' if s.get('has_analysis') else 'no'}}</td>
            </tr>
          {% endfor %}
        </table>
      </div>
    </div>
    """
    return render_template_string(UI_BASE, content=content, items=items), 200

@app.route("/ui/create", methods=["POST"])
def ui_create():
    ok, err = ensure_db_ok_or_fail()
    if not ok:
        return render_template_string(UI_BASE, content=f"<pre>{json.dumps(err, indent=2)}</pre>"), 200

    name = (request.form.get("name") or "").strip()
    source = (request.form.get("source") or "").strip() or "manual"

    sid = new_session_id()
    t = now_iso()
    payload = {"name": name, "source": source, "client_name": "", "job_type": "", "quote_total": 0.0, "notes": ""}
    s = Session(id=sid, created_at=t, updated_at=t, payload_json=json.dumps(payload, ensure_ascii=False), has_analysis=0)
    db.session.add(s)
    db.session.commit()
    return redirect(f"/ui/sessions/{sid}")

@app.route("/ui/sessions/<session_id>", methods=["GET"])
def ui_session(session_id):
    ok, err = ensure_db_ok_or_fail()
    if not ok:
        return render_template_string(UI_BASE, content=f"<pre>{json.dumps(err, indent=2)}</pre>"), 200

    s = Session.query.filter_by(id=session_id).first()
    if not s:
        return render_template_string(UI_BASE, content="<h2>Not Found</h2>"), 404

    media = Media.query.filter_by(session_id=session_id).order_by(Media.created_at.desc()).all()
    a = Analysis.query.filter_by(session_id=session_id).order_by(Analysis.created_at.desc()).first()

    content = """
    <p><a href="/ui">← Back</a></p>

    <div class="row">
      <div class="card">
        <h2>Session {{sid}}</h2>
        <div class="muted">Created {{created}} | Updated {{updated}}</div>
        <h3>Payload</h3>
        <pre>{{payload}}</pre>
      </div>

      <div class="card">
        <h2>Upload Media</h2>
        <form method="post" action="/api/sessions/{{sid}}/upload" enctype="multipart/form-data">
          <input type="file" name="file" />
          <button type="submit">Upload</button>
        </form>

        <h3>Media</h3>
        <ul>
          {% for m in media %}
            <li>
              <span class="pill">{{m.kind}}</span>
              <a href="{{m.url}}" target="_blank">{{m.original_filename}}</a>
              <span class="muted">({{m.size_bytes}} bytes)</span>
            </li>
          {% endfor %}
        </ul>
      </div>

      <div class="card">
        <h2>Analyze</h2>
        <form method="post" action="/api/sessions/{{sid}}/analyze">
          <button type="submit">Run Analyze (stub)</button>
        </form>

        <h3>Analysis</h3>
        {% if analysis %}
          <pre>{{analysis}}</pre>
        {% else %}
          <div class="muted">No analysis yet.</div>
        {% endif %}
      </div>
    </div>
    """
    return render_template_string(
        UI_BASE,
        content=content,
        sid=session_id,
        created=s.created_at,
        updated=s.updated_at,
        payload=json.dumps(s.payload(), indent=2),
        media=[m.to_dict() for m in media],
        analysis=json.dumps(a.payload(), indent=2) if a else None,
    ), 200

# ----------------------------
# Local run
# ----------------------------
if __name__ == "__main__":
    # Always boot; /api/health will report DB truth
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="127.0.0.1", port=port, debug=False)
