from app import app

# This is the entry point for gunicorn: `gunicorn wsgi:app`
# Do NOT rename `app` here, gunicorn expects this name.

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5065, debug=True)
