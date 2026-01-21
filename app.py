# app.py
# Compatibility shim. Some configs/old Procfiles may reference app:app.
# Always re-export the real app from jobflow_api.py.

from jobflow_api import app as app
