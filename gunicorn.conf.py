import os
import socket
import socks
import smtplib
import ipaddress

# ==========================================
#   GUNICORN CONFIGURATION (THREADED)
# ==========================================

worker_class = 'gthread'
workers = 2
threads = 4
timeout = 120
keepalive = 5

def post_worker_init(worker):
    """
    Apply SOCKS5 proxy settings safely, EXCLUDING internal Redis connections.
    """
    PROXY_HOST = os.environ.get('SMTP_PROXY_HOST')
    PROXY_PORT = int(os.environ.get('SMTP_PROXY_PORT', 1080))
    PROXY_USER = os.environ.get('SMTP_PROXY_USER')
    PROXY_PASS = os.environ.get('SMTP_PROXY_PASS')

    if PROXY_HOST:
        print(f"🔌 Worker: Applying Smart Proxy Patch ({PROXY_HOST}:{PROXY_PORT})...")
        
        # 1. Save original socket class
        original_socket = socket.socket

        # 2. Define Custom Socket that ignores internal addresses AND Private IPs
        class SmartSocket(socks.socksocket):
            def connect(self, dest_pair):
                host, port = dest_pair
                
                is_internal = False
                
                # Check 1: Hostname strings
                if isinstance(host, str):
                    if host.startswith("red-") or "render.internal" in host or host == "localhost":
                        is_internal = True
                
                # Check 2: IP Addresses (Redis client often resolves DNS first)
                if not is_internal:
                    try:
                        # Check if it's a private IP (10.x, 172.16.x, 192.168.x, 127.x)
                        ip = ipaddress.ip_address(host)
                        if ip.is_private or ip.is_loopback:
                            is_internal = True
                    except ValueError:
                        # Not an IP address, ignore
                        pass

                if is_internal:
                    # Revert to standard non-proxy connection for internal services
                    self.set_proxy(None)
                else:
                    # Enforce proxy for everything else (SMTP, External APIs)
                    if PROXY_USER and PROXY_PASS:
                        self.set_proxy(socks.SOCKS5, PROXY_HOST, PROXY_PORT, True, PROXY_USER, PROXY_PASS)
                    else:
                        self.set_proxy(socks.SOCKS5, PROXY_HOST, PROXY_PORT)
                
                return super(SmartSocket, self).connect(dest_pair)

        # 3. Patch socket.socket globally
        socket.socket = SmartSocket
        
        # 4. Force IPv4 Resolution (Fixes IPv6/PySocks crash)
        original_getaddrinfo = socket.getaddrinfo
        def patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
            if family == 0 or family == socket.AF_INET6:
                try:
                    return original_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)
                except socket.gaierror:
                    pass
            return original_getaddrinfo(host, port, family, type, proto, flags)
        socket.getaddrinfo = patched_getaddrinfo
