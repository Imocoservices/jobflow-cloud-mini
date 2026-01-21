# jobflow_api.py (JobFlow Cloud Mini - "boring reliability" build)
#
# Goals:
# - /api/health always works
# - /ui always works
# - /sessions works on a fresh DB
# - Detect stale DB schema and fail honestly
# - Deterministic minimal demo UI (create -> upload -> analyze -> view)
#
# Render notes:
# - Gunicorn imports app from app.py which imports app from here.
# - We also expose /__whoami + /__routes here so we can prove what's running.

import os
import json
import uuid
import datetime as dt
from pathlib import Path

from flask import Flask, request, jsonify, send_from_directory, redirect, render_template_string
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text, bindparam


APP_NAME = "jobflow_api"

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = BASE_DIR / "jobflow_local.db"
MEDIA_DIR = BASE_DIR / "media_uploads"
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = Path(os.environ.get("JOBFLOW_DB_PATH", str(DEFAULT_DB_PATH)))
DB_URL = os.environ.get("DATABASE_URL")  # optional override
if not DB_URL:
    DB_URL = "sqlite:///" + str(DB_PATH).replace("\\", "/")


app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = DB_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)


def now_iso():
    return dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def new_session_id():
    return "s_" + uuid.uuid4().hex[:10]


# ----------------------------
# Models (v1 contract)
# ----------------------------
class Session(db.Model):
    __tablename__ = "sessions"

    id = db.Column(db.String(64), primary_key=True)
    created_at = db.Column(db.String(40), nullable=False)
    updated_at = db.Column(db.String(40), nullable=False)

    payload_json = db.Column(db.Text, nullable=False, default="{}")  # REQUIRED
    has_analysis = db.Column(db.Integer, nullable=False, default=0)

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
    On stale DB, returns (False, error_payload).
    """
    try:
        db.create_all()
    except Exception as e:
        return False, {
            "ok": False,
            "error": "DB_INIT_FAILED",
            "detail": str(e),
            "action": "If this is local SQLite, delete jobflow_local.db and restart.",
        }

    # Hard guard: v1 column exists
    try:
        with db.engine.connect() as con:
            con.execute(text("SELECT payload_json FROM sessions LIMIT 1"))
    except Exception as e:
        return False, {
            "ok": False,
            "error": "STALE_DB_SCHEMA",
            "detail": str(e),
            "action": "Wipe local DB file and restart so schema recreates cleanly.",
            "wipe_instructions": [
                "Stop service",
                f"Delete: {str(DB_PATH)} (only if using SQLite)",
                "Start service again",
            ],
        }

    return True, None


def db_health_summary():
    return {
        "app": APP_NAME,
        "db": "sqlite" if DB_URL.startswith("sqlite") else "postgres",
        "ok": True,
        "time": now_iso(),
    }


# ----------------------------
# Debug endpoints (so we can prove what is running)
# ----------------------------
@app.route("/__whoami", methods=["GET"])
def __whoami():
    return jsonify({
        "entrypoint": "jobflow_api.py",
        "file": __file__,
        "import_name": getattr(app, "import_name", None),
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
    rules.sort(key=lambda x: x["rule"])
    return jsonify(rules)


# ----------------------------
# Health (both /health and /api/health)
# ----------------------------
@app.route("/health", methods=["GET"])
def health():
    payload = db_health_summary()

    # uptime (lazy init)
    import time as _time
    if "APP_START_TS" not in globals():
        globals()["APP_START_TS"] = _time.time()
    payload["uptime_sec"] = int(_time.time() - globals()["APP_START_TS"])
    payload["version"] = globals().get("JOBFLOW_VERSION", "v1")

    ok, err = ensure_db_ok_or_fail()
    if not ok:
        payload["ok"] = False
        payload["status"] = "attention"
        payload["error"] = err.get("error", "DB_ERROR")
        payload["detail"] = err.get("detail", "")
        payload["action"] = err.get("action", "")
        payload["wipe_instructions"] = err.get("wipe_instructions", [])
        return jsonify(payload), 200

    payload["status"] = "ok"
    return jsonify(payload), 200


@app.route("/api/health", methods=["GET"])
def api_health():
    return health()


# ----------------------------
# API
# ----------------------------
@app.route("/sessions", methods=["GET", "POST"])
@app.route("/api/sessions", methods=["GET", "POST"])
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
            "source": (data.get("source") or "").strip() or "manual",
            "client_name": (data.get("client_name") or "").strip(),
            "job_type": (data.get("job_type") or "").strip(),
            "quote_total": float(data.get("quote_total") or 0.0),
            "notes": (data.get("notes") or "").strip(),
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

    session_ids = [r.id for r in rows]
    media_counts = {sid: 0 for sid in session_ids}

    if session_ids:
        with db.engine.connect() as con:
            stmt = text(
                "SELECT session_id, COUNT(*) as c "
                "FROM media WHERE session_id IN :ids "
                "GROUP BY session_id"
            ).bindparams(bindparam("ids", expanding=True))
            res = con.execute(stmt, {"ids": session_ids}).fetchall()
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


@app.route("/sessions/<session_id>", methods=["GET"])
@app.route("/api/sessions/<session_id>", methods=["GET"])
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


@app.route("/sessions/<session_id>/upload", methods=["POST"])
@app.route("/api/sessions/<session_id>/upload", methods=["POST"])
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


@app.route("/sessions/<session_id>/analyze", methods=["POST"])
@app.route("/api/sessions/<session_id>/analyze", methods=["POST"])
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

    return jsonify({"ok": True, "analysis": a.to_dict()}), 200


@app.route("/media/<session_id>/<filename>", methods=["GET"])
def serve_media(session_id, filename):
    sid_dir = MEDIA_DIR / session_id
    if not sid_dir.exists():
        return jsonify({"ok": False, "error": "NOT_FOUND"}), 404
    return send_from_directory(str(sid_dir), filename)


# ----------------------------
# Minimal UI
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
    .card { border:1px solid #ddd; border-radius:10px; padding:16px; width: 440px; }
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
  <div class="muted">Reliability build. No magic. No lies.</div>
  <div class="muted">Try: <a href="/__whoami">/__whoami</a> | <a href="/__routes">/__routes</a> | <a href="/api/health">/api/health</a></div>
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
        return render_template_string(UI_BASE, content=content), 500

    rows = Session.query.order_by(Session.updated_at.desc()).limit(50).all()
    session_ids = [r.id for r in rows]
    media_counts = {sid: 0 for sid in session_ids}

    if session_ids:
        with db.engine.connect() as con:
            stmt = text(
                "SELECT session_id, COUNT(*) as c "
                "FROM media WHERE session_id IN :ids "
                "GROUP BY session_id"
            ).bindparams(bindparam("ids", expanding=True))
            res = con.execute(stmt, {"ids": session_ids}).fetchall()
            for r in res:
                media_counts[str(r[0])] = int(r[1])

    items = []
    for r in rows:
        item = r.to_list_item()
        item["media_count"] = media_counts.get(r.id, 0)
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
        return render_template_string(UI_BASE, content=f"<pre>{json.dumps(err,indent=2)}</pre>"), 500

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
        return render_template_string(UI_BASE, content=f"<pre>{json.dumps(err,indent=2)}</pre>"), 500

    s = Session.query.filter_by(id=session_id).first()
    if not s:
        return render_template_string(UI_BASE, content="<h2>Not Found</h2>"), 404

    media = Media.query.filter_by(session_id=session_id).order_by(Media.created_at.desc()).all()
    a = Analysis.query.filter_by(session_id=session_id).order_by(Analysis.created_at.desc()).first()

    content = """
    <p><a href="/ui">&larr; Back</a></p>

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


# ----------------------------
# Local dev only
# ----------------------------
if __name__ == "__main__":
    # Render/Gunicorn ignores this block.
    port = int(os.environ.get("PORT", "10000"))
    with app.app_context():
        ensure_db_ok_or_fail()
    app.run(host="0.0.0.0", port=port, debug=False)
