import os
from datetime import datetime

from flask import (
    Flask,
    render_template,
    redirect,
    url_for,
    request,
    flash,
)
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager,
    UserMixin,
    login_user,
    logout_user,
    current_user,
    login_required,
)
from werkzeug.security import generate_password_hash, check_password_hash

# ============================================
# Globals
# ============================================

db = SQLAlchemy()
login_manager = LoginManager()


# ============================================
# Models
# ============================================

class User(db.Model, UserMixin):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, raw_password: str) -> None:
        self.password_hash = generate_password_hash(raw_password)

    def check_password(self, raw_password: str) -> bool:
        return check_password_hash(self.password_hash, raw_password)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<User {self.email}>"


# ============================================
# App Factory
# ============================================

def create_app() -> Flask:
    # templates/static are in the project root, one level above jobflow_cloud
    app = Flask(
        __name__,
        template_folder="../templates",
        static_folder="../static",
    )

    # ---- Basic config ----
    app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret")

    # Render/Postgres usually exposes DATABASE_URL env
    database_url = os.getenv("DATABASE_URL", "sqlite:///local.db")

    # Some platforms use old "postgres://" prefix; SQLAlchemy prefers "postgresql://"
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)

    app.config["SQLALCHEMY_DATABASE_URI"] = database_url
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    # ---- Init extensions ----
    db.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "login"

    # Ensure tables exist (users table in Postgres)
    with app.app_context():
        db.create_all()

    # ========================================
    # Login manager loader
    # ========================================

    @login_manager.user_loader
    def load_user(user_id: str):
        try:
            return User.query.get(int(user_id))
        except Exception:
            return None

    # ========================================
    # Routes
    # ========================================

    @app.route("/")
    def index():
        # If logged in, send straight to dashboard
        if current_user.is_authenticated:
            return redirect(url_for("dashboard"))
        return redirect(url_for("login"))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")

            if not email or not password:
                flash("Email and password are required.", "danger")
                return render_template("login.html")

            user = User.query.filter_by(email=email).first()
            if user and user.check_password(password):
                login_user(user)
                flash("Logged in successfully.", "success")
                next_url = request.args.get("next")
                return redirect(next_url or url_for("dashboard"))
            else:
                flash("Invalid email or password.", "danger")

        return render_template("login.html")

    @app.route("/register", methods=["GET", "POST"])
    def register():
        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")

            # Basic validation
            if not email or not password:
                flash("Email and password are required.", "danger")
                return render_template("register.html")

            existing = User.query.filter_by(email=email).first()
            if existing:
                flash("That email is already registered. Please log in.", "warning")
                return redirect(url_for("login"))

            # Create new user
            user = User(email=email)
            user.set_password(password)
            db.session.add(user)
            db.session.commit()

            flash("Registration successful. Please log in.", "success")
            return redirect(url_for("login"))

        return render_template("register.html")

    @app.route("/logout")
    @login_required
    def logout():
        logout_user()
        flash("You have been logged out.", "info")
        return redirect(url_for("login"))

    @app.route("/dashboard")
    @login_required
    def dashboard():
        # Placeholder for now — this is where we’ll show sessions, quotes, etc.
        return render_template("dashboard.html", user=current_user)

    # Simple health check for Render
    @app.route("/health")
    def health():
        return {"status": "ok"}

    return app


# Gunicorn / Render entrypoint expects `app`
app = create_app()
