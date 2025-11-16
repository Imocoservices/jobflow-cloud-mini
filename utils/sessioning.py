import json, os
from datetime import datetime, timezone
from pathlib import Path

def ensure_dirs(sessions_dir: Path):
    sessions_dir.mkdir(parents=True, exist_ok=True)

def _current_session_id():
    # Group into 10-minute buckets: e.g., 2025-11-02-13-20
    now = datetime.now(timezone.utc)
    bucket_min = (now.minute // 10) * 10
    stamp = now.replace(minute=bucket_min, second=0, microsecond=0)
    return stamp.strftime("%Y%m%dT%H%M")

def get_or_create_session(sessions_dir: Path):
    sid = _current_session_id()
    folder = sessions_dir / sid
    folder.mkdir(parents=True, exist_ok=True)
    report_path = folder / "report.json"
    if report_path.exists():
        rpt = json.loads(report_path.read_text(encoding="utf-8"))
    else:
        rpt = {
            "session_id": sid,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "quote": [],
            "quote_total": 0,
            "status": "draft"
        }
        report_path.write_text(json.dumps(rpt, indent=2), encoding="utf-8")
    return sid, folder, rpt

def read_report(folder: Path):
    p = folder / "report.json"
    if not p.exists(): return None
    return json.loads(p.read_text(encoding="utf-8"))

def write_report(folder: Path, rpt: dict):
    (folder / "report.json").write_text(json.dumps(rpt, indent=2), encoding="utf-8")

def list_sessions(sessions_dir: Path):
    ensure_dirs(sessions_dir)
    items = []
    for child in sorted(sessions_dir.iterdir(), reverse=True):
        if child.is_dir() and (child / "report.json").exists():
            try:
                items.append(read_report(child))
            except Exception:
                pass
    return items
