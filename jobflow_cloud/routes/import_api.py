# jobflow_cloud/routes/import_api.py
import json
from datetime import datetime
from pathlib import Path

from flask import Blueprint, request, jsonify, current_app
from ..models import db, User, Session, Customer, gen_uuid

api_bp = Blueprint("api", __name__, url_prefix="/api")


def _load_or_create_customer(user, customer_info):
    if not customer_info:
        return None

    phone = (customer_info.get("phone") or "").strip()
    email = (customer_info.get("email") or "").strip().lower()

    q = None
    if phone:
        q = Customer.query.filter_by(user_id=user.id, phone=phone).first()
    if not q and email:
        q = Customer.query.filter_by(user_id=user.id, email=email).first()

    if q:
        return q

    c = Customer(
        user_id=user.id,
        name=customer_info.get("name") or "Customer",
        phone=phone or None,
        email=email or None,
        address_line1=customer_info.get("address"),
    )
    db.session.add(c)
    return c


@api_bp.route("/health")
def api_health():
    return jsonify(
        {
            "ok": True,
            "version": current_app.config["APP_VERSION"],
        }
    )


@api_bp.route("/version")
def api_version():
    return jsonify(
        {
            "app": "JobFlow AI Cloud v2",
            "version": current_app.config["APP_VERSION"],
        }
    )


@api_bp.route("/import_session", methods=["POST"])
def import_session():
    api_token = request.headers.get("X-Api-Token", "").strip()
    if not api_token:
        return jsonify({"error": "Missing X-Api-Token"}), 401

    user = User.query.filter_by(api_token=api_token).first()
    if not user:
        return jsonify({"error": "Invalid API token"}), 403

    expected_import = current_app.config.get("IMPORT_TOKEN") or ""
    if expected_import:
        incoming_import = request.headers.get("X-Import-Token", "").strip()
        if incoming_import != expected_import:
            return jsonify({"error": "Invalid import token"}), 403

    data = request.get_json(silent=True) or {}
    session_id = str(data.get("session_id") or gen_uuid())
    meta = data.get("meta") or {}
    report = data.get("report") or {}

    try:
        base_dir = Path(current_app.config["CLOUD_OUTPUT_DIR"]) / "sessions" / session_id
        base_dir.mkdir(parents=True, exist_ok=True)

        meta_path = base_dir / "meta.json"
        report_path = base_dir / "report.json"

        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

        title = (
            meta.get("title")
            or meta.get("label")
            or meta.get("job_name")
            or f"Session {session_id[:8]}"
        )

        s = Session.query.filter_by(id=session_id, user_id=user.id).first()
        if not s:
            s = Session(id=session_id, user_id=user.id, created_at=datetime.utcnow())

        s.title = title
        s.cloud_path = str(base_dir)
        s.meta_json_path = str(meta_path)
        s.report_json_path = str(report_path)
        s.updated_at = datetime.utcnow()

        customer_info = meta.get("customer") or {}
        if customer_info:
            cust = _load_or_create_customer(user, customer_info)
            s.customer = cust

        db.session.add(s)
        db.session.commit()

        current_app.logger.info(
            "[cloud-sync] Imported session %s for user %s", session_id, user.email
        )
        return jsonify({"status": "imported", "session_id": session_id})

    except Exception as e:
        current_app.logger.exception("Failed to import session: %s", e)
        db.session.rollback()
        return jsonify({"error": "Internal error importing session"}), 500
