# jobflow_api.py (JobFlow Cloud Mini - V1 "boring reliability" build)
#
# GOALS (in order):
# 1) /sessions always works on a fresh DB
# 2) Detect stale DB schema and fail honestly
# 3) Freeze v1 schema expectations (payload_json column exists)
# 4) Provide a minimal UI at /ui for demo flow (create -> upload -> analyze -> view)
#
# Reliability > intelligence.

import os
import json
import uuid
import datetime as dt
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from flask import Flask, request, jsonify, send_from_directory, redirect, render_template_string
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text

APP_NAME = "jobflow_cloud_mini"
JOBFLOW_VERSION = "v1"

# ----------------------------
# Paths / Config
# ----------------------------
BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = BASE_DIR / "jobflow_local.db"
MEDIA_DIR = BASE_DIR / "media_uploads"
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = Path(os.environ.get("JOBFLOW_DB_PATH", str(DEFAULT_DB_PATH)))

def now_iso():
    return dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

def new_session_id():
    return "s_" + uuid.uuid4().hex[:10]

def _guess_render_pg_domain():
    """
    Render external Postgres hostname usually looks like:
      dpg-xxxx-a.oregon-postgres.render.com
    Region can vary; default to oregon.
    Allow override via JOBFLOW_PG_DOMAIN.
    """
    override = (os.environ.get("JOBFLOW_PG_DOMAIN") or "").strip()
    if override:
        return override

    region = (os.environ.get("RENDER_REGION") or "").strip().lower()
    # Render commonly uses region names like "oregon". If unknown, default to oregon.
    if not region:
        region = "oregon"
    return f"{region}-postgres.render.com"

def normalize_database_url(db_url: str) -> str:
    """
    If DATABASE_URL hostname is a short Render ID like 'dpg-xxxx-a' (no dots),
    expand to 'dpg-xxxx-a.<region>-postgres.render.com' so DNS resolves.
    """
    if not db_url:
        return db_url

    try:
        p = urlparse(db_url)
        host = p.hostname or ""
        if host.startswith("dpg-") and "." not in host:
            domain = _guess_render_pg_domain()
            new_host = f"{host}.{domain}"

            # Rebuild netloc preserving username/password/port
            userinfo = ""
            if p.username:
                userinfo += p.username
            if p.password:
                userinfo += f":{p.password}"
            if userinfo:
                userinfo += "@"

            port = f":{p.port}" if p.port else ""
            netloc = f"{userinfo}{new_host}{port}"

            p2 = p._replace(netloc=netloc)
            return urlunparse(p2)

        return db_url
    except Exception:
        return db_url

def resolve_db_url():
    """
    Priority:
      1) DATABASE_URL (Render Postgres)
      2) JOBFLOW_DB_URL (optional manual override)
      3) SQLite file (local/dev)
    """
    db_url = (os.environ.get("DATABASE_URL") or "").strip()
    if not db_url:
        db_url = (os.environ.get("JOBFLOW_DB_URL") or "").strip()

    if db_url:
        # Some providers use postgres://; SQLAlchemy prefers postgresql:// but accepts both.
        # We'll keep as-is, but normalize host if needed.
        return normalize_database_url(db_url)

    # SQLite local (disposable)
    return "sqlite:///" + str(DB_PATH).replace("\\", "/")

DB_URL = resolve_db_url()

# ----------------------------
# Flask + SQLAlchemy
# ----------------------------
app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = DB_URL
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

    payload_json = db.Column(db.Text, nullable=False, default="{}")  # REQUIRED
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

    kind = db.Column(db.String(20), nullable=False, default="file")
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
# DB init + stale schema guard
# ----------------------------
def ensure_db_ok_or_fail():
    """
    Creates tables on fresh DB.
    On stale DB or bad connection, returns (False, error_payload).
    """
    try:
        db.create_all()
    except Exception as e:
        return False, {
            "ok": False,
            "error": "DB_INIT_FAILED",
            "detail": str(e),
            "action": "Fix DATABASE_URL on Render (attach Postgres / correct hostname) or use SQLite locally.",
        }

    # Hard guard: verify v1 column exists (payload_json)
    try:
        with db.engine.connect() as con:
            con.execute(text("SELECT payload_json FROM sessions LIMIT 1"))
    except Exception as e:
        return False, {
            "ok": False,
            "error": "STALE_OR_BAD_DB_SCHEMA",
            "detail": str(e),
            "action": "If SQLite: delete jobflow_local.db and restart. If Postgres: run migrations or reset schema.",
        }

    return True, None

def db_health_summary():
    return {
        "app": APP_NAME,
        "db_url_source": "env" if (os.environ.get("DATABASE_URL") or os.environ.get("JOBFLOW_DB_URL")) else "sqlite_default",
        "db": "sqlite" if DB_URL.startswith("sqlite") else "postgres",
        "db_url_effective": DB_URL if not DB_URL.startswith("postgres") else _redact_db_url(DB_URL),
        "time": now_iso(),
        "version": JOBFLOW_VERSION,
    }

def _redact_db_url(u: str) -> str:
    try:
        p = urlparse(u)
        # Keep scheme, host, db name; redact creds
        host = p.hostname or ""
        path = p.path or ""
        scheme = p.scheme
        return f"{scheme}://***:***@{host}{path}"
    except Exception:
        return "postgres://***"

# ----------------------------
# Debug endpoints (always available)
# ----------------------------
@app.route("/__whoami", methods=["GET"])
def __whoami():
    return jsonify({
        "entrypoint": os.environ.get("JOBFLOW_ENTRYPOINT", "jobflow_api.py"),
        "file": __file__,
        "import_name": getattr(app, "import_name", None),
        "db_url_source": "env" if (os.environ.get("DATABASE_URL") or os.environ.get("JOBFLOW_DB_URL")) else "sqlite_default",
        "db_url_effective": DB_URL if not DB_URL.startswith("postgres") else _redact_db_url(DB_URL),
    })

@app.route("/__routes", methods=["GET"])
def __routes():
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
# API
# ----------------------------
@app.route("/api/health", methods=["GET"])
@app.route("/health", methods=["GET"])
def health():
    import time as _time
    if "APP_START_TS" not in globals():
        globals()["APP_START_TS"] = _time.time()
    uptime_sec = int(_time.time() - globals()["APP_START_TS"])

    payload = db_health_summary()
    ok, err = ensure_db_ok_or_fail()
    if not ok:
        payload.update({
            "ok": False,
            "status": "attention",
            "uptime_sec": uptime_sec,
            "error": err.get("error", "DB_ERROR"),
            "detail": err.get("detail", ""),
            "action": err.get("action", ""),
        })
        return jsonify(payload), 200

    payload.update({
        "ok": True,
        "status": "ok",
        "uptime_sec": uptime_sec,
        "error": "",
    })
    return jsonify(payload), 200

@app.route("/api/sessions", methods=["GET", "POST"])
@app.route("/sessions", methods=["GET", "POST"])
def sessions():
    ok, err = ensure_db_ok_or_fail()
    if not ok:
        return jsonify(err), 500

    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        sid = new_session_id()
        t = now_iso()

        payload = {
            "name": (data.get("name") or "").strip(),
            "source": (data.get("source") or "manual").strip(),
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

    limit = int(request.args.get("limit", 50))
    offset = int(request.args.get("offset", 0))

    q = Session.query.order_by(Session.updated_at.desc())
    total = q.count()
    rows = q.offset(offset).limit(limit).all()

    session_ids = [r.id for r in rows]
    media_counts = {sid: 0 for sid in session_ids}
    if session_ids:
        with db.engine.connect() as con:
            res = con.execute(
                text("SELECT session_id, COUNT(*) as c FROM media WHERE session_id IN :ids GROUP BY session_id")
                .bindparams(ids=tuple(session_ids))
            ).fetchall()
            for r in res:
                media_counts[str(r[0])] = int(r[1])

    items = []
    for r in rows:
        item = r.to_list_item()
        item["media_count"] = media_counts.get(r.id, 0)
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
@app.route("/sessions/<session_id>", methods=["GET"])
def session_detail(session_id):
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
@app.route("/sessions/<session_id>/upload", methods=["POST"])
def upload_media(session_id):
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
@app.route("/sessions/<session_id>/analyze", methods=["POST"])
def analyze(session_id):
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

    return jsonify({"ok": True, "analysis": a.to_dict()})

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
    .row { display:flex; gap:24px; align-items:flex-start; }
    .card { border:1px solid #ddd; border-radius:10px; padding:16px; width: 420px; }
    input, button { padding:10px; font-size:14px; }
    input { width: 100%; margin:6px 0 10px; }
    table { width:100%; border-collapse: collapse; }
    td, th { border-bottom: 1px solid #eee; padding: 8px; text-align:left; font-size: 13px; }
    a { text-decoration:none; }
    .muted { color:#666; font-size: 12px; }
    .pill { display:inline-block; padding:3px 8px; border-radius:999px; border:1px solid #ddd; font-size: 12px; }
    pre { white-space: pre-wrap; word-break: break-word; }
  </style>
</head>
<body>
  <h1>JobFlow Mini <span class="pill">v1</span></h1>
  <div class="muted">Reliability build. No magic. No lies. Try: /__whoami | /__routes | /api/health</div>
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
            <div class="muted">Also check /__whoami and /api/health</div>
          </div>
        """
        return render_template_string(UI_BASE, content=content), 500

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
    return render_template_string(UI_BASE, content=content, items=items)

@app.route("/ui/create", methods=["POST"])
def ui_create():
    ok, err = ensure_db_ok_or_fail()
    if not ok:
        return jsonify(err), 500

    name = (request.form.get("name") or "").strip()
    source = (request.form.get("source") or "").strip() or "manual"

    sid = new_session_id()
    t = now_iso()
    payload = {"name": name, "source": source, "client_name": "", "job_type": "", "quote_total": 0.0, "notes": ""}

    s = Session(id=sid, created_at=t, updated_at=t, payload_json=json.dumps(payload), has_analysis=0)
    db.session.add(s)
    db.session.commit()
    return redirect(f"/ui/sessions/{sid}")

@app.route("/ui/sessions/<session_id>", methods=["GET"])
def ui_session(session_id):
    ok, err = ensure_db_ok_or_fail()
    if not ok:
        return render_template_string(UI_BASE, content=f"<pre>{json.dumps(err,indent=2)}</pre>"), 500

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
        <form method="post" action="/sessions/{{sid}}/upload" enctype="multipart/form-data">
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
        <form method="post" action="/sessions/{{sid}}/analyze">
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
    )

if __name__ == "__main__":
    # Local dev only; Render uses gunicorn.
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=False)
