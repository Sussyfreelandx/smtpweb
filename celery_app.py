from celery import Celery
import os
import ssl

# ===========================================
# UNIFIED CELERY APPLICATION DEFINITION
# ===========================================
# This file now ONLY defines the Celery object. 
# It is configured inside the application factory to avoid circular imports.

# Get Redis URL from environment. This is safe to do at the module level.
redis_url = os.environ.get('REDIS_URL')
if not redis_url:
    # Use a dummy URL if not set, it will be overridden by the app config later.
    # This allows the module to be imported without crashing.
    redis_url = 'redis://localhost:6379/0'

# Ensure Redis URL is in the 'rediss://' format for SSL on Render.
if 'RENDER' in os.environ and redis_url.startswith('redis://'):
    redis_url = redis_url.replace('redis://', 'rediss://', 1)

# Define the (initially) unconfigured Celery instance.
# IMPORTANT: we intentionally do NOT import/`include` task modules here.
# The Celery worker entrypoint (celery_worker.py) configures the task base class
# to run inside the Flask application context *before* importing tasks.
celery = Celery(
    'app',  # Use 'app' as the main app name
    backend=redis_url,
    broker=redis_url,
)

# Set a base configuration. This will also be updated by the app factory.
celery.conf.update(
    task_serializer='json',
    accept_content=['json'],
    result_serializer='json',
    timezone='UTC',
    enable_utc=True,
    broker_connection_retry_on_startup=True
)

# If on Render, apply SSL settings directly from the environment check.
if 'RENDER' in os.environ:
    celery.conf.update(
        broker_use_ssl={'ssl_cert_reqs': ssl.CERT_NONE},
        redis_backend_use_ssl={'ssl_cert_reqs': ssl.CERT_NONE}
    )
