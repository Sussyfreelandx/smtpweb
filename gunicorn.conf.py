import os

# ==========================================
#   GUNICORN CONFIGURATION (EVENTLET)
# ==========================================

# CRITICAL: Must match the worker class used in render.yaml and requirements
worker_class = 'eventlet'

# Eventlet workers can handle thousands of concurrent connections via greenlets,
# so we typically only need 1 process per CPU core.
workers = 1

# Threads are not used with the eventlet worker class
threads = 1

timeout = 120
keepalive = 5

# Logging
loglevel = 'info'
accesslog = '-'  # Log to stdout
errorlog = '-'   # Log to stderr
