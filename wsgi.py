import eventlet
# CRITICAL: Eventlet monkey patching must happen BEFORE any other imports
eventlet.monkey_patch()

import os
import socket
import socks  # pip install PySocks
import smtplib
import logging

# ==========================================
#   CRITICAL: WSGI PROXY CONFIGURATION
# ==========================================
# This ensures Gunicorn workers are patched correctly without recursion.

PROXY_HOST = os.environ.get('SMTP_PROXY_HOST')
PROXY_PORT = int(os.environ.get('SMTP_PROXY_PORT', 1080))
PROXY_USER = os.environ.get('SMTP_PROXY_USER')
PROXY_PASS = os.environ.get('SMTP_PROXY_PASS')

# GUARD: Check if we have already patched to prevent RecursionError
if PROXY_HOST and not getattr(socket, '_paris_proxy_patched', False):
    print(f"🔌 WSGI: Applying Proxy Patch ({PROXY_HOST}:{PROXY_PORT})...")
    
    # 1. Force IPv4 Resolution (Fixes Office365/Gmail crashes on Render)
    # We save the *current* getaddrinfo (which might be eventlet-patched)
    original_getaddrinfo = socket.getaddrinfo

    def patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        # Force IPv4 (AF_INET) if we are resolving a hostname
        if family == 0 or family == socket.AF_INET6:
            try:
                return original_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)
            except socket.gaierror:
                pass
        return original_getaddrinfo(host, port, family, type, proto, flags)

    socket.getaddrinfo = patched_getaddrinfo
    
    # 2. Configure Proxy
    if PROXY_USER and PROXY_PASS: 
        socks.set_default_proxy(socks.SOCKS5, PROXY_HOST, PROXY_PORT, username=PROXY_USER, password=PROXY_PASS)
        print(f"🔌 WSGI: Proxy Auth Configured")
    else:
        socks.set_default_proxy(socks.SOCKS5, PROXY_HOST, PROXY_PORT)
        print(f"🔌 WSGI: Proxy Configured (No Auth)")
    
    # 3. Patch smtplib
    socks.wrap_module(smtplib)
    
    # Mark as patched so we don't do it again if this file is re-imported
    socket._paris_proxy_patched = True

# ==========================================

# CRITICAL: Import celery here so 'celery -A wsgi.celery' works
from app import create_app, db, socketio, celery
from app.models import User, Campaign, Recipient, SMTPServer, Suppression

# Create a fresh app instance for the web server
app = create_app()

@app.shell_context_processor
def make_shell_context():
    return {
        'db': db,
        'User': User,
        'Campaign': Campaign,
        'Recipient': Recipient,
        'SMTPServer': SMTPServer,
        'Suppression': Suppression
    }

# For Gunicorn with eventlet
if __name__ == '__main__': 
    socketio.run(app, debug=False, host='0.0.0.0', port=5000)