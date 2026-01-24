"""
WSGI entrypoint for production servers (gunicorn, uWSGI, Render, etc.)
"""
import socks
import eventlet

# --- START FIX: Infinite Recursion Loop ---
# This function patches socks.socksocket.setblocking to prevent infinite recursion
# when used with eventlet. It breaks the loop:
# setblocking -> settimeout -> eventlet override -> setblocking -> ...
def _patched_setblocking(self, flag):
    desired_timeout = None if flag else 0.0
    
    # If the timeout is already set to the desired value, do nothing.
    # This check is what prevents the infinite recursion.
    if self.gettimeout() == desired_timeout:
        return

    self.settimeout(desired_timeout)

# Apply the patch to the socks library class
socks.socksocket.setblocking = _patched_setblocking
# --- END FIX ---

# Now apply the eventlet monkey patch.
# We can remove os=False unless you specifically need it for other reasons;
# the recursion fix above handles the main socket conflict.
eventlet.monkey_patch()

# Now, import other modules
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
