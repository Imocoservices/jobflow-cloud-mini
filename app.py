# app.py
# Render/Gunicorn entrypoint.
# Procfile uses: web: gunicorn ... app:app
#
# We export the real Flask app from jobflow_api.py so ALL routes exist on Render:
# - /ui
# - /sessions
# - /health
# - etc.
#
# We also add a couple of convenience routes:
# - /           -> redirect to /ui
# - /api/health -> alias to /health (so both work)

from __future__ import annotations

from flask import redirect, jsonify

# IMPORTANT: this must import the Flask instance named "app"
from jobflow_api import app as app  # noqa: F401


def _has_route(rule: str) -> bool:
    try:
        for r in app.url_map.iter_rules():
            if r.rule == rule:
                return True
    except Exception:
        # If anything weird happens, just don't block startup.
        return False
    return False


# Root should go somewhere useful (your UI)
if not _has_route("/"):
    app.add_url_rule("/", endpoint="root_redirect", view_func=lambda: redirect("/ui", code=302))

# Keep your existing /health, but also support /api/health (you already tested that URL)
if not _has_route("/api/health"):

    def api_health():
        # Prefer the canonical health if it exists; otherwise return ok.
        if _has_route("/health"):
            return redirect("/health", code=302)
        return jsonify({"status": "ok"})

    app.add_url_rule("/api/health", endpoint="api_health", view_func=api_health, methods=["GET"])
