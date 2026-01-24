#!/bin/bash
# exit on error
set -o errexit

# Apply eventlet monkey-patching via a startup hook is done in wsgi.py
# We simply run gunicorn using the config file we updated.
exec gunicorn -c gunicorn.conf.py wsgi:app
