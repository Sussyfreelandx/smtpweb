import os

# ==========================================
#   GUNICORN CONFIGURATION
# ==========================================

# Bind to port 10000 (Render's default)
bind = "0.0.0.0:10000"

# Worker configuration - use threads, not eventlet
worker_class = 'gthread'
workers = 2
threads = 4
timeout = 120
keepalive = 5

# Logging
accesslog = '-'
errorlog = '-'
loglevel = 'info'

# Pre-load application
preload_app = False

def post_worker_init(worker):
    """
    Apply SOCKS5 proxy settings safely after worker starts. 
    """
    import socket
    import os
    
    PROXY_HOST = os.environ.get('SMTP_PROXY_HOST')
    PROXY_PORT = int(os.environ.get('SMTP_PROXY_PORT', 1080))
    PROXY_USER = os.environ.get('SMTP_PROXY_USER')
    PROXY_PASS = os.environ.get('SMTP_PROXY_PASS')

    if PROXY_HOST and not getattr(socket, '_paris_proxy_patched', False):
        import socks
        import ipaddress
        
        print(f"🔌 Worker: Applying Smart Proxy Patch ({PROXY_HOST}:{PROXY_PORT})...")
        
        original_socket = socket.socket

        class SmartSocket(socks.socksocket):
            def connect(self, dest_pair):
                host, port = dest_pair
                is_internal = False
                
                if isinstance(host, str):
                    if host.startswith("red-") or "render.internal" in host or host == "localhost":
                        is_internal = True
                
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

        socket.socket = SmartSocket
        
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
        print(f"✅ Worker:  Proxy patch applied")
