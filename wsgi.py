import os
import socket
import socks
import smtplib
from app import create_app, db, socketio, celery
from app.models import User, Campaign, Recipient, SMTPServer, Suppression

# Apply Proxy Patch fallback (if not handled by gunicorn config)
# This check ensures we don't double-patch, which causes recursion errors.
PROXY_HOST = os.environ.get('SMTP_PROXY_HOST')
if PROXY_HOST and not getattr(socket, '_paris_proxy_patched', False):
    try:
        import eventlet
        eventlet.monkey_patch()
    except ImportError:
        pass

    PROXY_PORT = int(os.environ.get('SMTP_PROXY_PORT', 1080))
    PROXY_USER = os.environ.get('SMTP_PROXY_USER')
    PROXY_PASS = os.environ.get('SMTP_PROXY_PASS')
    
    print(f"🔌 WSGI: Applying Proxy Patch ({PROXY_HOST}:{PROXY_PORT})...")
    
    # Save original to prevent recursion
    original_getaddrinfo = socket.getaddrinfo
    def patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        if family == 0 or family == socket.AF_INET6:
            try:
                return original_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)
            except socket.gaierror:
                pass
        return original_getaddrinfo(host, port, family, type, proto, flags)
    socket.getaddrinfo = patched_getaddrinfo
    
    if PROXY_USER and PROXY_PASS: 
        socks.set_default_proxy(socks.SOCKS5, PROXY_HOST, PROXY_PORT, username=PROXY_USER, password=PROXY_PASS)
    else:
        socks.set_default_proxy(socks.SOCKS5, PROXY_HOST, PROXY_PORT)
    
    socks.wrap_module(smtplib)
    socket._paris_proxy_patched = True

# Create app instance
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
    socketio.run(app, debug=False, host='0.0.0.0', port=5000)
