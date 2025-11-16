# master.py — Flask backend for JobFlow AI (desktop + mobile PWA)

import os
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory, send_file, render_template
from dotenv import load_dotenv

# ---- env / paths ----
ROOT = Path(__file__).parent

# Load local .env (if present) and your shared relationship_bot .env
load_dotenv(ROOT / ".env")
load_dotenv(Path(r"C:\Users\Joeyv\relationship_bot\.env"))

DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "5065"))
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", f"http://127.0.0.1:{DASHBOARD_PORT}")
BIND_HOST = os.getenv("BIND_HOST", "0.0.0.0")  # allow LAN access for phones

OUTPUT = ROOT / "output"
SESSIONS_DIR = OUTPUT / "sessions"
STATIC_DIR = ROOT / "static"
TEMPLATES_DIR = ROOT / "templates"

# ---- utils ----
from utils.sessioning import get_or_create_session, list_sessions, read_report, write_report, ensure_dirs
from utils.ai import transcribe_audio, suggest_quote
from utils.exporters import export_html_quote, make_ics

# ---- app ----
app = Flask(__name__, static_folder=str(STATIC_DIR), template_folder=str(TEMPLATES_DIR))


# ---------- health ----------
@app.get("/health")
def health():
    return jsonify(ok=True, time=datetime.now(timezone.utc).isoformat(), host=BIND_HOST, port=DASHBOARD_PORT)


# ---------- desktop UI ----------
@app.get("/")
def index():
    return render_template("index.html")


# ---------- mobile PWA UI ----------
@app.get("/mobile")
def mobile():
    return render_template("mobile.html")

@app.get("/manifest.webmanifest")
def manifest():
    return send_from_directory(ROOT, "manifest.webmanifest", mimetype="application/manifest+json")

@app.get("/sw.js")
def sw():
    # service worker must be served from the app root scope
    return send_from_directory(ROOT, "sw.js", mimetype="text/javascript")


# ---------- sessions ----------
@app.get("/api/sessions")
def api_sessions():
    return jsonify(list_sessions(SESSIONS_DIR))

@app.get("/api/sessions/<sid>")
def api_get_session(sid):
    rpt = read_report(SESSIONS_DIR / sid)
    if rpt is None:
        return jsonify(error="not found"), 404
    return jsonify(rpt)

@app.post("/api/sessions/<sid>")
def api_update_session(sid):
    body = request.get_json(force=True, silent=True) or {}
    folder = SESSIONS_DIR / sid
    rpt = read_report(folder) or {"session_id": sid}
    for k in ["client_name","job_title","notes","quote","quote_total","status"]:
        if k in body:
            rpt[k] = body[k]
    rpt["updated_at"] = datetime.now(timezone.utc).isoformat()
    write_report(folder, rpt)
    return jsonify(ok=True, report=rpt)


# ---------- uploads ----------
@app.post("/api/upload/photo")
def upload_photo():
    ensure_dirs(SESSIONS_DIR)
    file = request.files.get("file")
    if not file:
        return jsonify(error="no file"), 400

    sid, folder, rpt = get_or_create_session(SESSIONS_DIR)
    up_dir = folder / "uploads"
    up_dir.mkdir(parents=True, exist_ok=True)
    fname = f"photo_{uuid.uuid4().hex}{Path(file.filename).suffix or '.jpg'}"
    fpath = up_dir / fname
    file.save(fpath)

    photos = rpt.setdefault("photos", [])
    photos.append(str(fpath.name))
    rpt["updated_at"] = datetime.now(timezone.utc).isoformat()
    write_report(folder, rpt)
    return jsonify(ok=True, session_id=sid, filename=fname)

@app.post("/api/upload/audio")
def upload_audio():
    ensure_dirs(SESSIONS_DIR)
    file = request.files.get("file")
    if not file:
        return jsonify(error="no file"), 400

    sid, folder, rpt = get_or_create_session(SESSIONS_DIR)
    up_dir = folder / "uploads"
    up_dir.mkdir(parents=True, exist_ok=True)
    fname = f"audio_{uuid.uuid4().hex}{Path(file.filename).suffix or '.m4a'}"
    fpath = up_dir / fname
    file.save(fpath)

    # Transcribe with OpenAI Whisper if key set
    transcript_text, err = transcribe_audio(fpath)
    transcripts = rpt.setdefault("transcripts", [])
    if err:
        transcripts.append({"file": fname, "error": err})
    else:
        transcripts.append({"file": fname, "text": transcript_text})

    rpt["updated_at"] = datetime.now(timezone.utc).isoformat()
    write_report(folder, rpt)
    return jsonify(ok=True, session_id=sid, filename=fname, transcript_error=err is not None)


# ---------- AI quote ----------
@app.post("/api/sessions/<sid>/suggest_quote")
def api_suggest_quote(sid):
    folder = SESSIONS_DIR / sid
    rpt = read_report(folder)
    if not rpt:
        return jsonify(error="not found"), 404

    # Prefer latest transcript text; fall back to notes
    text = ""
    if rpt.get("transcripts"):
        for t in reversed(rpt["transcripts"]):
            if t.get("text"):
                text = t["text"]; break
    if not text:
        text = rpt.get("notes","")

    items, total, error = suggest_quote(text)
    if error:
        return jsonify(error=error), 500

    rpt["quote"] = items
    rpt["quote_total"] = total
    rpt.setdefault("status","draft")
    rpt["updated_at"] = datetime.now(timezone.utc).isoformat()
    write_report(folder, rpt)
    return jsonify(ok=True, report=rpt)


# ---------- exports ----------
@app.get("/api/sessions/<sid>/export/html")
def api_export_html(sid):
    folder = SESSIONS_DIR / sid
    rpt = read_report(folder)
    if not rpt:
        return jsonify(error="not found"), 404
    html_path = export_html_quote(folder, rpt)
    return send_file(html_path, as_attachment=True, download_name=f"quote_{sid}.html")

# Optional PDF (enabled automatically if WeasyPrint is available)
try:
    import weasyprint  # type: ignore
    @app.get("/api/sessions/<sid>/export/pdf")
    def api_export_pdf(sid):
        folder = SESSIONS_DIR / sid
        rpt = read_report(folder)
        if not rpt:
            return jsonify(error="not found"), 404
        html_path = export_html_quote(folder, rpt)
        pdf_path = folder / f"quote_{sid}.pdf"
        weasyprint.HTML(filename=str(html_path)).write_pdf(str(pdf_path))
        return send_file(pdf_path, as_attachment=True, download_name=pdf_path.name)
except Exception:
    pass


# ---------- calendar ----------
@app.get("/api/sessions/<sid>/calendar.ics")
def api_calendar(sid):
    folder = SESSIONS_DIR / sid
    rpt = read_report(folder)
    if not rpt:
        return jsonify(error="not found"), 404
    ics_path = make_ics(folder, rpt)
    return send_file(ics_path, as_attachment=True, download_name=ics_path.name)


# ---------- static passthrough ----------
@app.get("/static/<path:fname>")
def static_files(fname):
    return send_from_directory(STATIC_DIR, fname)


# ---------- main ----------
if __name__ == "__main__":
    ensure_dirs(SESSIONS_DIR)
    app.run(host=BIND_HOST, port=DASHBOARD_PORT, debug=True)
