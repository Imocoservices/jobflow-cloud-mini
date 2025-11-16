# jobflow_cloud/config.py
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

class Config:
    # Core
    SECRET_KEY = os.getenv("SECRET_KEY", "change-me-please")

    # Database: use DATABASE_URL on Render, fallback to local SQLite
    DATABASE_URL = os.getenv("DATABASE_URL")
    if DATABASE_URL:
        SQLALCHEMY_DATABASE_URI = DATABASE_URL.replace("postgres://", "postgresql://")
    else:
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{BASE_DIR / 'cloud.db'}"

    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Where we store meta.json / report.json
    CLOUD_OUTPUT_DIR = os.getenv("CLOUD_OUTPUT_DIR", str(BASE_DIR / "cloud_output"))

    # Extra security for imports
    IMPORT_TOKEN = os.getenv("IMPORT_TOKEN", "")

    # Registration gate (optional)
    ACCESS_CODE = os.getenv("ACCESS_CODE", "")

    # Branding / white-label
    BRAND_NAME = os.getenv("BRAND_NAME", "JobFlow AI")
    PRIMARY_COLOR = os.getenv("PRIMARY_COLOR", "#667eea")
    ACCENT_COLOR = os.getenv("ACCENT_COLOR", "#764ba2")
    LOGO_URL = os.getenv("LOGO_URL", "/static/logo.png")

    # Version
    APP_VERSION = "2.0.0"
