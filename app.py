import os
from datetime import datetime

from flask import (
    Flask,
    request,
    jsonify,
    redirect,
    url_for,
    flash,
)
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
from flask import render_template_string

# ====================================
# App + Config
# ====================================

app = Flask(__name__)

app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret-key")

db_url = os.getenv("DATABASE_URL", "sqlite:///local.db")
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"


# ====================================
# Models
# ====================================

class User(db.Model, UserMixin):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    sessions = db.relationship("Session", back_populates="user", lazy="dynamic")

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)


class Session(db.Model):
    """
    A JobFlow session synced from your local bot.

    external_id = the ID your local relationship_bot uses
    (for example 'session_2025-11-18_001' or a UUID).
    """
    __tablename__ = "sessions"

    id = db.Column(db.Integer, primary_key=True)
    external_id = db.Column(db.String(128), index=True, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)

    client_name = db.Column(db.String(255))
    title = db.Column(db.String(255))
    summary = db.Column(db.Text)
    transcript = db.Column(db.Text)
    analysis = db.Column(db.JSON)

    status = db.Column(db.String(50), default="new")  # new / reviewed / quoted
    predicted_price_cents = db.Column(db.Integer)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    user = db.relationship("User", back_populates="sessions")
    uploads = db.relationship(
        "Upload",
        back_populates="session",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )
    quote = db.relationship(
        "Quote",
        back_populates="session",
        uselist=False,
        cascade="all, delete-orphan",
    )


class Upload(db.Model):
    """
    Uploads associated with a session (images, audio, video, other).
    file_url should be a URL the cloud app can use (https://... or /static/...).
    """
    __tablename__ = "uploads"

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey("sessions.id"), nullable=False)

    kind = db.Column(db.String(50), nullable=False)  # image / audio / video / other
    file_url = db.Column(db.String(1024), nullable=False)
    filename = db.Column(db.String(255))
    mime_type = db.Column(db.String(255))
    size_bytes = db.Column(db.Integer)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    session = db.relationship("Session", back_populates="uploads")


class Quote(db.Model):
    __tablename__ = "quotes"

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey("sessions.id"), nullable=False)

    title = db.Column(db.String(255))
    notes = db.Column(db.Text)
    currency = db.Column(db.String(10), default="USD")
    total_cents = db.Column(db.Integer, default=0)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    session = db.relationship("Session", back_populates="quote")
    items = db.relationship(
        "QuoteLineItem",
        back_populates="quote",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )


class QuoteLineItem(db.Model):
    __tablename__ = "quote_line_items"

    id = db.Column(db.Integer, primary_key=True)
    quote_id = db.Column(db.Integer, db.ForeignKey("quotes.id"), nullable=False)

    description = db.Column(db.String(512), nullable=False)
    quantity = db.Column(db.Float, default=1.0)
    unit = db.Column(db.String(50), default="unit")
    unit_price_cents = db.Column(db.Integer, default=0)
    line_total_cents = db.Column(db.Integer, default=0)
    sort_order = db.Column(db.Integer, default=0)

    quote = db.relationship("Quote", back_populates="items")


# ====================================
# Login Manager
# ====================================

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# ====================================
# Helpers
# ====================================

def to_cents(amount):
    """Convert dollars to integer cents."""
    if amount is None:
        return None
    try:
        return int(round(float(amount) * 100))
    except (ValueError, TypeError):
        return None


def from_cents(cents):
    if cents is None:
        return None
    return cents / 100.0


# ====================================
# Inline Templates
# ====================================

LOGIN_TEMPLATE = """
<!doctype html>
<title>JobFlow Cloud Mini – Login</title>
<h1>Login</h1>
{% with messages = get_flashed_messages(with_categories=true) %}
  {% if messages %}
    <ul>
    {% for category, msg in messages %}
      <li><strong>{{ category }}:</strong> {{ msg }}</li>
    {% endfor %}
    </ul>
  {% endif %}
{% endwith %}
<form method="post">
  <label>Email:<br><input type="email" name="email" required></label><br>
  <label>Password:<br><input type="password" name="password" required></label><br>
  <button type="submit">Login</button>
</form>
<p>No account? <a href="{{ url_for('register') }}">Register here</a>.</p>
"""

REGISTER_TEMPLATE = """
<!doctype html>
<title>JobFlow Cloud Mini – Register</title>
<h1>Register</h1>
{% with messages = get_flashed_messages(with_categories=true) %}
  {% if messages %}
    <ul>
    {% for category, msg in messages %}
      <li><strong>{{ category }}:</strong> {{ msg }}</li>
    {% endfor %}
    </ul>
  {% endif %}
{% endwith %}
<form method="post">
  <label>Email:<br><input type="email" name="email" required></label><br>
  <label>Password:<br><input type="password" name="password" required></label><br>
  <label>Confirm Password:<br><input type="password" name="confirm" required></label><br>
  <button type="submit">Register</button>
</form>
<p>Already registered? <a href="{{ url_for('login') }}">Login here</a>.</p>
"""

DASHBOARD_TEMPLATE = """
<!doctype html>
<title>JobFlow Cloud Mini – Dashboard</title>
<h1>JobFlow Cloud Mini – Sessions</h1>
<p>Logged in as {{ current_user.email }}</p>
<p><a href="{{ url_for('logout') }}">Logout</a></p>

{% if not sessions %}
  <p>No sessions yet. Once your local bot calls <code>/api/sync-session</code>,
  they will appear here.</p>
{% else %}
  <table border="1" cellpadding="6">
    <tr>
      <th>ID</th>
      <th>External ID</th>
      <th>Client</th>
      <th>Title</th>
      <th>Status</th>
      <th>Predicted Price</th>
      <th>Quote Total</th>
      <th>Created</th>
    </tr>
    {% for s in sessions %}
      <tr>
        <td>{{ s.id }}</td>
        <td>{{ s.external_id }}</td>
        <td>{{ s.client_name or '' }}</td>
        <td>{{ s.title or '' }}</td>
        <td>{{ s.status }}</td>
        <td>
          {% if s.predicted_price_cents is not none %}
            ${{ "%.2f"|format(from_cents(s.predicted_price_cents)) }}
          {% else %}
            -
          {% endif %}
        </td>
        <td>
          {% if s.quote and s.quote.total_cents is not none %}
            ${{ "%.2f"|format(from_cents(s.quote.total_cents)) }}
          {% else %}
            -
          {% endif %}
        </td>
        <td>{{ s.created_at }}</td>
      </tr>
    {% endfor %}
  </table>
{% endif %}
"""


# ====================================
# Auth Routes
# ====================================

@app.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm") or ""

        if not email or not password:
            flash("Email and password are required.", "danger")
            return redirect(url_for("register"))

        if password != confirm:
            flash("Passwords do not match.", "danger")
            return redirect(url_for("register"))

        existing = User.query.filter_by(email=email).first()
        if existing:
            flash("Email already registered. Please log in.", "warning")
            return redirect(url_for("login"))

        user = User(email=email)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()

        flash("Registration successful. Please log in.", "success")
        return redirect(url_for("login"))

    return render_template_string(REGISTER_TEMPLATE)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""

        user = User.query.filter_by(email=email).first()
        if user and user.check_password(password):
            login_user(user)
            return redirect(url_for("dashboard"))
        else:
            flash("Invalid email or password.", "danger")
            return redirect(url_for("login"))

    return render_template_string(LOGIN_TEMPLATE)


@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("Logged out.", "info")
    return redirect(url_for("login"))


# ====================================
# Dashboard
# ====================================

@app.route("/dashboard")
@login_required
def dashboard():
    sessions = (
        Session.query.filter_by(user_id=current_user.id)
        .order_by(Session.created_at.desc())
        .limit(50)
        .all()
    )
    return render_template_string(
        DASHBOARD_TEMPLATE,
        sessions=sessions,
        from_cents=from_cents,
    )


# ====================================
# API: Sync Session from Local Bot
# (Open auth for now so we can test easily)
# ====================================

@app.route("/api/sync-session", methods=["POST"])
def sync_session():
    """
    Upsert a session (and its uploads/quote) from your local JobFlow bot.

    Minimal required field: session_id (local external ID).
    """
    data = request.get_json(silent=True) or {}

    external_id = data.get("session_id")
    if not external_id:
        return jsonify({"error": "session_id is required"}), 400

    # Attach everything to the first user, or create a default one.
    user = User.query.order_by(User.id.asc()).first()
    if not user:
        user = User(email="default@jobflow.local")
        user.set_password("changeme")
        db.session.add(user)
        db.session.commit()

    session_obj = Session.query.filter_by(
        user_id=user.id,
        external_id=external_id,
    ).first()

    if not session_obj:
        session_obj = Session(
            user_id=user.id,
            external_id=external_id,
        )
        db.session.add(session_obj)

    session_obj.client_name = data.get("client_name")
    session_obj.title = data.get("title")
    session_obj.summary = data.get("summary")
    session_obj.transcript = data.get("transcript")
    session_obj.analysis = data.get("analysis") or {}
    session_obj.status = data.get("status") or "new"
    session_obj.predicted_price_cents = to_cents(data.get("predicted_price"))

    # Optional timestamps
    ts = data.get("timestamps") or {}
    created_str = ts.get("created_at")
    updated_str = ts.get("updated_at")

    def parse_iso(dt_str):
        if not dt_str:
            return None
        try:
            return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        except Exception:
            return None

    created_at = parse_iso(created_str)
    updated_at = parse_iso(updated_str)

    if created_at:
        session_obj.created_at = created_at
    if updated_at:
        session_obj.updated_at = updated_at

    # Uploads (replace existing)
    session_obj.uploads.delete()  # because lazy="dynamic"
    uploads = data.get("uploads") or []
    for u in uploads:
        url = u.get("url")
        if not url:
            continue
        upload = Upload(
            session=session_obj,
            kind=(u.get("type") or "other"),
            file_url=url,
            filename=u.get("filename"),
            mime_type=u.get("mime_type"),
            size_bytes=u.get("size_bytes"),
        )
        db.session.add(upload)

    # Quote + items
    quote_payload = data.get("quote") or {}
    if quote_payload:
        quote_obj = session_obj.quote
        if not quote_obj:
            quote_obj = Quote(session=session_obj)
            db.session.add(quote_obj)

        quote_obj.title = quote_payload.get("title")
        quote_obj.notes = quote_payload.get("notes")
        quote_obj.currency = quote_payload.get("currency") or "USD"

        quote_obj.items.delete()
        total_cents = 0

        items = quote_payload.get("items") or []
        for idx, item in enumerate(items):
            desc = item.get("description") or ""
            if not desc.strip():
                continue

            qty = item.get("quantity") or 1
            unit = item.get("unit") or "unit"
            unit_price = item.get("unit_price") or 0

            qty_val = float(qty)
            unit_price_c = to_cents(unit_price) or 0
            line_total_c = int(round(qty_val * unit_price_c))

            total_cents += line_total_c

            q_item = QuoteLineItem(
                quote=quote_obj,
                description=desc,
                quantity=qty_val,
                unit=unit,
                unit_price_cents=unit_price_c,
                line_total_cents=line_total_c,
                sort_order=idx,
            )
            db.session.add(q_item)

        quote_obj.total_cents = total_cents

    db.session.commit()

    # Response
    resp = {
        "session_id": session_obj.id,
        "external_id": session_obj.external_id,
        "client_name": session_obj.client_name,
        "title": session_obj.title,
        "status": session_obj.status,
        "predicted_price": from_cents(session_obj.predicted_price_cents),
        "quote_total": (
            from_cents(session_obj.quote.total_cents)
            if session_obj.quote and session_obj.quote.total_cents is not None
            else None
        ),
        "upload_count": session_obj.uploads.count(),
    }
    return jsonify(resp), 200


# ====================================
# Health + DB init
# ====================================

@app.route("/health")
def health():
    return jsonify({"status": "ok"})


with app.app_context():
    db.create_all()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5065")), debug=True)
