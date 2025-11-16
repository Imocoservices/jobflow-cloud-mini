# utils/exporters.py
from pathlib import Path
from datetime import datetime, timedelta

def export_html_quote(folder: Path, rpt: dict) -> Path:
    html = []
    html.append("<!doctype html><html><head><meta charset='utf-8'>")
    html.append("<style>body{font-family:Arial;margin:24px} table{width:100%;border-collapse:collapse} th,td{border:1px solid #ddd;padding:8px} th{background:#f3f3f3;text-align:left}</style>")
    html.append("</head><body>")
    html.append(f"<h2>Quote — {rpt.get('job_title','Untitled')}</h2>")
    html.append(f"<p><b>Client:</b> {rpt.get('client_name','')}</p>")
    html.append(f"<p><b>Created:</b> {rpt.get('created_at','')}</p>")
    html.append(f"<p><b>Notes:</b> {rpt.get('notes','')}</p>")
    html.append("<table><thead><tr><th>Description</th><th>Qty</th><th>Unit</th><th>Line Total</th></tr></thead><tbody>")
    total = 0.0
    for it in rpt.get("quote",[]):
        q = float(it.get("quantity",1))
        p = float(it.get("unit_price",0))
        d = str(it.get("description",""))
        lt = q*p
        total += lt
        html.append(f"<tr><td>{d}</td><td>{q}</td><td>${p:.2f}</td><td>${lt:.2f}</td></tr>")
    html.append(f"</tbody></table><h3>Total: ${total:.2f}</h3>")
    html.append("</body></html>")
    out = folder / f"quote_{rpt.get('session_id','session')}.html"
    out.write_text("".join(html), encoding="utf-8")
    return out

def make_ics(folder: Path, rpt: dict) -> Path:
    start_dt = datetime.now()
    end_dt = start_dt + timedelta(hours=1)  # 1-hour appointment
    start = start_dt.strftime("%Y%m%dT%H%M%S")
    end = end_dt.strftime("%Y%m%dT%H%M%S")
    summary = rpt.get("job_title","Job Appointment")
    ics = f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//JobFlow AI//EN
BEGIN:VEVENT
UID:{rpt.get('session_id','session')}@jobflow.local
DTSTAMP:{start}Z
DTSTART:{start}Z
DTEND:{end}Z
SUMMARY:{summary}
END:VEVENT
END:VCALENDAR
"""
    path = folder / f"session_{rpt.get('session_id','session')}.ics"
    path.write_text(ics, encoding="utf-8")
    return path
