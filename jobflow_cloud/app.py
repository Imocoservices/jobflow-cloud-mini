# jobflow_cloud/app.py

import os

from flask import (
    Flask,
    render_template,
    redirect,
    url_for,
    request,
    flash,
    jsonify,
)
from flask_login import (
    LoginManager,
    login_user,
    login_required,
    logout_user,
    current_user,
)

from .models import db, User


def _get_database_uri() -> str:
    """
    Use Render's DATABASE_URL if present (Postgres),
    otherwise fall back to a local SQLite DB for dev.
    """
    uri = os.environ.get("DATABASE_URL")

    if uri:
        # Render often gives postgres://, SQLAlchemy wants postgresql+psycopg2://
        if uri.startswith("postgres://"):
            uri = uri.replace("postgres://", "postgresql+psycopg2://", 1)
        return uri

    # Local development fallback
    return "sqlite:///jobflow_local.db"


def create_app() -> Flask:
    app = Flask(__name__, static_folder="static", template_folder="templates")

    # === Core config ===
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-jobflow-secret")
    app.config["SQLALCHEMY_DATABASE_URI"] = _get_database_uri()
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    # === Init extensions ===
    db.init_app(app)

    login_manager = LoginManager()
    login_manager.login_view = "login"  # where to send unauthenticated users
    login_manager.init_app(app)

    @login_manager.user_loader
    def load_user(user_id: str):
        try:
            return User.query.get(int(user_id))
        except Exception:
            return None

    # === Simple health endpoint for monitoring ===
    @app.route("/health")
    def health():
        return jsonify({"status": "ok", "service": "jobflow-cloud-mini"}), 200

    # === Auth + pages ===

    @app.route("/")
    def index():
        if current_user.is_authenticated:
            return redirect(url_for("dashboard"))
        return redirect(url_for("login"))

    @app.route("/register", methods=["GET", "POST"])
    def register():
        if current_user.is_authenticated:
            return redirect(url_for("dashboard"))

        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")

            if not email or not password:
                flash("Please enter both email and password.", "error")
                return render_template("register.html")

            existing = User.query.filter_by(email=email).first()
            if existing:
                flash("An account with that email already exists. Try logging in.", "error")
                return redirect(url_for("login"))

            user = User(email=email)
            user.set_password(password)
            db.session.add(user)
            db.session.commit()

            flash("Account created. You can now log in.", "success")
            return redirect(url_for("login"))

        return render_template("register.html")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if current_user.is_authenticated:
            return redirect(url_for("dashboard"))

        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")

            user = User.query.filter_by(email=email).first()
            if not user or not user.check_password(password):
                flash("Invalid email or password.", "error")
                return render_template("login.html")

            login_user(user)
            flash("Logged in successfully.", "success")
            return redirect(url_for("dashboard"))

        return render_template("login.html")

    @app.route("/logout")
    @login_required
    def logout():
        logout_user()
        flash("You have been logged out.", "success")
        return redirect(url_for("login"))

    @app.route("/dashboard")
    @login_required
    def dashboard():
        # Placeholder for now — later we’ll show sessions/quotes here
        return render_template("dashboard.html", user=current_user)

    # === DB bootstrap ===
    # This will auto-create tables on first run (both local + Render)
    with app.app_context():
        db.create_all()

    return app
