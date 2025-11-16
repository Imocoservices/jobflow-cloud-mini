# jobflow_cloud/cloud_app.py
import json
import logging
from datetime import datetime
from pathlib import Path

from flask import (
    Flask,
    request,
    jsonify,
    render_template,
    redirect,
    url_for,
    flash,
)
from flask_login import (
    LoginManager,
    login_user,
    login_required,
    logout_user,
    current_user,
)
from werkzeug.security import generate_password_hash, check_password_hash

from config import Config
from models import db, User, Customer, Session, gen_uuid

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("jobflow-cloud")


def create_app() -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config.from_object(Config)

    # --- DB / Login setup ---
    db.init_app(app)

    login_manager = LoginManager()
    login_manager.login_view = "login"
    login_manager.init_app(app)

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(User, user_id)

    # Ensure cloud_output structure exists
    with app.app_context():
        db.create_all()
        out_dir = Path(app.config["CLOUD_OUTPUT_DIR"])
        (out_dir / "sessions").mkdir(parents=True, exist_ok=True)

    # ---------- Helpers ----------

    def get_brand_context():
        return dict(
            brand_name=app.config["BRAND_NAME"],
            primary_color=app.config["PRIMARY_COLOR"],
            accent_color=app.config["ACCENT_COLOR"],
            logo_url=app.config["LOGO_URL"],
            app_version=app.config["APP_VERSION"],
        )

    def load_json_safe(path: Path):
        try:
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning("Failed to read JSON %s: %s", path, e)
        return {}

    # ---------- Routes: Public / Health / Root ----------

    @app.route("/")
    def index():
        if current_user.is_authenticated:
            return redirect(url_for("sessions_view"))
        # keep root JSON like v1 but updated
        return jsonify(
            {
                "app": "JobFlow AI Cloud v2",
                "health": "/api/health",
                "sessions": "/sessions",
                "version": app.config["APP_VERSION"],
                "version_api": "/api/version",
            }
        )

    @app.route("/api/health")
    def api_health():
        return jsonify(
            {
                "ok": True,
                "ts": datetime.utcnow().isoformat() + "Z",
                "version": app.config["APP_VERSION"],
            }
        )

    @app.route("/api/version")
    def api_version():
        return jsonify(
            {
                "app": "JobFlow AI Cloud v2",
                "version": app.config["APP_VERSION"],
            }
        )

    # ---------- Auth: Register / Login / Logout ----------

    @app.route("/register", methods=["GET", "POST"])
    def register():
        if current_user.is_authenticated:
            return redirect(url_for("sessions_view"))

        access_code_required = app.config.get("ACCESS_CODE") or ""
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")
            access_code = request.form.get("access_code", "").strip()

            if access_code_required and access_code != access_code_required:
                flash("Invalid access code.", "error")
                return render_template("register.html", **get_brand_context())

            if not name or not email or not password:
                flash("Name, email, and password are required.", "error")
                return render_template("register.html", **get_brand_context())

            if User.query.filter_by(email=email).first():
                flash("An account with that email already exists.", "error")
                return render_template("register.html", **get_brand_context())

            user = User(
                name=name,
                email=email,
                password_hash=generate_password_hash(password),
            )
            db.session.add(user)
            db.session.commit()

            flash("Account created. Please log in.", "success")
            return redirect(url_for("login"))

        return render_template("register.html", **get_brand_context())

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if current_user.is_authenticated:
            return redirect(url_for("sessions_view"))

        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")

            user = User.query.filter_by(email=email).first()
            if not user or not check_password_hash(user.password_hash, password):
                flash("Invalid email or password.", "error")
                return render_template("login.html", **get_brand_context())

            login_user(user)
            return redirect(url_for("sessions_view"))

        return render_template("login.html", **get_brand_context())

    @app.route("/logout")
    @login_required
    def logout():
        logout_user()
        flash("Logged out.", "success")
        return redirect(url_for("login"))

    # ---------- Settings: API Token ----------

    @app.route("/settings/api-token", methods=["GET", "POST"])
    @login_required
    def settings_api_token():
        user = current_user
        if request.method == "POST":
            # regenerate
            from models import gen_api_token

            user.api_token = gen_api_token()
            db.session.commit()
            flash("API token regenerated.", "success")

        return render_template(
            "settings_api_token.html",
            user=user,
            **get_brand_context(),
        )

    # ---------- Sessions UI ----------

    @app.route("/sessions")
    @login_required
    def sessions_view():
        sessions = (
            Session.query.filter_by(user_id=current_user.id)
            .order_by(Session.updated_at.desc())
            .all()
        )

        output_dir = Path(app.config["CLOUD_OUTPUT_DIR"])

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

        return render_template(
            "sessions.html",
            sessions=view_models,
            **get_brand_context(),
        )

    @app.route("/sessions/<session_id>")
    @login_required
    def session_detail(session_id):
        s = Session.query.filter_by(id=session_id, user_id=current_user.id).first_or_404()
        meta = load_json_safe(Path(s.meta_json_path)) if s.meta_json_path else {}
        report = load_json_safe(Path(s.report_json_path)) if s.report_json_path else {}

        return render_template(
            "session_detail.html",
            session_obj=s,
            meta=meta,
            report=report,
            **get_brand_context(),
        )

    @app.route("/sessions/<session_id>/proposal")
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
            "proposal.html",
            session_obj=s,
            customer=customer,
            summary=summary,
            quote_items=quote_items,
            total=total,
            **get_brand_context(),
        )

    # ---------- API: Import Session (Local -> Cloud) ----------

    @app.route("/api/import_session", methods=["POST"])
    def api_import_session():
        """
        Secure, multi-tenant import used by local master.py.

        Headers:
          X-Api-Token: user's API token (required)
          X-Import-Token: optional global shared secret (if configured)

        Body JSON:
          {
            "session_id": "string-id",
            "meta": {...},
            "report": {...}
          }
        """
        api_token = request.headers.get("X-Api-Token", "").strip()
        if not api_token:
            return jsonify({"error": "Missing X-Api-Token"}), 401

        user = User.query.filter_by(api_token=api_token).first()
        if not user:
            return jsonify({"error": "Invalid API token"}), 403

        expected_import = app.config.get("IMPORT_TOKEN") or ""
        if expected_import:
            incoming_import = request.headers.get("X-Import-Token", "").strip()
            if incoming_import != expected_import:
                return jsonify({"error": "Invalid import token"}), 403

        data = request.get_json(silent=True) or {}
        session_id = str(data.get("session_id") or gen_uuid())
        meta = data.get("meta") or {}
        report = data.get("report") or {}

        try:
            base_dir = Path(app.config["CLOUD_OUTPUT_DIR"]) / "sessions" / session_id
            base_dir.mkdir(parents=True, exist_ok=True)

            meta_path = base_dir / "meta.json"
            report_path = base_dir / "report.json"

            meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
            report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

            # Title
            title = (
                meta.get("title")
                or meta.get("label")
                or meta.get("job_name")
                or f"Session {session_id[:8]}"
            )

            # Find or create session row
            s = Session.query.filter_by(id=session_id, user_id=user.id).first()
            if not s:
                s = Session(id=session_id, user_id=user.id, created_at=datetime.utcnow())

            s.title = title
            s.cloud_path = str(base_dir)
            s.meta_json_path = str(meta_path)
            s.report_json_path = str(report_path)
            s.updated_at = datetime.utcnow()

            # Customer handling
            customer_info = meta.get("customer") or {}
            if customer_info:
                cust = None
                phone = (customer_info.get("phone") or "").strip()
                email = (customer_info.get("email") or "").strip().lower()

                if phone:
                    cust = Customer.query.filter_by(user_id=user.id, phone=phone).first()
                if not cust and email:
                    cust = Customer.query.filter_by(user_id=user.id, email=email).first()

                if not cust:
                    cust = Customer(
                        user_id=user.id,
                        name=customer_info.get("name") or "Customer",
                        phone=phone or None,
                        email=email or None,
                        address_line1=customer_info.get("address"),
                    )
                    db.session.add(cust)

                s.customer = cust

            db.session.add(s)
            db.session.commit()

            log.info(
                "[cloud-sync] Imported session %s for user %s", session_id, user.email
            )
            return jsonify({"status": "imported", "session_id": session_id})

        except Exception as e:
            log.exception("Failed to import session: %s", e)
            db.session.rollback()
            return jsonify({"error": "Internal error importing session"}), 500

    return app


app = create_app()

if __name__ == "__main__":
    app.run(debug=True, port=8000)
