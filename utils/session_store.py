from __future__ import annotations
import json
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Any

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def ensure_session_folder(root: Path, session_id: str) -> Path:
    p = root / session_id
    p.mkdir(parents=True, exist_ok=True)
    rp = p / "report.json"
    if not rp.exists():
        base = {
            "id": session_id,
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "client_name": "",
            "job_type": "",
            "status": "new",
            "accepted_at": None,
            "summary": "",
            "tasks": [],
            "materials": [],
            "entities": {},
            "transcript": "",
            "media": {"photos": [], "audio": []},
            "quote": [],
            "quote_total": 0.0,
            "estimate": {"suggested_price": None, "quote_suggested": []},
        }
        rp.write_text(json.dumps(base, indent=2), encoding="utf-8")
    return p

def load_report(p: Path) -> Dict[str, Any]:
    rp = p / "report.json"
    if not rp.exists():
        return {}
    try:
        return json.loads(rp.read_text(encoding="utf-8"))
    except Exception:
        return {}

def save_report(p: Path, data: Dict[str, Any]):
    data["updated_at"] = now_iso()
    (p / "report.json").write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
