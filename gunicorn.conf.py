import os

# ==========================================
#   GUNICORN CONFIGURATION (THREADED)
# ==========================================

worker_class = 'gthread'
workers = 2
threads = 4
timeout = 120
keepalive = 5

# REMOVED: post_worker_init hook that was patching sockets globally.
# The proxy logic is now securely handled inside app/core_logic/smtp_handler.py
# ensuring it only applies to SMTP connections and doesn't crash Redis/Celery.
