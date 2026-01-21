import os
import sys

# Gunicorn configuration
workers = 1
worker_class = 'eventlet'
worker_connections = 1000
timeout = 120
keepalive = 2

def post_worker_init(worker):
    """
    Called just after a worker has been initialized.
    We must patch here to ensure it happens before ANY application code runs.
    """
    import eventlet
    import socket
    
    # 1. Patch Eventlet immediately to prevent "blocking functions" error
    eventlet.monkey_patch()
    
    # 2. Apply Proxy Patch safely
    apply_proxy_patch()

def apply_proxy_patch():
    """Apply SOCKS5 proxy settings safely."""
    import socket
    import socks
    import smtplib
    
    # Prevent double-patching which causes RecursionErrors
    if getattr(socket, '_paris_proxy_patched', False):
        return

    PROXY_HOST = os.environ.get('SMTP_PROXY_HOST')
    PROXY_PORT = int(os.environ.get('SMTP_PROXY_PORT', 1080))
    PROXY_USER = os.environ.get('SMTP_PROXY_USER')
    PROXY_PASS = os.environ.get('SMTP_PROXY_PASS')

    if PROXY_HOST:
        print(f"🔌 Worker: Applying Proxy Patch ({PROXY_HOST}:{PROXY_PORT})...")
        
        # Save original getaddrinfo to prevent infinite recursion
        original_getaddrinfo = socket.getaddrinfo

        def patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
            # Force IPv4 (AF_INET) if we are resolving a hostname
            # This prevents PySocks from crashing on IPv6 addresses in Render
            if family == 0 or family == socket.AF_INET6:
                try:
                    return original_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)
                except socket.gaierror:
                    pass
            return original_getaddrinfo(host, port, family, type, proto, flags)

        # Apply the patches
        socket.getaddrinfo = patched_getaddrinfo
        
        if PROXY_USER and PROXY_PASS: 
            socks.set_default_proxy(socks.SOCKS5, PROXY_HOST, PROXY_PORT, username=PROXY_USER, password=PROXY_PASS)
        else:
            socks.set_default_proxy(socks.SOCKS5, PROXY_HOST, PROXY_PORT)
        
        socks.wrap_module(smtplib)
        
        # Mark as patched to avoid recursion
        socket._paris_proxy_patched = True
