import os
import socket
import socks
import smtplib
import ipaddress
from app import create_app, db, socketio, celery
from app.models import User, Campaign, Recipient, SMTPServer, Suppression

# ==========================================
#   SMART PROXY CONFIGURATION
# ==========================================
PROXY_HOST = os.environ.get('SMTP_PROXY_HOST')

# Only apply if not already applied by Gunicorn
if PROXY_HOST and socket.socket is not socks.socksocket and not getattr(socket, '_paris_proxy_patched', False):
    PROXY_PORT = int(os.environ.get('SMTP_PROXY_PORT', 1080))
    PROXY_USER = os.environ.get('SMTP_PROXY_USER')
    PROXY_PASS = os.environ.get('SMTP_PROXY_PASS')
    
    print(f"🔌 WSGI (Fallback): Applying Smart Proxy Patch...")
    
    # Define Smart Socket
    class SmartSocket(socks.socksocket):
        def connect(self, dest_pair):
            host, port = dest_pair
            is_internal = False
            
            # Check Hostnames
            if isinstance(host, str):
                if host.startswith("red-") or "render.internal" in host or host == "localhost":
                    is_internal = True
            
            # Check IPs
            if not is_internal:
                try:
                    ip = ipaddress.ip_address(host)
                    if ip.is_private or ip.is_loopback:
                        is_internal = True
                except ValueError:
                    pass

            if is_internal:
                self.set_proxy(None)
            else:
                if PROXY_USER and PROXY_PASS:
                    self.set_proxy(socks.SOCKS5, PROXY_HOST, PROXY_PORT, True, PROXY_USER, PROXY_PASS)
                else:
                    self.set_proxy(socks.SOCKS5, PROXY_HOST, PROXY_PORT)
            return super(SmartSocket, self).connect(dest_pair)

    # Apply Patch
    socket.socket = SmartSocket
    
    # Force IPv4
    original_getaddrinfo = socket.getaddrinfo
    def patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        if family == 0 or family == socket.AF_INET6:
            try:
                return original_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)
            except socket.gaierror:
                pass
        return original_getaddrinfo(host, port, family, type, proto, flags)
    socket.getaddrinfo = patched_getaddrinfo
    
    socket._paris_proxy_patched = True

# ==========================================

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
