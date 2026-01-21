import os
import socket
import socks
import smtplib

# ==========================================
#   GUNICORN CONFIGURATION (THREADED)
# ==========================================

# Use 'gthread' for standard threaded behavior
# This eliminates "blocking function" errors caused by eventlet
worker_class = 'gthread'
workers = 2
threads = 4  # Allow concurrent requests per worker
timeout = 120
keepalive = 5

def post_worker_init(worker):
    """
    Apply SOCKS5 proxy settings when worker starts.
    In threaded mode, this is safe and reliable.
    """
    PROXY_HOST = os.environ.get('SMTP_PROXY_HOST')
    PROXY_PORT = int(os.environ.get('SMTP_PROXY_PORT', 1080))
    PROXY_USER = os.environ.get('SMTP_PROXY_USER')
    PROXY_PASS = os.environ.get('SMTP_PROXY_PASS')

    if PROXY_HOST:
        print(f"🔌 Worker: Applying Proxy Patch ({PROXY_HOST}:{PROXY_PORT})...")
        
        # 1. Force IPv4 Resolution (Fixes IPv6/PySocks crash)
        original_getaddrinfo = socket.getaddrinfo

        def patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
            if family == 0 or family == socket.AF_INET6:
                try:
                    return original_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)
                except socket.gaierror:
                    pass
            return original_getaddrinfo(host, port, family, type, proto, flags)

        socket.getaddrinfo = patched_getaddrinfo
        
        # 2. Set Default Proxy
        if PROXY_USER and PROXY_PASS: 
            socks.set_default_proxy(socks.SOCKS5, PROXY_HOST, PROXY_PORT, username=PROXY_USER, password=PROXY_PASS)
        else:
            socks.set_default_proxy(socks.SOCKS5, PROXY_HOST, PROXY_PORT)
        
        # 3. Wrap SMTP
        socks.wrap_module(smtplib)
