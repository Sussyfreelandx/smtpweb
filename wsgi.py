"""
WSGI entrypoint for production servers (gunicorn, uWSGI, Render, etc.)
"""
import eventlet
# Monkey patch must be the very first thing. os=False is the fix.
eventlet.monkey_patch(os=False)

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
    socketio.run(app, host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", 5000)))
