import os
import ssl

# ===========================================
# CELERY WORKER CONFIGURATION
# ===========================================
# This file is the entry point for the Celery worker. 
# It MUST read REDIS_URL from environment before creating the Celery app. 

# Get Redis URL - print for debugging
redis_url = os.environ.get('REDIS_URL')
print(f"🔍 REDIS_URL from environment: {redis_url[:30] if redis_url else 'NOT SET'}...")

if not redis_url:
    raise RuntimeError("REDIS_URL environment variable is not set!  Check Render configuration.")

# Clean up trailing slashes
if redis_url.endswith('/'):
    redis_url = redis_url.rstrip('/')

# Force SSL for Render Redis (rediss://)
if redis_url.startswith('redis://'):
    redis_url = redis_url.replace('redis://', 'rediss://', 1)
    print(f"🔒 Converted to SSL: {redis_url[:30]}...")

# NOW import Celery and create the app with the correct broker
from celery import Celery

celery = Celery(
    'paris_sender',
    broker=redis_url,
    backend=redis_url,
    include=['app.tasks']
)

# Configure Celery
celery.conf.update(
    task_serializer='json',
    accept_content=['json'],
    result_serializer='json',
    timezone='UTC',
    enable_utc=True,
    broker_connection_retry_on_startup=True,
    broker_use_ssl={'ssl_cert_reqs': ssl.CERT_NONE},
    redis_backend_use_ssl={'ssl_cert_reqs': ssl.CERT_NONE},
)

print(f"✅ Celery configured with broker: {redis_url[:30]}...")

# Import Flask app for context
from app import create_app, db

flask_app = create_app()

class ContextTask(celery.Task):
    def __call__(self, *args, **kwargs):
        with flask_app.app_context():
            return self.run(*args, **kwargs)

celery.Task = ContextTask

print("🚀 Celery worker ready!")
