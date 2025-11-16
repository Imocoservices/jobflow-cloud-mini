# jobflow_cloud/app.py
from pathlib import Path
from datetime import datetime

from flask import Flask, jsonify, redirect, url_for
from flask_login import LoginManager, current_user

from .config import Config
from .models import db, User


login_manager = LoginManager()
login_manager.login_view = "auth.login"


def create_app() -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config.from_object(Config)

    db.init_app(app)
    login_manager.init_app(app)

    from .auth import auth_bp
    from .routes.sessions import sessions_bp
    from .routes.import_api import api_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(sessions_bp)
    app.register_blueprint(api_bp)

    # Create DB + directories
    with app.app_context():
        db.create_all()
        out_dir = Path(app.config["CLOUD_OUTPUT_DIR"]) / "sessions"
        out_dir.mkdir(parents=True, exist_ok=True)

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(User, user_id)

    @app.context_processor
    def inject_brand():
        return dict(
            brand_name=app.config["BRAND_NAME"],
            primary_color=app.config["PRIMARY_COLOR"],
            accent_color=app.config["ACCENT_COLOR"],
            logo_url=app.config["LOGO_URL"],
            app_version=app.config["APP_VERSION"],
            now=datetime.utcnow(),
        )

    @app.route("/")
    def index():
        if current_user.is_authenticated:
            return redirect(url_for("sessions.sessions_view"))
        return jsonify(
            {
                "app": "JobFlow AI Cloud v2",
                "health": "/api/health",
                "sessions": "/sessions",
                "version": app.config["APP_VERSION"],
                "version_api": "/api/version",
            }
        )

    return app
