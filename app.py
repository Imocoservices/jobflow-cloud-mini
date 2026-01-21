# app.py
# Compatibility shim (NOT the Render entrypoint)
# Render should run: jobflow_api:app

from jobflow_api import app  # noqa: F401
