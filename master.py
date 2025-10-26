# master.py — Core v1 backend for JobFlow AI (Estimator + Sessions + Dashboard)
import os, json, uuid, shutil
from datetime import datetime, timezone
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory, render_template
from dotenv import load_dotenv

from utils.estimator import estimate, record_label

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", 5065))
TIMEZONE = os.getenv("TIMEZONE", "America/New_York")

OUTPUT = ROOT / "output"
SESSIONS_DIR = OUTPUT / "sessions"
OUTPUT.mkdir(parents=True, exist_ok=True)
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def session_path(session_id: str) -> Path:
    return SESSIONS_DIR / session_id

def report_path(session_id: str) -> Path:
    return session_path(session_id) / "report.json"

def read_json(p: Path, default=None):
    if not p.exists(): return default
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)

def write_json(p: Path, data):
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def seed_session(client_name: str, notes: str, quote=None):
    sid = datetime.utcnow().strftime("%Y%m%d") + "-" + uuid.uuid4().hex[:6]
    rpt = {
        "session_id": sid,
        "client_name": client_name,
        "notes": notes,
        "quote": quote or [],
        "quote_total": sum((i["quantity"]*i["unit_price"]) for i in (quote or [])),
        "payments": [],
        "created_at": now_iso(),
        "updated_at": now_iso()
    }
    write_json(report_path(sid), rpt)
    return rpt

app = Flask(__name__, template_folder=str(ROOT / "templates"))

# ---------- Web UI ----------
@app.get("/")
def root():
    return render_template("admin.html")

@app.get("/admin")
def admin():
    return render_template("admin.html")

# ---------- Sessions API ----------
@app.get("/api/sessions")
def api_list_sessions():
    sessions = []
    for p in sorted(SESSIONS_DIR.glob("*/report.json")):
        try:
            rpt = read_json(p, {})
            sessions.append({
                "session_id": rpt.get("session_id"),
                "client_name": rpt.get("client_name", ""),
            })
        except Exception:
            continue
    return jsonify({"ok": True, "sessions": sessions})

@app.get("/api/sessions/<session_id>")
def api_get_session(session_id):
    rpt = read_json(report_path(session_id), {})
    if not rpt:
        return jsonify({"ok": False, "error": "not found"}), 404
    return jsonify(rpt)

@app.patch("/api/sessions/<session_id>")
def api_patch_session(session_id):
    rpt = read_json(report_path(session_id), {})
    if not rpt:
        return jsonify({"ok": False, "error": "not found"}), 404
    data = request.get_json(force=True, silent=True) or {}
    for k in ["client_name", "notes"]:
        if k in data:
            rpt[k] = data[k]
    rpt["updated_at"] = now_iso()
    write_json(report_path(session_id), rpt)
    return jsonify({"ok": True, "session_id": session_id})

@app.patch("/api/sessions/<session_id>/quote")
def api_patch_quote(session_id):
    rpt = read_json(report_path(session_id), {})
    if not rpt: return jsonify({"ok": False, "error": "not found"}), 404
    data = request.get_json(force=True, silent=True) or {}
    rpt["quote"] = data.get("quote", rpt.get("quote", []))
    rpt["quote_total"] = float(data.get("quote_total", 0.0))
    rpt["updated_at"] = now_iso()
    write_json(report_path(session_id), rpt)
    return jsonify({"ok": True, "quote_total": rpt["quote_total"]})

# ---------- Estimator ----------
@app.post("/api/estimate")
def api_estimate():
    body = request.get_json(force=True, silent=True) or {}
    res = estimate(
        job_type = body.get("job_type","paint_interior"),
        unit_type= body.get("unit_type","sqft"),
        quantity = float(body.get("quantity", 0)),
        difficulty= body.get("difficulty","normal"),
        rush      = body.get("rush","none")
    )
    return jsonify(res)

# ---------- Card Payment Record (Phase-1) ----------
@app.post("/api/sessions/<session_id>/card_payment")
def api_card_payment(session_id):
    rpt = read_json(report_path(session_id), {})
    if not rpt: return jsonify({"ok": False, "error": "not found"}), 404
    body = request.get_json(force=True, silent=True) or {}
    amt = float(body.get("amount", 0))
    ref = (body.get("ref") or "").strip()
    if amt <= 0 or not ref:
        return jsonify({"ok": False, "error":"amount>0 and ref required"}), 400
    pay = {
        "ts": now_iso(),
        "method": "card",
        "amount": amt,
        "ref": ref,
        "status": "recorded"
    }
    rpt.setdefault("payments", []).append(pay)
    rpt["updated_at"] = now_iso()
    write_json(report_path(session_id), rpt)
    return jsonify({"ok": True, "payment": pay})

# ---------- Accept/Label (optional learning hook) ----------
@app.post("/api/sessions/<session_id>/accept")
def api_accept(session_id):
    rpt = read_json(report_path(session_id), {})
    if not rpt: return jsonify({"ok": False, "error": "not found"}), 404
    accepted = float(request.args.get("price", rpt.get("quote_total", 0.0)))
    record_label(session_id, accepted_total=accepted, context={"quote": rpt.get("quote",[])})
    return jsonify({"ok": True, "accepted_total": accepted})

# ---------- Seed demo ----------
@app.post("/api/seed-demo")
def api_seed_demo():
    s1 = seed_session("Katie H.", "Garage floor epoxy (2 car bay).", [
        {"description":"Floor prep + epoxy, 420 sqft", "quantity":1, "unit_price":1450.00}
    ])
    s2 = seed_session("Louis D.", "Interior repaint living+hall 900 sqft.", [
        {"description":"Walls+ceiling repaint, 900 sqft", "quantity":1, "unit_price":1950.00}
    ])
    return jsonify({"ok": True, "sessions": [s1["session_id"], s2["session_id"]]})

# ---------- Static health ----------
@app.get("/api/health")
def api_health():
    return jsonify({"ok": True, "time": now_iso(), "sessions": len(list(SESSIONS_DIR.glob('*/report.json')))})

if __name__ == "__main__":
    print(f"[master] starting on http://127.0.0.1:{DASHBOARD_PORT}")
    app.run(host="127.0.0.1", port=DASHBOARD_PORT, debug=True)
