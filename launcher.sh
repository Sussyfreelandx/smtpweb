#!/bin/bash
# exit on error
set -o errexit

# Execute Gunicorn using the configuration file.
# This ensures that the same settings are used for both local and production environments.
# The worker class is now consistently set to 'gthread' via gunicorn.conf.py,
# resolving the previous conflict with 'eventlet'.
exec gunicorn -c gunicorn.conf.py wsgi:app
