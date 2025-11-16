from pathlib import Path
from datetime import datetime, timedelta

ICS_TMPL = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//JobFlowAI//EN
BEGIN:VEVENT
UID:{uid}
DTSTAMP:{dt}
DTSTART:{start}
DTEND:{end}
SUMMARY:{title}
DESCRIPTION:{notes}
END:VEVENT
END:VCALENDAR
"""

def write_ics(sess_dir: Path, title: str, when: str|None, notes: str="") -> Path:
    # when: "YYYY-MM-DD HH:MM" local or None -> now+1d 09:00
    try:
        if when:
            dt = datetime.strptime(when, "%Y-%m-%d %H:%M")
        else:
            now = datetime.now()
            dt = (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    except Exception:
        now = datetime.now()
        dt = (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)

    dtend = dt + timedelta(hours=1)
    def fmt(x): return x.strftime("%Y%m%dT%H%M%S")
    uid = f"jobflow-{fmt(datetime.now())}@local"

    ics = ICS_TMPL.format(
        uid=uid, dt=fmt(datetime.utcnow()),
        start=fmt(dt), end=fmt(dtend),
        title=title.replace("\n"," "),
        notes=(notes or "").replace("\n"," "),
    )
    out = sess_dir / "event.ics"
    out.write_text(ics, encoding="utf-8")
    return out
