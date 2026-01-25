"""
WSGI entrypoint for production servers (gunicorn, uWSGI, Render, etc.)
"""
import socks
import socket
import eventlet

# --- CRITICAL FIX: Infinite Recursion & Redis Connection Loop ---
# 1. Patch setblocking to prevent infinite recursion
def _patched_setblocking(self, flag):
    desired_timeout = None if flag else 0.0
    if self.gettimeout() == desired_timeout:
        return
    self.settimeout(desired_timeout)

socks.socksocket.setblocking = _patched_setblocking

# 2. Apply eventlet monkey patch explicitly
eventlet.monkey_patch()

# 3. FIX FOR REDIS: Ensure socks.socksocket uses the GreenSocket
# After monkey_patch(), socket.socket is now GreenSocket.
# We must ensure socks.socksocket inherits from it to avoid "bad file descriptor"
# or connection errors in Redis.
socks.socksocket = socket.socket
# ----------------------------------------------------------------

# Now, import other modules
import os
from app import create_app, socketio

# Create the Flask app via factory
_app = None
try:
    _app = create_app()
except Exception as e:
    raise

# Expose the app object for WSGI servers
app = _app

if __name__ == "__main__":
    socketio.run(app, host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", 5000)))
