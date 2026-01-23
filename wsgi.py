"""
WSGI entrypoint for production servers (gunicorn, uWSGI, Render, etc.)

Set FLASK_APP=wsgi:app when using flask CLI or let your WSGI server import `app`.
"""

import os
from app import create_app, socketio

# Create the Flask app via factory
_app = None
try:
    _app = create_app()
except Exception as e:
    # If app creation fails, raise so that the process fails loudly during deployment
    raise

# Expose the app object for WSGI servers
app = _app

# Optional: expose socketio.run when running locally with "python wsgi.py"
if __name__ == "__main__":  # pragma: no cover
    # Use eventlet or gevent if available for production websockets in simple runs
    socketio.run(app, host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", 5000)))
