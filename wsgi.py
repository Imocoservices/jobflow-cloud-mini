from app import app

# Entry point for Render + gunicorn
# gunicorn runs: gunicorn wsgi:app
if __name__ == "__main__":
    app.run()
