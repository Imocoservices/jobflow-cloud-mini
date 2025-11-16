# jobflow_cloud/config.py
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "change-me-please")

    DATABASE_URL = os.getenv("DATABASE_URL")
    if DATABASE_URL:
        SQLALCHEMY_DATABASE_URI = DATABASE_URL.replace("postgres://", "postgresql://")
    else:
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{BASE_DIR / 'cloud.db'}"

    SQLALCHEMY_TRACK_MODIFICATIONS = False

    CLOUD_OUTPUT_DIR = os.getenv(
        "CLOUD_OUTPUT_DIR", str(BASE_DIR.parent / "cloud_output")
    )

    IMPORT_TOKEN = os.getenv("IMPORT_TOKEN", "")
    ACCESS_CODE = os.getenv("ACCESS_CODE", "")

    BRAND_NAME = os.getenv("BRAND_NAME", "JobFlow AI")
    PRIMARY_COLOR = os.getenv("PRIMARY_COLOR", "#667eea")
    ACCENT_COLOR = os.getenv("ACCENT_COLOR", "#764ba2")
    LOGO_URL = os.getenv("LOGO_URL", "/static/img/logo.png")

    APP_VERSION = "2.0.0"
