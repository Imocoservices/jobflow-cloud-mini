import os
import uuid
import datetime
from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from werkzeug.utils import secure_filename
from openai import OpenAI

# ============================================================
#  App + Config
# ============================================================

app = Flask(__name__)
CORS(app)

DATABASE_URL = os.environ.get("DATABASE_URL")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)
client = OpenAI(api_key=OPENAI_API_KEY)

UPLOAD_ROOT = "uploads"
os.makedirs(UPLOAD_ROOT, exist_ok=True)

# ============================================================
#  Models
# ============================================================

class Session(db.Model):
    __tablename__ = "sessions"

    id = db.Column(db.String, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    payload = db.Column(db.JSON, default={})


class Media(db.Model):
    __tablename__ = "media"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    session_id = db.Column(db.String, db.ForeignKey("sessions.id"))
    media_type = db.Column(db.String)  # "image" or "audio"
    filename = db.Column(db.String)
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)


class Analysis(db.Model):
    __tablename__ = "analysis"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    session_id = db.Column(db.String, db.ForeignKey("sessions.id"))
    result = db.Column(db.JSON)
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)


# ============================================================
#  Schema Init (SAFE)
# ============================================================

def init_schema():
    with app.app_context():
        db.create_all()


# Gunicorn will import this file → Flask will call this
init_schema()

# ============================================================
#  Helpers
# ============================================================

def ensure_session(sid: str):
    session = Session.query.get(sid)
    if not session:
        session = Session(id=sid, payload={})
        db.session.add(session)
        db.session.commit()
    else:
        session.updated_at = datetime.datetime.utcnow()
        db.session.commit()
    return session


def save_media_file(sid, file_storage, media_type):
    folder = os.path.join(UPLOAD_ROOT, sid)
    os.makedirs(folder, exist_ok=True)

    filename = secure_filename(file_storage.filename)
    path = os.path.join(folder, filename)
    file_storage.save(path)

    m = Media(session_id=sid, media_type=media_type, filename=filename)
    db.session.add(m)
    db.session.commit()

    return path


# ============================================================
#  Endpoints
# ============================================================

@app.route("/api/health")
def health():
    return jsonify({"ok": True, "service": "jobflow-cloud-mini"})


@app.route("/api/sessions", methods=["GET"])
def list_sessions():
    items = Session.query.order_by(Session.created_at.desc()).limit(50).all()
    return jsonify([
        {
            "id": s.id,
            "created_at": s.created_at.isoformat(),
            "updated_at": s.updated_at.isoformat(),
        }
        for s in items
    ])


@app.route("/api/sessions/<sid>", methods=["GET"])
def get_session(sid):
    s = Session.query.get(sid)
    if not s:
        return jsonify({"error": "not found"}), 404

    media_items = Media.query.filter_by(session_id=sid).all()
    analysis_items = Analysis.query.filter_by(session_id=sid).all()

    return jsonify({
        "id": s.id,
        "payload": s.payload,
        "media": [
            {"id": m.id, "type": m.media_type, "filename": m.filename}
            for m in media_items
        ],
        "analysis": [
            {"id": a.id, "result": a.result}
            for a in analysis_items
        ],
    })


@app.route("/api/sessions/<sid>/upsert", methods=["POST", "PUT"])
def upsert_session(sid):
    ensure_session(sid)
    return jsonify({"ok": True, "id": sid})


@app.route("/api/sessions/<sid>/image", methods=["POST"])
def upload_image(sid):
    ensure_session(sid)
    file = request.files.get("file")
    if not file:
        return jsonify({"error": "no file"}), 400
    save_media_file(sid, file, "image")
    return jsonify({"ok": True})


@app.route("/api/sessions/<sid>/audio", methods=["POST"])
def upload_audio(sid):
    ensure_session(sid)
    file = request.files.get("file")
    if not file:
        return jsonify({"error": "no file"}), 400
    save_media_file(sid, file, "audio")
    return jsonify({"ok": True})


@app.route("/api/sessions/<sid>/analyze", methods=["POST"])
def analyze(sid):
    s = Session.query.get(sid)
    if not s:
        return jsonify({"error": "not found"}), 404

    # Collect all media filenames
    media = Media.query.filter_by(session_id=sid).all()
    text_summary = f"Session {sid} has {len(media)} media files."

    # Save analysis
    a = Analysis(session_id=sid, result={"summary": text_summary})
    db.session.add(a)
    db.session.commit()

    return jsonify({"ok": True, "analysis": a.result})


# ============================================================
#  Main (Render uses gunicorn, but this allows local runs)
# ============================================================

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000, debug=True)
