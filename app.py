# app.py
# Render entrypoint: Procfile uses "app:app"
# This file exposes the real Flask app and adds debug endpoints
# so we can prove what Render is actually running.

from jobflow_api import app as app  # the real app

from flask import jsonify

@app.route("/__whoami", methods=["GET"])
def __whoami():
    return jsonify({
        "app_py": __file__,
        "imported_app_module": getattr(app, "import_name", None),
    })

@app.route("/__routes", methods=["GET"])
def __routes():
    rules = []
    for r in app.url_map.iter_rules():
        rules.append({
            "rule": str(r),
            "endpoint": r.endpoint,
            "methods": sorted([m for m in r.methods if m not in ("HEAD", "OPTIONS")]),
        })
    rules = sorted(rules, key=lambda x: x["rule"])
    return jsonify(rules)
