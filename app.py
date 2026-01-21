# app.py (Render entrypoint)
from flask import jsonify
from jobflow_api import app as app  # import the real Flask app

@app.route("/__whoami", methods=["GET"])
def __whoami():
    return jsonify({
        "entrypoint_file": __file__,
        "imported_app_import_name": getattr(app, "import_name", None),
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
    rules.sort(key=lambda x: x["rule"])
    return jsonify(rules)
