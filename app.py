# app.py
# Optional: some platforms use "app:app" as entrypoint.
# We expose the same Flask app from jobflow_api.py.

from jobflow_api import app  # noqa: F401
