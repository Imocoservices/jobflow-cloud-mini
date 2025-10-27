# master.py
# JobFlow AI Dashboard Core (cloud + local)
# Windows 11 / Python 3.12 / Render-ready
# Minimal deps: Flask, python-dotenv, PyYAML (optional for future pricebook use)

from __future__ import annotations

import os
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

from flask import Flask, jsonify, request, Response, send_from_directory
from dotenv import load_dotenv

# -----------------------------------------------------------------------------
# App config
# -----------------------------------------------------------------------------

APP_NAME = "JobFlow AI Dashboard Core"
APP_VERSION = "1.0.0"

# Load .env if present
load_dotenv()

# Resolve ports (Render uses PORT; local uses .env or default)
DEFAULT_PORT = int(os.getenv("DASHBOARD_PORT", "5065"))
PORT = int(os.getenv("PORT", DEFAULT_PORT))  # Render injects PORT

TIMEZONE = os.getenv("TIMEZONE", "America/New_York")  # informational

ROOT = Path(__file__).parent.resolve()
OUTPUT = ROOT / "output"
SESSIONS = OUTPUT / "sessions"
HOTDROP = OUTPUT / "hotdrop"
OUTPUT.mkdir(exist_ok=True)
SESSIONS.mkdir(parents=True, exist_ok=True)
HOTDROP.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def _now_iso() -> str:
    # UTC ISO for consistency across machines
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def _jsonp(data: Any, status: int = 200):
    """jsonify + common headers"""
    rsp = jsonify(data)
    rsp.status_code = status
    rsp.headers["Access-Control-Allow-Origin"] = "*"
    rsp.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, OPTIONS"
    rsp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    rsp.headers["Cache-Control"] = "no-cache"
    return rsp

def _session_dir(session_id: str) -> Path:
    return SESSIONS / session_id

def _report_path(session_id: str) -> Path:
    return _session_dir(session_id) / "report.json"

def _safe_read_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return default

def _safe_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)

def _new_session_id(prefix: str = "voice") -> str:
    # Example: voice_20251011_194317_754111z (kept similar to your sample)
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    suffix = uuid.uuid4().hex[:6]
    return f"{prefix}_{ts}_{suffix}z"

def _init_report(base: Dict[str, Any]) -> Dict[str, Any]:
    base.setdefault("images", [])
    base.setdefault("notes", "")
    base.setdefault("quote", [])
    base.setdefault("quote_total", 0.0)
    base.setdefault("quote_finalized", False)
    base.setdefault("payment_status", "unpaid")  # unpaid | partial | paid
    base.setdefault("payments", [])
    base.setdefault("created_at", _now_iso())
    base.setdefault("updated_at", _now_iso())
    return base

def _quote_total(items: List[Dict[str, Any]]) -> float:
    total = 0.0
    for it in items or []:
        try:
            q = float(it.get("quantity", 0) or 0)
            p = float(it.get("unit_price", 0) or 0)
            total += q * p
        except Exception:
            continue
    return round(total, 2)

def _normalize_item(it: Dict[str, Any]) -> Dict[str, Any]:
    """Make flexible input shapes tolerant."""
    # common aliases
    desc = it.get("description") or it.get("desc") or it.get("name") or "Line item"
    qty = it.get("quantity", it.get("qty", 1))
    price = it.get("unit_price", it.get("price", it.get("unitPrice", 0)))
    try:
        qty = float(qty)
    except Exception:
        qty = 1.0
    try:
        price = float(price)
    except Exception:
        price = 0.0
    return {"description": str(desc), "quantity": qty, "unit_price": price}

def _report_summary(session_id: str, report: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "session_id": session_id,
        "client_name": report.get("client_name", "Client"),
        "created_at": report.get("created_at"),
        "updated_at": report.get("updated_at"),
        "job_type": report.get("job_type", ""),
        "note_preview": (report.get("notes") or "")[:160],
        "item_count": len(report.get("quote", []) or []),
        "quote_total": float(report.get("quote_total", 0)),
        "image_count": len(report.get("images", []) or []),
        "quote_finalized": bool(report.get("quote_finalized", False)),
        "payment_status": report.get("payment_status", "unpaid"),
    }

def _load_report(session_id: str) -> Tuple[Dict[str, Any], Path]:
    path = _report_path(session_id)
    report = _safe_read_json(path, {})
    return report, path

def _save_report(session_id: str, report: Dict[str, Any]) -> None:
    report["updated_at"] = _now_iso()
    _safe_write_json(_report_path(session_id), report)

def _list_session_ids() -> List[str]:
    ids = []
    for p in SESSIONS.glob("*"):
        if p.is_dir() and (p / "report.json").exists():
            ids.append(p.name)
    return sorted(ids, reverse=True)

# -----------------------------------------------------------------------------
# Admin UI (inline, avoids template/caching issues)
# -----------------------------------------------------------------------------

ADMIN_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>JobFlow AI — Admin</title>
<style>
  :root { --bg:#060b16; --card:#0f1a2f; --line:#14284d; --text:#e9f0ff; --muted:#98abc4; --btn:#0f2240; --btn-b:#16345f; }
  *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--text);font-family:system-ui,Segoe UI,Roboto,Arial}
  header{padding:16px 22px;background:#0a1426;border-bottom:1px solid #0f203a;font-weight:800;font-size:18px}
  main{max-width:1100px;margin:0 auto;padding:20px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px}
  .row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
  .btn{background:var(--btn);border:1px solid var(--btn-b);color:#d8e6f8;padding:10px 14px;border-radius:10px;font-weight:600;cursor:pointer}
  .btn:hover{border-color:#275c9e}
  .muted{color:var(--muted)}
  .grid{margin-top:12px;border-top:1px solid var(--line)}
  .head,.item{display:grid;grid-template-columns:1.3fr .9fr .6fr .5fr .8fr;gap:10px;padding:10px 4px;border-bottom:1px solid var(--line)}
  .head{font-size:12px;color:#7e93b0;text-transform:uppercase;letter-spacing:.08em}
  .click{cursor:pointer}
  pre{white-space:pre-wrap;background:#0b152a;border:1px solid #14264a;border-radius:10px;padding:10px}
  .split{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:14px}
</style>
</head>
<body>
<header>JobFlow AI — Admin</header>
<main>
  <div class="card">
    <div class="row">
      <div style="font-weight:800">Sessions</div>
      <button id="seed" class="btn">Seed Demo</button>
      <button id="refresh" class="btn">Refresh Sessions</button>
      <span id="hint" class="muted"></span>
    </div>

    <div id="list" class="grid">
      <div class="head"><div>Client</div><div>Created</div><div>Total</div><div>Items</div><div>Session ID</div></div>
      <!-- rows injected -->
    </div>

    <div class="split">
      <div>
        <h3>Details</h3>
        <pre id="detail">(select a session)</pre>
      </div>
      <div>
        <h3>Quote</h3>
        <pre id="quote">(select a session)</pre>
      </div>
    </div>
  </div>
</main>

<script>
(function(){
  const $ = (q)=>document.querySelector(q);
  let current=null;

  function row(s){
    const el=document.createElement('div');
    el.className='item click';
    el.innerHTML = `
      <div>${s.client_name}</div>
      <div class="muted" style="font-size:12px">${s.created_at}</div>
      <div>$${Number(s.quote_total||0).toFixed(2)}</div>
      <div>${s.item_count||0}</div>
      <div style="font-family:monospace">${(s.session_id||'').slice(0,18)}…</div>
    `;
    el.onclick=()=>select(s.session_id);
    return el;
  }

  async function load(){
    $("#hint").textContent="Loading…";
    const r = await fetch('/api/sessions');
    const j = await r.json();
    const box = $("#list");
    [...box.querySelectorAll('.item')].forEach(n=>n.remove());
    (j.sessions||[]).forEach(s=>box.appendChild(row(s)));
    $("#hint").textContent=`${(j.sessions||[]).length} session(s)`;
  }

  async function select(id){
    current=id;
    const [d,q] = await Promise.all([
      fetch(`/api/sessions/${encodeURIComponent(id)}`).then(r=>r.json()),
      fetch(`/api/sessions/${encodeURIComponent(id)}/quote`).then(r=>r.json()),
    ]);
    $("#detail").textContent = JSON.stringify(d,null,2);
    $("#quote").textContent = JSON.stringify(q,null,2);
  }

  async function seed(){
    const btn=$("#seed");
    btn.disabled=true; btn.textContent='Seeding…';
    try{
      await fetch('/api/seed-demo',{method:'POST'});
      await load();
    }finally{
      btn.disabled=false; btn.textContent='Seed Demo';
    }
  }

  $("#seed").addEventListener('click', seed);
  $("#refresh").addEventListener('click', load);
  load();
})();
</script>
</body>
</html>
"""

@app.get("/admin")
def admin_page():
    return Response(ADMIN_HTML, mimetype="text/html")

# -----------------------------------------------------------------------------
# API: health/version
# -----------------------------------------------------------------------------

@app.get("/api/health")
def health():
    return _jsonp({"ok": True, "ts": _now_iso()})

@app.get("/api/version")
def version():
    return _jsonp({"app": APP_NAME, "version": APP_VERSION, "port": PORT})

# -----------------------------------------------------------------------------
# API: sessions
# -----------------------------------------------------------------------------

@app.get("/api/sessions")
def api_sessions():
    """List sessions (summaries). Optional filter: ?id=<session_id>"""
    qid = request.args.get("id", "").strip()
    results: List[Dict[str, Any]] = []

    if qid:
        # Return only the specified one (if exists)
        rep, _ = _load_report(qid)
        if rep:
            results.append(_report_summary(qid, rep))
        return _jsonp({"sessions": results})

    for sid in _list_session_ids():
        rep, _ = _load_report(sid)
        if rep:
            results.append(_report_summary(sid, rep))
    return _jsonp({"sessions": results})

@app.get("/api/sessions/<session_id>")
def api_session_detail(session_id: str):
    rep, _ = _load_report(session_id)
    if not rep:
        return _jsonp({"error": "not_found", "session_id": session_id}, 404)
    # never send internal paths
    safe = dict(rep)
    return _jsonp(safe)

@app.patch("/api/sessions/<session_id>")
def api_session_patch(session_id: str):
    rep, path = _load_report(session_id)
    if not rep:
        return _jsonp({"error": "not_found", "session_id": session_id}, 404)

    body = request.get_json(silent=True) or {}
    if "notes" in body:
        rep["notes"] = str(body.get("notes") or "")
    if "client_name" in body:
        rep["client_name"] = str(body.get("client_name") or rep.get("client_name") or "Client")
    _save_report(session_id, rep)
    return _jsonp({"ok": True, "session_id": session_id})

# -----------------------------------------------------------------------------
# Quote routes
# -----------------------------------------------------------------------------

@app.get("/api/sessions/<session_id>/quote")
def api_quote_get(session_id: str):
    rep, _ = _load_report(session_id)
    if not rep:
        return _jsonp({"error": "not_found"}, 404)
    items = rep.get("quote", []) or []
    total = _quote_total(items)
    rep["quote_total"] = total
    _save_report(session_id, rep)
    return _jsonp({"session_id": session_id, "items": items, "quote_total": total, "finalized": rep.get("quote_finalized", False)})

@app.post("/api/sessions/<session_id>/quote")
def api_quote_post(session_id: str):
    """
    Accepts multiple shapes:
      1) {"description": "...", "quantity":1, "unit_price":100}
      2) {"action":"add", "item": {...}}
      3) {"items":[ {...}, {...} ]}
    """
    rep, _ = _load_report(session_id)
    if not rep:
        return _jsonp({"error": "not_found"}, 404)
    if rep.get("quote_finalized"):
        return _jsonp({"error": "finalized"}, 400)

    body = request.get_json(silent=True) or {}
    items_to_add: List[Dict[str, Any]] = []

    # shape 1
    if "description" in body or "unit_price" in body or "qty" in body or "quantity" in body:
        items_to_add.append(_normalize_item(body))

    # shape 2
    if body.get("action") == "add" and isinstance(body.get("item"), dict):
        items_to_add.append(_normalize_item(body["item"]))

    # shape 3
    if isinstance(body.get("items"), list):
        for it in body["items"]:
            if isinstance(it, dict):
                items_to_add.append(_normalize_item(it))

    if not items_to_add:
        return _jsonp({"ok": False, "msg": "No items to add"}, 400)

    rep.setdefault("quote", [])
    rep["quote"].extend(items_to_add)
    rep["quote_total"] = _quote_total(rep["quote"])
    _save_report(session_id, rep)
    return _jsonp({"ok": True, "quote_total": rep["quote_total"]})

@app.post("/api/sessions/<session_id>/quote/finalize")
def api_quote_finalize(session_id: str):
    rep, _ = _load_report(session_id)
    if not rep:
        return _jsonp({"error": "not_found"}, 404)
    rep["quote_finalized"] = True
    rep["quote_total"] = _quote_total(rep.get("quote", []))
    _save_report(session_id, rep)
    return _jsonp({"ok": True, "finalized": True, "quote_total": rep["quote_total"]})

# -----------------------------------------------------------------------------
# Payments (mock)
# -----------------------------------------------------------------------------

@app.post("/api/sessions/<session_id>/card_payment")
def api_card_payment(session_id: str):
    rep, _ = _load_report(session_id)
    if not rep:
        return _jsonp({"error": "not_found"}, 404)

    body = request.get_json(silent=True) or {}
    amount = float(body.get("amount", 0) or 0)
    brand = str(body.get("brand") or "visa")
    last4 = str(body.get("last4") or "4242")

    if amount <= 0:
        return _jsonp({"ok": False, "msg": "amount must be > 0"}, 400)

    receipt = {
        "id": f"rcpt_{uuid.uuid4().hex[:10]}",
        "amount": amount,
        "brand": brand,
        "last4": last4,
        "created_at": _now_iso(),
    }
    rep.setdefault("payments", []).append(receipt)

    total = float(rep.get("quote_total", 0) or 0)
    paid = sum(float(p.get("amount", 0) or 0) for p in rep["payments"])
    if paid <= 0:
        rep["payment_status"] = "unpaid"
    elif paid < total:
        rep["payment_status"] = "partial"
    else:
        rep["payment_status"] = "paid"

    _save_report(session_id, rep)
    return _jsonp({"ok": True, "receipt_id": receipt["id"], "payment_status": rep["payment_status"]})

# -----------------------------------------------------------------------------
# Estimate (fallback text → line items)
# -----------------------------------------------------------------------------

@app.post("/api/estimate")
def api_estimate():
    """
    Simple fallback estimator from text -> items.
    If you want to call your utils/estimator.py later, wire here.
    """
    body = request.get_json(silent=True) or {}
    text = (body.get("text") or body.get("notes") or "").lower()

    # naive heuristics
    items = []
    if "paint" in text:
        items.append({"description": "General labor/materials", "quantity": 1, "unit_price": 150.0})
    if "fence" in text:
        items.append({"description": "Fence repair labor", "quantity": 1, "unit_price": 200.0})
    if not items:
        items.append({"description": "General labor/materials", "quantity": 1, "unit_price": 150.0})

    suggested = _quote_total(items)
    return _jsonp({"ok": True, "estimate": {"summary": "Auto-estimate (fallback)", "confidence": 0.35, "items": items, "suggested_total": suggested}})

# -----------------------------------------------------------------------------
# Seed data
# -----------------------------------------------------------------------------

@app.post("/api/seed-demo")
def api_seed_demo():
    """
    Creates two example sessions (idempotent-ish).
    """
    created = []
    # Session 01 (if not exists)
    sid1 = "session_seed_01"
    p1 = _report_path(sid1)
    if not p1.exists():
        rep1 = _init_report({
            "client_name": "Client (edited)",
            "job_type": "Voice estimate",
            "notes": "Follow-up visit needed. Touch-up paint; confirm panel sizes; schedule pickup.",
        })
        rep1["session_id"] = sid1
        rep1["quote"] = [
            {"description": "Touch-up paint", "quantity": 1, "unit_price": 125.0},
            {"description": "Materials", "quantity": 1, "unit_price": 50.0},
        ]
        rep1["quote_total"] = _quote_total(rep1["quote"])
        _safe_write_json(p1, rep1)
        created.append(sid1)
    else:
        created.append(f"{sid1}_exists")

    # Session 02 (fresh id every time)
    sid2 = "session_seed_02"
    p2 = _report_path(sid2)
    rep2 = _init_report({
        "client_name": "Katie Aldi",
        "job_type": "Voice estimate",
        "notes": "Replace broken fence sections; paint the fence; trim vegetation around it.",
    })
    rep2["session_id"] = sid2
    rep2["quote"] = [
        {"description": "Replace broken fence sections", "quantity": 10, "unit_price": 75.0},
        {"description": "Paint fence", "quantity": 1, "unit_price": 75.0},
        {"description": "Trim vegetation around fence", "quantity": 1, "unit_price": 75.0},
    ]
    rep2["quote_total"] = _quote_total(rep2["quote"])
    _safe_write_json(p2, rep2)
    created.append(sid2)

    return _jsonp({"ok": True, "created": created})

# -----------------------------------------------------------------------------
# CORS preflight
# -----------------------------------------------------------------------------

@app.route("/api/<path:_any>", methods=["OPTIONS"])
def api_options(_any):
    return _jsonp({"ok": True})

# -----------------------------------------------------------------------------
# Root/help
# -----------------------------------------------------------------------------

@app.get("/")
def root_index():
    info = {
        "app": APP_NAME,
        "version": APP_VERSION,
        "admin": "/admin",
        "health": "/api/health",
        "version_api": "/api/version",
        "sessions": "/api/sessions",
        "seed_demo": "/api/seed-demo",
    }
    return _jsonp(info)

# -----------------------------------------------------------------------------
# Boot
# -----------------------------------------------------------------------------

def _print_banner():
    print("✅ JobFlow AI Configuration Loaded")
    print(f" Base:    {str(ROOT)}")
    print(f" Sessions:{str(SESSIONS)}")
    print(f" Hotdrop: {str(HOTDROP)}")
    print(f" Flask:   http://127.0.0.1:{PORT}")
    print("\n🚀 JobFlow AI Dashboard running")
    print(f"URL: http://127.0.0.1:{PORT}")
    print("\n* Tip: There are .env or .flaskenv files present. Do \"pip install python-dotenv\" to use them.")

if __name__ == "__main__":
    _print_banner()
    # Use 0.0.0.0 for Render; reloader disabled to keep logs clean there
    app.run(host="0.0.0.0", port=PORT, debug=False)
