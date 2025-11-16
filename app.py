import os
import json
from datetime import datetime
from pathlib import Path

from flask import (
    Flask,
    request,
    jsonify,
    abort,
    redirect,
    url_for,
    session as flask_session,
    Response,
    render_template_string,
)

# -------------------------------------------------------------------
# Paths / config
# -------------------------------------------------------------------

ROOT_DIR = Path(__file__).resolve().parent
CLOUD_OUTPUT_DIR = ROOT_DIR / "cloud_output"
CLOUD_SESSIONS_DIR = CLOUD_OUTPUT_DIR / "sessions"
CLOUD_OUTPUT_DIR.mkdir(exist_ok=True)
CLOUD_SESSIONS_DIR.mkdir(exist_ok=True)

ACCESS_CODE = os.getenv("ACCESS_CODE", "2468")  # simple shared code for now
SECRET_KEY = os.getenv("SECRET_KEY", "change-me-super-secret")  # for login session

IMPORT_TOKEN = os.getenv("IMPORT_TOKEN", "").strip()  # optional extra protection for /api/import_session

app = Flask(__name__)
app.secret_key = SECRET_KEY


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------

def session_dir(session_id: str) -> Path:
    d = CLOUD_SESSIONS_DIR / session_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def meta_path(session_id: str) -> Path:
    return session_dir(session_id) / "meta.json"


def report_path(session_id: str) -> Path:
    return session_dir(session_id) / "report.json"


def save_session_payload(session_id: str, meta: dict, report: dict):
    sp = session_dir(session_id)
    now_iso = datetime.utcnow().isoformat() + "Z"

    # normalize meta
    meta = meta or {}
    meta.setdefault("id", session_id)
    meta.setdefault("label", session_id)
    meta.setdefault("created_at", now_iso)
    meta["updated_at"] = now_iso

    (sp / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    (sp / "report.json").write_text(json.dumps(report or {}, indent=2), encoding="utf-8")


def load_meta(session_id: str) -> dict:
    p = meta_path(session_id)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def load_report(session_id: str) -> dict:
    p = report_path(session_id)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def list_sessions():
    sessions = []
    for d in sorted(CLOUD_SESSIONS_DIR.iterdir()):
        if not d.is_dir():
            continue
        sid = d.name
        meta = load_meta(sid)
        report = load_report(sid)
        total = 0.0
        for it in report.get("quote_items", []):
            try:
                total += float(it.get("total", 0) or 0)
            except (ValueError, TypeError):
                continue
        sessions.append({
            "id": sid,
            "label": meta.get("label", sid),
            "client_name": meta.get("client_name", ""),
            "created_at": meta.get("created_at", ""),
            "updated_at": meta.get("updated_at", ""),
            "total": total,
        })
    # newest first
    sessions.sort(key=lambda s: s.get("updated_at") or "", reverse=True)
    return sessions


# -------------------------------------------------------------------
# Auth decorator
# -------------------------------------------------------------------

def login_required(fn):
    from functools import wraps

    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not flask_session.get("authed"):
            return redirect(url_for("login", next=request.path))
        return fn(*args, **kwargs)
    return wrapper


# -------------------------------------------------------------------
# API: import session from local JobFlow
# -------------------------------------------------------------------

@app.route("/api/import_session", methods=["POST"])
def api_import_session():
    """
    Called by your local JobFlow capture app.

    Expected JSON body:
    {
      "session_id": "jobflow-007",
      "meta": {...},
      "report": {...},
      "pushed_at": "...",
      "source": "jobflow_local_capture"
    }
    """
    if IMPORT_TOKEN:
        # optional shared secret to prevent randoms posting
        auth_header = request.headers.get("X-Import-Token", "")
        if auth_header != IMPORT_TOKEN:
            return "Unauthorized", 401

    data = request.get_json(silent=True) or {}
    session_id = data.get("session_id")
    if not session_id:
        return "Missing session_id", 400

    meta = data.get("meta") or {}
    report = data.get("report") or {}
    save_session_payload(session_id, meta, report)

    return jsonify({"ok": True})


# -------------------------------------------------------------------
# Auth routes
# -------------------------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        code = (request.form.get("code") or "").strip()
        if code == ACCESS_CODE:
            flask_session["authed"] = True
            next_url = request.args.get("next") or url_for("sessions_view")
            return redirect(next_url)
        else:
            return render_template_string(LOGIN_HTML, error="Invalid code", code=code)
    return render_template_string(LOGIN_HTML, error=None, code="")


@app.route("/logout")
def logout():
    flask_session.clear()
    return redirect(url_for("login"))


# -------------------------------------------------------------------
# UI pages
# -------------------------------------------------------------------

@app.route("/")
def root():
    if not flask_session.get("authed"):
        return redirect(url_for("login"))
    return redirect(url_for("sessions_view"))


@app.route("/sessions")
@login_required
def sessions_view():
    sessions = list_sessions()
    return render_template_string(SESSIONS_HTML, sessions=sessions)


@app.route("/sessions/<session_id>")
@login_required
def session_detail(session_id):
    meta = load_meta(session_id)
    report = load_report(session_id)
    if not meta and not report:
        abort(404)

    items = report.get("quote_items", [])
    total = 0.0
    for it in items:
        try:
            total += float(it.get("total", 0) or 0)
        except (ValueError, TypeError):
            continue

    return render_template_string(SESSION_DETAIL_HTML,
                                  session_id=session_id,
                                  meta=meta,
                                  report=report,
                                  items=items,
                                  total=total)


@app.route("/sessions/<session_id>/proposal")
@login_required
def session_proposal(session_id):
    meta = load_meta(session_id)
    report = load_report(session_id)
    if not meta and not report:
        abort(404)

    client_name = meta.get("client_name", "")
    job_label = meta.get("label", session_id)
    summary = report.get("summary", "")
    items = report.get("quote_items", [])

    total = 0.0
    for it in items:
        try:
            total += float(it.get("total", 0) or 0)
        except (ValueError, TypeError):
            continue

    created = meta.get("created_at", "") or report.get("generated_at", "")
    today = datetime.now().strftime("%Y-%m-%d")

    html = f"""
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8" />
  <title>Quote – {job_label}</title>
  <style>
    body {{
      font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
      margin: 24px;
      color: #111827;
    }}
    h1,h2,h3 {{ margin: 0 0 8px; }}
    .muted {{ color: #6b7280; font-size: 0.9rem; }}
    table {{
      width: 100%;
      border-collapse: collapse;
      margin-top: 12px;
    }}
    th, td {{
      border: 1px solid #d1d5db;
      padding: 6px 8px;
      font-size: 0.9rem;
    }}
    th {{
      background: #f3f4f6;
      text-align: left;
    }}
    tfoot td {{ font-weight: 600; }}
  </style>
</head>
<body>
  <h1>JobFlow AI Proposal</h1>
  <div class="muted">Session: {session_id} • Created: {created} • Printed: {today}</div>
  <hr style="margin:12px 0;" />

  <h2>Client</h2>
  <p>{client_name or "____________________"}</p>

  <h2>Job</h2>
  <p><strong>{job_label}</strong></p>

  <h3>Summary / Scope</h3>
  <p>{summary.replace("\\n", "<br/>")}</p>

  <h3>Line Items</h3>
  <table>
    <thead>
      <tr>
        <th style="width:40%;">Description</th>
        <th style="width:10%;">Qty</th>
        <th style="width:10%;">Unit</th>
        <th style="width:15%;">Unit Price</th>
        <th style="width:15%;">Line Total</th>
        <th style="width:10%;">Notes</th>
      </tr>
    </thead>
    <tbody>
"""
    for it in items:
        desc = (it.get("description") or "").replace("\n", "<br/>")
        qty = it.get("quantity", "")
        unit = it.get("unit", "")
        unit_price = it.get("unit_price", "")
        line_total = it.get("total", "")
        notes = (it.get("notes") or "").replace("\n", "<br/>")
        html += f"""
      <tr>
        <td>{desc}</td>
        <td>{qty}</td>
        <td>{unit}</td>
        <td>{unit_price}</td>
        <td>{line_total}</td>
        <td>{notes}</td>
      </tr>
"""

    html += f"""
    </tbody>
    <tfoot>
      <tr>
        <td colspan="4" style="text-align:right;">Total</td>
        <td colspan="2">${total:.2f}</td>
      </tr>
    </tfoot>
  </table>

  <p class="muted" style="margin-top:16px;">
    To save as PDF, use your browser's Print function and select "Save as PDF".
  </p>
</body>
</html>
"""
    return Response(html, mimetype="text/html")


# -------------------------------------------------------------------
# Health
# -------------------------------------------------------------------

@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.utcnow().isoformat() + "Z"})


# -------------------------------------------------------------------
# Embedded templates
# -------------------------------------------------------------------

LOGIN_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <title>JobFlow Cloud Login</title>
  <style>
    body {
      font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
      background: #020617;
      color: #e5e7eb;
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: 100vh;
      margin: 0;
    }
    .card {
      background: #020617;
      border-radius: 16px;
      border: 1px solid #1f2937;
      padding: 24px;
      width: 320px;
      box-shadow: 0 18px 40px rgba(0,0,0,0.4);
    }
    h1 { margin: 0 0 12px; font-size: 1.3rem; }
    label { font-size: 0.9rem; color: #9ca3af; }
    input[type="password"], input[type="text"] {
      width: 100%;
      margin-top: 6px;
      padding: 8px;
      border-radius: 10px;
      border: 1px solid #4b5563;
      background: #020617;
      color: #e5e7eb;
    }
    button {
      margin-top: 12px;
      width: 100%;
      padding: 8px 12px;
      border-radius: 999px;
      border: none;
      background: linear-gradient(135deg,#6366f1,#8b5cf6);
      color: white;
      font-weight: 500;
      cursor: pointer;
    }
    .error { color: #f97373; font-size: 0.85rem; margin-top: 6px; }
    .muted { font-size: 0.8rem; color: #9ca3af; margin-top: 10px; }
  </style>
</head>
<body>
  <form class="card" method="post">
    <h1>JobFlow Cloud</h1>
    <p class="muted">Enter your access code to view sessions.</p>
    <label>Access code</label>
    <input type="password" name="code" value="{{code}}">
    {% if error %}
      <div class="error">{{error}}</div>
    {% endif %}
    <button type="submit">Login</button>
  </form>
</body>
</html>
"""

SESSIONS_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <title>JobFlow Sessions</title>
  <style>
    body {
      font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
      background: #020617;
      color: #e5e7eb;
      margin: 0;
    }
    .page {
      max-width: 960px;
      margin: 0 auto;
      padding: 16px;
    }
    .header-row {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 12px;
    }
    h1 { margin: 0; font-size: 1.4rem; }
    a { color: #a5b4fc; text-decoration: none; }
    a:hover { text-decoration: underline; }
    .card {
      background: #020617;
      border-radius: 14px;
      border: 1px solid #1f2937;
      padding: 12px;
      margin-bottom: 10px;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    .meta { font-size: 0.8rem; color: #9ca3af; }
    .badge {
      font-size: 0.75rem;
      padding: 2px 8px;
      border-radius: 999px;
      border: 1px solid #4b5563;
      color: #e5e7eb;
    }
  </style>
</head>
<body>
  <div class="page">
    <div class="header-row">
      <h1>JobFlow Sessions</h1>
      <div>
        <a href="/logout">Logout</a>
      </div>
    </div>
    {% if not sessions %}
      <p>No sessions imported yet.</p>
    {% else %}
      {% for s in sessions %}
        <div class="card">
          <div>
            <div><a href="/sessions/{{s.id}}">{{s.label}}</a></div>
            <div class="meta">
              Client: {{s.client_name or "Unknown"}}<br>
              Updated: {{s.updated_at or "?"}}
            </div>
          </div>
          <div>
            <span class="badge">${{ '%.2f'|format(s.total) }}</span>
          </div>
        </div>
      {% endfor %}
    {% endif %}
  </div>
</body>
</html>
"""

SESSION_DETAIL_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <title>Session {{session_id}}</title>
  <style>
    body {
      font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
      background: #020617;
      color: #e5e7eb;
      margin: 0;
    }
    .page {
      max-width: 960px;
      margin: 0 auto;
      padding: 16px;
    }
    a { color: #a5b4fc; text-decoration: none; }
    a:hover { text-decoration: underline; }
    .muted { color: #9ca3af; font-size: 0.85rem; }
    table {
      width: 100%;
      border-collapse: collapse;
      margin-top: 10px;
      font-size: 0.9rem;
    }
    th, td {
      border: 1px solid #1f2937;
      padding: 6px 8px;
    }
    th {
      background: #020617;
    }
    .top-row {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 10px;
    }
    .btn {
      display: inline-block;
      padding: 6px 10px;
      border-radius: 999px;
      border: 1px solid #4b5563;
      color: #e5e7eb;
      text-decoration: none;
      font-size: 0.85rem;
    }
    .btn-primary {
      border-color: #6366f1;
    }
  </style>
</head>
<body>
  <div class="page">
    <div class="top-row">
      <div>
        <a href="/sessions">&larr; Back to sessions</a>
        <h1 style="margin:4px 0;">{{meta.label or session_id}}</h1>
        <div class="muted">
          Session: {{session_id}}<br>
          Client: {{meta.client_name or "Unknown"}}<br>
          Updated: {{meta.updated_at or "?"}}
        </div>
      </div>
      <div>
        <a class="btn btn-primary" href="/sessions/{{session_id}}/proposal" target="_blank">Open Proposal</a>
      </div>
    </div>

    <h2 style="margin-top:12px;">Summary</h2>
    <p>{{report.summary or "[No summary]"}}</p>

    <h3>Line Items</h3>
    {% if not items %}
      <p class="muted">No quote items.</p>
    {% else %}
      <table>
        <thead>
          <tr>
            <th>Description</th>
            <th>Qty</th>
            <th>Unit</th>
            <th>Unit Price</th>
            <th>Total</th>
            <th>Notes</th>
          </tr>
        </thead>
        <tbody>
        {% for it in items %}
          <tr>
            <td>{{it.description}}</td>
            <td>{{it.quantity}}</td>
            <td>{{it.unit}}</td>
            <td>{{it.unit_price}}</td>
            <td>{{it.total}}</td>
            <td>{{it.notes}}</td>
          </tr>
        {% endfor %}
        </tbody>
        <tfoot>
          <tr>
            <td colspan="4" style="text-align:right;">Total</td>
            <td colspan="2">${{ '%.2f'|format(total) }}</td>
          </tr>
        </tfoot>
      </table>
    {% endif %}

    <h3 style="margin-top:14px;">Raw JSON (debug)</h3>
    <pre style="background:#020617;border-radius:8px;border:1px solid #1f2937;padding:8px;font-size:0.8rem;white-space:pre-wrap;word-wrap:break-word;">{{report | tojson(indent=2)}}</pre>
  </div>
</body>
</html>
"""


# -------------------------------------------------------------------
# Main (for local testing; Render will use gunicorn)
# -------------------------------------------------------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")), debug=True)
