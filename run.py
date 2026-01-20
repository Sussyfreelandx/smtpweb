import os
import socket
import socks # pip install PySocks
import smtplib

# ==========================================
#   CRITICAL: RUN SCRIPT PROXY CONFIGURATION
# ==========================================
# This ensures manual runs (python run.py) also use the proxy tunnel.

PROXY_HOST = os.environ.get('SMTP_PROXY_HOST')
PROXY_PORT = int(os.environ.get('SMTP_PROXY_PORT', 1080))
PROXY_USER = os.environ.get('SMTP_PROXY_USER')
PROXY_PASS = os.environ.get('SMTP_PROXY_PASS')

if PROXY_HOST:
    # 1. Force IPv4 Resolution
    original_getaddrinfo = socket.getaddrinfo
    def patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
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
        print(f"🔌 Run Script Proxy Active: {PROXY_HOST}:{PROXY_PORT} (Auth: Yes)")
    else:
        socks.set_default_proxy(socks.SOCKS5, PROXY_HOST, PROXY_PORT)
        print(f"🔌 Run Script Proxy Active: {PROXY_HOST}:{PROXY_PORT} (Auth: No)")
    
    # 3. Patch smtplib
    socks.wrap_module(smtplib)

# ==========================================

from app import create_app, db, socketio
from app.models import User, Campaign, Recipient, SMTPServer, Suppression

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


if __name__ == '__main__':
    # Run with SocketIO support
    socketio.run(app, debug=True, host='0.0.0.0', port=5000)
