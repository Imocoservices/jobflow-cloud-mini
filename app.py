# app.py
# Render entrypoint shim.
# Procfile uses: app:app
# We forward to the real Flask app in jobflow_api.py.

from jobflow_api import app as app  # noqa: F401

# Optional: if jobflow_api defines routes but not "/", ensure "/" exists.
# If "/" already exists in jobflow_api, this won't break anything.
try:
    from flask import redirect

    if "root_redirect" not in [r.endpoint for r in app.url_map.iter_rules()]:
        @app.route("/", methods=["GET"])
        def root_redirect():
            return redirect("/ui", code=302)
except Exception:
    # Never fail import because of a helper redirect.
    pass
