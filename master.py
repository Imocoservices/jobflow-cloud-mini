# master.py — JobFlow AI (cloud mini) — full fixed script
import os, json
from pathlib import Path
from datetime import datetime, timezone
from flask import Flask, request, jsonify, send_from_directory, abort, redirect

# ---------- paths ----------
ROOT = Path(__file__).parent.resolve()
OUTPUT = ROOT / "output"
SESSIONS = OUTPUT / "sessions"
UPLOADS = OUTPUT / "uploads"
for p in (OUTPUT, SESSIONS, UPLOADS):
    p.mkdir(parents=True, exist_ok=True)

# ---------- app ----------
app = Flask(__name__)
PORT = int(os.getenv("PORT", "5065"))

ISO = "%Y-%m-%dT%H:%M:%S.%fZ"
now_iso = lambda: datetime.now(timezone.utc).strftime(ISO)

def _read(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}

def _write(p: Path, data: dict):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def _normalize(report: dict):
    d = dict(report or {})
    d["client_name"] = (d.get("client_name") or "").strip()
    d["job_type"] = (d.get("job_type") or "General").strip()
    d["created_at"] = d.get("created_at") or now_iso()
    d["updated_at"] = d.get("updated_at") or d["created_at"]
    items = d.get("quote") or []
    norm = []
    total = 0.0
    for it in items:
        desc = (it.get("description") or "").strip()
        qty = float(it.get("quantity") or 0)
        unit = float(it.get("unit_price") or 0)
        total += qty * unit
        norm.append({"description": desc, "quantity": qty, "unit_price": unit})
    d["quote"] = norm
    d["quote_total"] = round(total, 2)
    d["quote_finalized"] = bool(d.get("quote_finalized", False))
    d["notes"] = d.get("notes") or ""
    return d

def ensure_session_dirs(sid: str):
    sdir = SESSIONS / sid
    (sdir / "gallery").mkdir(parents=True, exist_ok=True)
    return sdir

def get_payload(sid: str):
    sdir = ensure_session_dirs(sid)
    data = _normalize(_read(sdir / "report.json"))
    imgs = []
    gdir = sdir / "gallery"
    if gdir.exists():
        for p in sorted(gdir.iterdir()):
            if p.is_file():
                imgs.append({"filename": p.name, "url": f"/uploads/{sid}/{p.name}"})
    return {
        "session_id": sid,
        "client_name": data["client_name"],
        "job_type": data["job_type"],
        "created_at": data["created_at"],
        "updated_at": data["updated_at"],
        "quote": data["quote"],
        "quote_total": data["quote_total"],
        "quote_finalized": data["quote_finalized"],
        "notes": data["notes"],
        "images": imgs,
    }

# ---------- seed (for cloud demo) ----------
@app.post("/api/seed-demo")
def seed_demo():
    if not any(SESSIONS.iterdir()):
        for i, (name, job, items) in enumerate(
            [
                ("Client 1", "Painting", [
                    {"description": "Prep & tape", "quantity": 2, "unit_price": 75},
                    {"description": "2 coats", "quantity": 3, "unit_price": 150},
                ]),
                ("Client 2", "Drywall", [
                    {"description": "Patch holes", "quantity": 4, "unit_price": 25},
                    {"description": "Skim coat", "quantity": 1, "unit_price": 180},
                ]),
                ("Client 3", "Painting", [
                    {"description": "Accent wall", "quantity": 1, "unit_price": 250},
                    {"description": "Cleanup", "quantity": 1, "unit_price": 50},
                ]),
            ],
            start=1,
        ):
            sid = f"client_{i}"
            sdir = ensure_session_dirs(sid)
            data = _normalize({
                "client_name": name,
                "job_type": job,
                "created_at": now_iso(),
                "updated_at": now_iso(),
                "quote": items,
                "notes": "Demo data",
            })
            _write(sdir / "report.json", data)
    return jsonify({"ok": True})

# ---------- health ----------
@app.get("/api/health")
def health():
    return jsonify({"status": "ok", "timestamp": now_iso()})

# ---------- sessions API ----------
@app.get("/api/sessions")
def list_sessions():
    rows = []
    for sdir in sorted(SESSIONS.iterdir()):
        if not sdir.is_dir():
            continue
        data = _normalize(_read(sdir / "report.json"))
        rows.append({
            "session_id": sdir.name,
            "client_name": data["client_name"],
            "job_type": data["job_type"],
            "created_at": data["created_at"],
            "updated_at": data["updated_at"],
            "quote_total": data["quote_total"],
            "note_preview": (data["notes"] or "")[:140],
        })
    return jsonify({"sessions": rows})

@app.get("/api/sessions/<sid>")
def get_session(sid):
    sdir = SESSIONS / sid
    if not sdir.exists():
        return jsonify({"error": "not_found"}), 404
    return jsonify(get_payload(sid))

@app.route("/api/sessions/<sid>", methods=["PATCH", "POST"])
def patch_session(sid):
    body = request.get_json(silent=True) or {}
    sdir = ensure_session_dirs(sid)
    data = _normalize(_read(sdir / "report.json"))
    if "client_name" in body:
        data["client_name"] = (body.get("client_name") or "").strip()
    if "job_type" in body:
        data["job_type"] = (body.get("job_type") or "").strip()
    if "notes" in body:
        data["notes"] = body.get("notes") or ""
    data["updated_at"] = now_iso()
    _write(sdir / "report.json", _normalize(data))
    return jsonify({"ok": True})

@app.post("/api/sessions/<sid>/quote")
def save_quote(sid):
    body = request.get_json(silent=True) or {}
    items = body.get("items") or []
    sdir = ensure_session_dirs(sid)
    data = _normalize(_read(sdir / "report.json"))
    data["quote"] = items
    data["updated_at"] = now_iso()
    data = _normalize(data)
    _write(sdir / "report.json", data)
    return jsonify({"ok": True, "total": data["quote_total"]})

# ---------- uploads (optional) ----------
@app.get("/uploads/<sid>/<filename>")
def serve_upload(sid, filename):
    udir = SESSIONS / sid / "gallery"
    path = udir / filename
    if path.exists():
        return send_from_directory(udir, filename)
    abort(404)

# ---------- simple admin UI (hardened JS) ----------
ADMIN = r"""<!doctype html><meta charset="utf-8"/>
<title>JobFlow Admin</title>
<meta name=viewport content="width=device-width, initial-scale=1"/>
<link rel="preconnect" href="//fonts.googleapis.com"><link rel="preconnect" href="//fonts.gstatic.com" crossorigin>
<style>
body{margin:0;background:#0f172a;color:#e5e7eb;font:15px system-ui,Segoe UI,Roboto,Arial}
.wrap{display:grid;grid-template-columns:320px 1fr;gap:16px;height:100vh}
.col{padding:16px}
.panel{background:#111827;border-radius:12px;box-shadow:0 2px 8px #0003;padding:16px;height:calc(100vh - 32px);overflow:auto}
.item{padding:12px;border:1px solid #1f2937;border-radius:10px;margin:8px 0;cursor:pointer}
.item:hover{border-color:#374151}
.item[role=button]{outline:none}
.row{display:flex;justify-content:space-between;align-items:center}
.btn{background:#1f2937;color:#fff;border:1px solid #374151;border-radius:8px;padding:8px 12px;cursor:pointer}
.btn.good{background:#22c55e;border-color:#22c55e;color:#04190a}
.btn.bad{background:#ef4444;border-color:#ef4444}
.muted{color:#9ca3af}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
input,textarea{width:100%;background:#0b1220;color:#e5e7eb;border:1px solid #1f2937;border-radius:8px;padding:8px}
table{width:100%;border-collapse:collapse}th,td{border-top:1px solid #1f2937;padding:8px}
#flash{position:fixed;right:16px;top:16px;padding:10px 14px;border-radius:8px;display:none;background:#22c55e;color:#04190a;z-index:9999}
#flash.err{background:#ef4444;color:#fff}
</style>
<div id=flash></div>
<div class=wrap>
  <div class=col><div class=panel>
    <div class=row><h2>Sessions</h2><div>
      <button class=btn id=btnRefresh>Refresh</button>
      <button class=btn id=btnSeed>Seed Demo</button>
    </div></div>
    <div id=sessions aria-live="polite"></div>
  </div></div>
  <div class=col><div class=panel>
    <div class=row><h2 id=title>Select a session</h2></div>
    <div id=detail class=muted>Nothing selected.</div>
  </div></div>
</div>
<script>
let current=null; const $=s=>document.querySelector(s);
const el=(t,a={},c=[])=>{const n=document.createElement(t); Object.entries(a).forEach(([k,v])=>k==='class'?n.className=v:n.setAttribute(k,v)); (Array.isArray(c)?c:[c]).forEach(x=>n.append(x?.nodeType?x:document.createTextNode(x??""))); return n;}
function flash(m,ok=true){const f=$("#flash"); f.textContent=m; f.className=ok?"":"err"; f.style.display="block"; setTimeout(()=>f.style.display="none",2000)}
function fmt(n){return new Intl.NumberFormat('en-US',{style:'currency',currency:'USD'}).format(+n||0)}
function when(s){try{return new Date(s).toLocaleString()}catch{return s||'N/A'}}

async function seed(){ try{ await fetch('/api/seed-demo',{method:'POST'}); await loadSessions(); flash('Seeded')}catch(e){flash('Seed failed',false)}}
async function loadSessions(){
  $("#sessions").innerHTML="Loading...";
  try{
    const r=await fetch('/api/sessions'); if(!r.ok) throw new Error('http '+r.status);
    const j=await r.json();
    const wrap=el('div'); (j.sessions||[]).forEach(s=>{
      const card=el('div',{class:'item',tabindex:'0',role:'button','data-id':s.session_id},[
        el('div',{class:'row'},[
          el('div',{},[el('div',{},s.client_name||'Untitled'), el('div',{class:'muted'},s.job_type||'')]),
          el('div',{},fmt(s.quote_total||0))
        ]),
        el('div',{class:'muted'},'Created '+when(s.created_at))
      ]);
      wrap.append(card);
    });
    const list=$("#sessions"); list.innerHTML=""; list.append(wrap);
  }catch(e){ $("#sessions").textContent='Failed to load sessions'; flash('Load sessions failed',false) }
}

async function openSession(id){
  current=id;
  try{
    const r=await fetch('/api/sessions/'+id);
    if(!r.ok){flash("Load failed",false);return}
    const s=await r.json(); $("#title").textContent=(s.client_name||'Untitled')+' — '+fmt(s.quote_total||0);
    const d=el('div',{},[
      el('h3',{},'Overview'),
      el('div',{class:'grid2'},[
        el('div',{},[el('label',{},'Client Name'), el('input',{id:'client_name',value:s.client_name||''})]),
        el('div',{},[el('label',{},'Job Type'), el('input',{id:'job_type',value:s.job_type||''})]),
      ]),
      el('div',{},[el('button',{class:'btn good',id:'btnSaveOverview'},'Save Overview')]),
      el('h3',{},'Notes'),
      el('div',{},[el('textarea',{id:'notes',rows:'6'},s.notes||'')]),
      el('div',{},[el('button',{class:'btn good',id:'btnSaveNotes'},'Save Notes')]),
      el('h3',{},'Quote'),
      renderQuote(s.quote||[])
    ]);
    const wrap=el('div'); wrap.append(d); $("#detail").innerHTML=""; $("#detail").append(wrap);
    attachDetailHandlers();
  }catch(e){ flash('Open failed',false) }
}

function renderQuote(items){
  const t=el('table',{},[
    el('thead',{},el('tr',{},[el('th',{},'Description'),el('th',{},'Qty'),el('th',{},'Unit'),el('th',{},'Subtotal'),el('th',{},'')])),
    el('tbody',{id:'qbody'})
  ]);
  const body=t.querySelector('#qbody');
  function add(it={}){ const tr=el('tr',{},[
    el('td',{},el('input',{value:it.description||''})),
    el('td',{},el('input',{type:'number',step:'0.01',value:it.quantity??0})),
    el('td',{},el('input',{type:'number',step:'0.01',value:it.unit_price??0})),
    el('td',{},el('span',{},fmt((+it.quantity||0)*(+it.unit_price||0)))),
    el('td',{},el('button',{class:'btn bad remove'},'✖'))
  ]); body.append(tr) }
  items.forEach(add);
  const bar=el('div',{class:'row'},[
    el('button',{class:'btn addItem'},'+ Add Item'),
    el('button',{class:'btn good saveQuote'},'Save Quote')
  ]);
  return el('div',{},[t,bar]);
}

function attachDetailHandlers(){
  const detail=$("#detail");
  detail.addEventListener('click', async (e)=>{
    const t=e.target;
    if(t.classList.contains('remove')){ t.closest('tr')?.remove(); return }
    if(t.classList.contains('addItem')){
      const body=$("#qbody"); const tr=document.createElement('tr');
      tr.innerHTML=`<td><input value=""></td>
        <td><input type="number" step="0.01" value="1"></td>
        <td><input type="number" step="0.01" value="0"></td>
        <td><span>$0.00</span></td>
        <td><button class="btn bad remove">✖</button></td>`;
      body.append(tr); return;
    }
    if(t.id==='btnSaveOverview'){ await saveOverview(); return }
    if(t.id==='btnSaveNotes'){ await saveNotes(); return }
    if(t.classList.contains('saveQuote')){ await saveQuote(); return }
  }, {once:false});
}

async function saveOverview(){
  if(!current) return;
  const body={client_name:$("#client_name").value, job_type:$("#job_type").value};
  let r=await fetch('/api/sessions/'+current,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  if(!r.ok){ r=await fetch('/api/sessions/'+current,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}); }
  if(!r.ok){flash('Save failed',false);return} flash('Overview saved'); await openSession(current); await loadSessions();
}
async function saveNotes(){
  if(!current) return;
  const body={notes:$("#notes").value};
  let r=await fetch('/api/sessions/'+current,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  if(!r.ok){ r=await fetch('/api/sessions/'+current,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}); }
  if(!r.ok){flash('Notes failed',false);return} flash('Notes saved'); await openSession(current);
}
async function saveQuote(){
  if(!current) return;
  const rows=[...document.querySelectorAll('#qbody tr')];
  const items=rows.map(tr=>{ const [d,q,u]=tr.querySelectorAll('input'); return {description:d.value,quantity:parseFloat(q.value)||0,unit_price:parseFloat(u.value)||0}; });
  const r=await fetch('/api/sessions/'+current+'/quote',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({items})});
  if(!r.ok){flash('Quote failed',false);return} flash('Quote saved'); await openSession(current); await loadSessions();
}

document.addEventListener('DOMContentLoaded',()=>{
  $("#btnSeed").addEventListener('click', seed);
  $("#btnRefresh").addEventListener('click', loadSessions);
  $("#sessions").addEventListener('click',(e)=>{
    const card=e.target.closest('.item'); if(!card) return;
    const id=card.getAttribute('data-id'); if(!id) return;
    openSession(id);
  });
  $("#sessions").addEventListener('keydown',(e)=>{
    if(e.key==='Enter'){ const card=e.target.closest('.item'); if(card){ openSession(card.getAttribute('data-id')); }}
  });
  loadSessions();
});
</script>
"""

@app.get("/admin")
def admin():
    return (ADMIN, 200, {"Content-Type": "text/html; charset=utf-8"})

@app.get("/")
def root():
    return redirect("/admin", code=302)

if __name__ == "__main__":
    print("✅ JobFlow AI Cloud — starting on port", PORT)
    app.run(host="0.0.0.0", port=PORT, debug=False)
