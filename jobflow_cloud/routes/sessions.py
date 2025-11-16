# jobflow_cloud/routes/sessions.py
import json
from pathlib import Path
from datetime import datetime

from flask import Blueprint, render_template, current_app
from flask_login import login_required, current_user

from ..models import Session

sessions_bp = Blueprint("sessions", __name__)


def load_json_safe(path: Path):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {}


@sessions_bp.route("/sessions")
@login_required
def sessions_view():
    sessions = (
        Session.query.filter_by(user_id=current_user.id)
        .order_by(Session.updated_at.desc())
        .all()
    )

    view_models = []
    for s in sessions:
        meta = load_json_safe(Path(s.meta_json_path)) if s.meta_json_path else {}
        report = load_json_safe(Path(s.report_json_path)) if s.report_json_path else {}
        customer = meta.get("customer") or {}
        total = report.get("quote_total") or report.get("total") or ""

        view_models.append(
            {
                "id": s.id,
                "title": s.title or meta.get("title") or f"Session {s.id[:8]}",
                "customer_name": customer.get("name", ""),
                "total": total,
                "updated_at": s.updated_at,
            }
        )

    return render_template("sessions/list.html", sessions=view_models)


@sessions_bp.route("/sessions/<session_id>")
@login_required
def session_detail(session_id):
    s = Session.query.filter_by(id=session_id, user_id=current_user.id).first_or_404()
    meta = load_json_safe(Path(s.meta_json_path)) if s.meta_json_path else {}
    report = load_json_safe(Path(s.report_json_path)) if s.report_json_path else {}

    return render_template(
        "sessions/detail.html",
        session_obj=s,
        meta=meta,
        report=report,
    )


@sessions_bp.route("/sessions/<session_id>/proposal")
@login_required
def session_proposal(session_id):
    s = Session.query.filter_by(id=session_id, user_id=current_user.id).first_or_404()
    meta = load_json_safe(Path(s.meta_json_path)) if s.meta_json_path else {}
    report = load_json_safe(Path(s.report_json_path)) if s.report_json_path else {}

    customer = meta.get("customer") or {}
    summary = report.get("summary", "")
    quote_items = report.get("quote_items") or report.get("quote", [])
    total = report.get("quote_total") or ""

    return render_template(
        "sessions/proposal.html",
        session_obj=s,
        customer=customer,
        summary=summary,
        quote_items=quote_items,
        total=total,
    )


@sessions_bp.route("/settings/api-token", methods=["GET", "POST"])
@login_required
def settings_api_token():
    from ..models import db, gen_api_token
    from flask import request, flash, redirect, url_for

    user = current_user
    if request.method == "POST":
        user.api_token = gen_api_token()
        user.updated_at = datetime.utcnow()
        db.session.commit()
        flash("API token regenerated.", "success")
        return redirect(url_for("sessions.settings_api_token"))

    return render_template("settings/api_token.html", user=user)
