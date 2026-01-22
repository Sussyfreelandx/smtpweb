#!/bin/bash
# exit on error
set -o errexit

# Apply eventlet monkey-patching via a startup hook.
# This is the most robust way to ensure Gunicorn's arbiter and workers
# are all patched correctly, preventing the "do not call blocking functions" crash.
exec gunicorn wsgi:app --worker-class eventlet --preload
