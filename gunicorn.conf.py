import os

# ==========================================
#   GUNICORN CONFIGURATION (EVENTLET)
# ==========================================

# CRITICAL: Must be 'eventlet' to support WebSockets and monkey-patching.
# DO NOT use 'gthread' or 'sync' here, or it will crash with RuntimeError.
worker_class = 'eventlet'

# For Eventlet, 1 worker per CPU core is usually sufficient as it handles
# concurrency via greenlets, not threads.
workers = 1

# Threads are NOT used with the eventlet worker class.
threads = 1

timeout = 120
keepalive = 5

# Logging
loglevel = 'info'
accesslog = '-'
errorlog = '-'
