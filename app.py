# app.py
# Render entrypoint: Procfile uses "app:app"
# This file exposes the real Flask app from jobflow_api.py

from jobflow_api import app  # noqa: F401
