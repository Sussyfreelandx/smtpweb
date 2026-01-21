import eventlet
# Monkey patch immediately before anything else loads
eventlet.monkey_patch()

# Gunicorn config
bind = "0.0.0.0:10000"
workers = 1
worker_class = "eventlet"
timeout = 120
keepalive = 5

# Logging
errorlog = "-"
loglevel = "info"
accesslog = "-"

def on_starting(server):
    """
    Hook to verify patching on startup.
    """
    import socket
    if not hasattr(socket, 'is_patched'):
        print("!!! WARNING: Socket does not appear to be patched! !!!")
