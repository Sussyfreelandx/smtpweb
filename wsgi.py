"""
WSGI entrypoint for production servers (gunicorn, uWSGI, Render, etc.)
"""
import eventlet

# --- CRITICAL FIX: Apply Eventlet Monkey-Patch FIRST ---
# This MUST be the first piece of code executed to ensure all standard libraries
# (like socket) are patched before any other module (e.g., redis, sqlalchemy)
# imports them. This resolves Redis connection issues under eventlet.
eventlet.monkey_patch()
# --------------------------------------------------------

# Now, import other modules
import os
from app import create_app, socketio

# Create the Flask app via factory
_app = None
try:
    _app = create_app()
except Exception as e:
    # It's better to log the exception here if you have a logger
    raise

# Expose the app object for WSGI servers
app = _app

if __name__ == "__main__":
    # Use the environment variables for host and port
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host=host, port=port)
