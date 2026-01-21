# app.py
# Render entrypoint: Procfile uses "app:app"
# This file exposes the real Flask app.

from jobflow_api import app as app  # noqa: F401
