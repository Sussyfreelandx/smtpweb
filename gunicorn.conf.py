import os

# ==========================================
#   GUNICORN CONFIGURATION
# ==========================================

# Bind to port 10000 (Render's default)
bind = "0.0.0.0:10000"

# Worker configuration - Use eventlet for Socket.IO support
worker_class = 'eventlet'
workers = 1  # For eventlet/socket.io, 1 worker is often sufficient for many connections, scale if needed
threads = 1  # Threads are not used with eventlet
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
    
    # Ensure eventlet patching happened
    try:
        import eventlet
        eventlet.monkey_patch()
    except ImportError:
        pass

    PROXY_HOST = os.environ.get('SMTP_PROXY_HOST')
    PROXY_PORT = int(os.environ.get('SMTP_PROXY_PORT', 1080))
    PROXY_USER = os.environ.get('SMTP_PROXY_USER')
    PROXY_PASS = os.environ.get('SMTP_PROXY_PASS')

    if PROXY_HOST and not getattr(socket, '_paris_proxy_patched', False):
        import socks
        import ipaddress
        
        print(f"🔌 Worker: Applying Smart Proxy Patch ({PROXY_HOST}:{PROXY_PORT})...")
        
        # We patch the socket module that is already monkey-patched by eventlet
        # This is tricky but required for SOCKS support in async workers
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
                        # Simple check if IP is private
                        try:
                            ip = ipaddress.ip_address(host)
                            if ip.is_private or ip.is_loopback:
                                is_internal = True
                        except ValueError:
                            pass
                    except Exception:
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
        
        # Patch getaddrinfo to handle IPv6/IPv4 preference if needed
        original_getaddrinfo = socket.getaddrinfo
        def patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
            return original_getaddrinfo(host, port, family, type, proto, flags)
            
        socket.getaddrinfo = patched_getaddrinfo
        
        socket._paris_proxy_patched = True
        print(f"✅ Worker: Proxy patch applied")
