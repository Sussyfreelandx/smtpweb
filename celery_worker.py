import os
import ssl
from app import create_app
from celery import Celery

# ===========================================
# UNIFIED CELERY APPLICATION DEFINITION
# ===========================================
# This file is the single source of truth for the Celery application.
# It is imported by both the worker and the web application.

def make_celery(app):
    """
    Creates and configures a Celery instance, linking it with the Flask app's context.
    """
    # 1. Get Redis URL from environment, required for both broker and backend.
    redis_url = os.environ.get('REDIS_URL')
    if not redis_url:
        raise RuntimeError("FATAL: REDIS_URL environment variable is not set.")

    # 2. Ensure Redis URL is in the 'rediss://' format for SSL on Render.
    if redis_url.startswith('redis://'):
        redis_url = redis_url.replace('redis://', 'rediss://', 1)

    # 3. Define Celery SSL options for Render's managed Redis.
    celery_ssl_options = {
        'ssl_cert_reqs': ssl.CERT_NONE
    }
    broker_use_ssl = celery_ssl_options if redis_url.startswith('rediss://') else None
    redis_backend_use_ssl = celery_ssl_options if redis_url.startswith('rediss://') else None
    
    # 4. Create the Celery instance.
    # The first argument 'app.name' makes tasks named like 'app.tasks.send_campaign_task'.
    celery_instance = Celery(
        app.import_name,
        backend=redis_url,
        broker=redis_url,
        include=['app.tasks']  # Auto-discover tasks from this module
    )

    # 5. Update Celery configuration.
    celery_instance.conf.update(
        broker_url=redis_url,
        result_backend=redis_url,
        task_serializer='json',
        accept_content=['json'],
        result_serializer='json',
        timezone='UTC',
        enable_utc=True,
        broker_connection_retry_on_startup=True,
        # Apply SSL options only if the scheme is 'rediss'.
        broker_use_ssl=broker_use_ssl,
        redis_backend_use_ssl=redis_backend_use_ssl
    )

    # 6. Define a custom Celery Task class that runs within the Flask app context.
    # This is CRITICAL for tasks to access the database (db.session), config, etc.
    class ContextTask(celery_instance.Task):
        def __call__(self, *args, **kwargs):
            with app.app_context():
                return self.run(*args, **kwargs)

    celery_instance.Task = ContextTask
    
    return celery_instance

# Create a temporary Flask app to initialize Celery
# This allows the worker to start without running the full web server.
flask_app = create_app()

# EXPORTED CELERY INSTANCE
# This is the object that both the worker and web app will use.
celery = make_celery(flask_app)
