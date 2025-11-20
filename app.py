import os
import secrets
from datetime import datetime
from decimal import Decimal, InvalidOperation

from flask import (
    Flask,
    request,
    jsonify,
    render_template,
    redirect,
    url_for,
    flash,
)
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager,
    UserMixin,
    login_user,
    logout_user,
    login_required,
    current_user,
)
from werkzeug.security import generate_password_hash, check_password_hash

# =====================
# App + Config
# =====================

app = Flask(__name__)
CORS(app)

# Secret key for sessions
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret-change-me")

# Database URL (Render usually provides DATABASE_URL)
database_url = os.getenv("DATABASE_URL", "sqlite:///jobflow_cloud_mini.db")

# Render sometimes uses postgres:// – SQLAlchemy prefers postgresql://
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = database_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"


# =====================
# Models
# =====================

class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    api_key = db.Column(db.String(64), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    sessions = db.relationship("Session", backref="user", lazy=True)

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    def get_id(self):
        # Flask-Login requirement
        return str(self.id)


class Session(db.Model):
    __tablename__ = "sessions"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)

    # From local JobFlow bot
    external_id = db.Column(db.String(255), nullable=False)

    client_name = db.Column(db.String(255))
    summary = db.Column(db.Text)
    transcript = db.Column(db.Text)
    analysis_json = db.Column(db.JSON)  # stores dict from "analysis"
    status = db.Column(db.String(50))

    predicted_price = db.Column(db.Numeric(12, 2))

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    uploads = db.relationship(
        "Upload", backref="session", lazy=True, cascade="all, delete-orphan"
    )
    quote = db.relationship(
        "Quote", backref="session", uselist=False, cascade="all, delete-orphan"
    )

    __table_args__ = (
        db.UniqueConstraint("user_id", "external_id", name="uq_user_session_external"),
    )


class Upload(db.Model):
    __tablename__ = "uploads"

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey("sessions.id"), nullable=False)

    kind = db.Column(db.String(20))  # image, audio, video, other
    file_url = db.Column(db.String(1024))
    filename = db.Column(db.String(255))
    mime_type = db.Column(db.String(100))
    size_bytes = db.Column(db.Integer)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class Quote(db.Model):
    __tablename__ = "quotes"

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey("sessions.id"), nullable=False)

    total_amount = db.Column(db.Numeric(12, 2))
    currency = db.Column(db.String(10), default="USD")
    notes = db.Column(db.Text)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    line_items = db.relationship(
        "QuoteLineItem", backref="quote", lazy=True, cascade="all, delete-orphan"
    )


class QuoteLineItem(db.Model):
    __tablename__ = "quote_line_items"

    id = db.Column(db.Integer, primary_key=True)
    quote_id = db.Column(db.Integer, db.ForeignKey("quotes.id"), nullable=False)

    description = db.Column(db.Text)
    quantity = db.Column(db.Numeric(12, 2))
    unit_price = db.Column(db.Numeric(12, 2))
    line_total = db.Column(db.Numeric(12, 2))


# =====================
# Login Manager
# =====================

@login_manager.user_loader
def load_user(user_id):
    try:
        return User.query.get(int(user_id))
    except (TypeError, ValueError):
        return None


# =====================
# Helpers
# =====================

def generate_api_key() -> str:
    return secrets.token_hex(32)


def parse_decimal(value):
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


# =====================
# Health
# =====================

@app.route("/health")
def health():
    return jsonify({"status": "ok"})


# =====================
# Auth Routes (HTML)
# =====================

@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm") or ""

        if not email or not password:
            flash("Email and password are required.", "danger")
            return render_template("register.html")

        if password != confirm:
            flash("Passwords do not match.", "danger")
            return render_template("register.html")

        existing = User.query.filter_by(email=email).first()
        if existing:
            flash("Email already registered. Please log in.", "warning")
            return redirect(url_for("login"))

        user = User(email=email, api_key=generate_api_key())
        user.set_password(password)
        db.session.add(user)
        db.session.commit()

        flash("Registration successful. You can now log in.", "success")
        return redirect(url_for("login"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""

        user = User.query.filter_by(email=email).first()
        if user and user.check_password(password):
            login_user(user)
            flash("Logged in successfully.", "success")
            next_url = request.args.get("next") or url_for("dashboard")
            return redirect(next_url)

        flash("Invalid email or password.", "danger")

    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("You have been logged out.", "info")
    return redirect(url_for("login"))


# =====================
# Dashboard & Session Views
# =====================

@app.route("/")
def index():
    # Redirect root to dashboard or login
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
    sessions = (
        Session.query.filter_by(user_id=current_user.id)
        .order_by(Session.created_at.desc())
        .all()
    )
    return render_template("dashboard.html", sessions=sessions)


@app.route("/session/<int:session_id>")
@login_required
def session_detail(session_id):
    session_obj = Session.query.get_or_404(session_id)
    if session_obj.user_id != current_user.id:
        # Basic protection – you could return 404 to avoid leaking IDs
        return "Not found", 404

    return render_template("session_detail.html", session=session_obj)


@app.route("/settings")
@login_required
def settings():
    return render_template("settings.html", user=current_user)


# =====================
# API: Sync Session
# =====================

@app.route("/api/sync-session", methods=["POST"])
def sync_session():
    """
    Cloud endpoint called by local JobFlow bot.

    Auth:
      - Prefer X-API-Key header
      - Fallback: body["api_key"]

    Payload shape (roughly):

    {
      "api_key": "...",          # optional if using header
      "session": {...},          # required
      "uploads": [...],          # optional
      "quote": {...}             # optional
    }
    """
    data = request.get_json(force=True, silent=True) or {}

    # ---- Auth via API key ----
    api_key = request.headers.get("X-API-Key") or data.get("api_key")
    if not api_key:
        return jsonify({"ok": False, "error": "Missing API key"}), 401

    user = User.query.filter_by(api_key=api_key).first()
    if not user:
        return jsonify({"ok": False, "error": "Invalid API key"}), 401

    # ---- Session payload ----
    session_payload = data.get("session") or {}
    external_id = session_payload.get("external_id")

    if not external_id:
        return jsonify(
            {"ok": False, "error": "Missing 'session.external_id' in payload"}
        ), 400

    # Find existing session or create new
    session_obj = Session.query.filter_by(
        user_id=user.id, external_id=external_id
    ).first()

    creating_new = False
    if not session_obj:
        session_obj = Session(user_id=user.id, external_id=external_id)
        creating_new = True
        db.session.add(session_obj)

    # Update core fields
    session_obj.client_name = session_payload.get("client_name")
    session_obj.summary = session_payload.get("summary")
    session_obj.transcript = session_payload.get("transcript")
    session_obj.status = session_payload.get("status")

    predicted_price = parse_decimal(session_payload.get("predicted_price"))
    session_obj.predicted_price = predicted_price

    analysis = session_payload.get("analysis")
    if analysis is not None:
        session_obj.analysis_json = analysis

    # ---- Uploads ----
    uploads_payload = data.get("uploads") or []

    # Remove existing uploads for this session
    if not creating_new:
        Upload.query.filter_by(session_id=session_obj.id).delete()

    for u in uploads_payload:
        upload = Upload(
            session=session_obj,
            kind=u.get("kind"),
            file_url=u.get("file_url"),
            filename=u.get("filename"),
            mime_type=u.get("mime_type"),
            size_bytes=u.get("size_bytes"),
        )
        db.session.add(upload)

    # ---- Quote ----
    quote_payload = data.get("quote")
    if quote_payload:
        quote = Quote.query.filter_by(session_id=session_obj.id).first()
        if not quote:
            quote = Quote(session=session_obj)
            db.session.add(quote)

        quote.currency = quote_payload.get("currency") or "USD"
        quote.notes = quote_payload.get("notes")

        total_amount = parse_decimal(quote_payload.get("total_amount"))
        quote.total_amount = total_amount

        # Replace line items
        QuoteLineItem.query.filter_by(quote_id=quote.id).delete()

        line_items = quote_payload.get("line_items") or []
        computed_total = Decimal("0")
        for item in line_items:
            qty = parse_decimal(item.get("quantity") or 1)
            unit_price = parse_decimal(item.get("unit_price") or 0)
            if qty is None:
                qty = Decimal("1")
            if unit_price is None:
                unit_price = Decimal("0")

            line_total = qty * unit_price
            computed_total += line_total

            db.session.add(
                QuoteLineItem(
                    quote=quote,
                    description=item.get("description"),
                    quantity=qty,
                    unit_price=unit_price,
                    line_total=line_total,
                )
            )

        # If total_amount missing, use computed sum
        if quote.total_amount is None:
            quote.total_amount = computed_total

    db.session.commit()

    return jsonify(
        {
            "ok": True,
            "session_id": session_obj.id,
            "external_id": session_obj.external_id,
            "message": "Session synced",
        }
    )


# =====================
# App init
# =====================

with app.app_context():
    db.create_all()


# =====================
# Main (for local debug)
# =====================

if __name__ == "__main__":
    # Local debug only. Render uses gunicorn via wsgi:app.
    port = int(os.getenv("PORT", "5065"))
    app.run(host="0.0.0.0", port=port, debug=True)
