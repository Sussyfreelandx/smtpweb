"""
Celery worker entrypoint.

Render's worker process is configured to run:
  celery -A celery_worker.celery worker ...

This module wires the Celery instance (from celery_app.py) to the Flask app
context, then imports tasks so they are registered using the ContextTask.
"""

import os

try:
    import eventlet
    eventlet.monkey_patch()
except ImportError:
    print(
        "Warning: eventlet is not installed. Eventlet-based Celery concurrency pools will not be available "
        "(install with: pip install eventlet).",
        flush=True,
    )
except Exception as e:
    # Best-effort: keep the worker running, but surface the problem in logs.
    print(f"Warning: eventlet.monkey_patch() failed; eventlet concurrency may not work correctly: {e}", flush=True)

from celery_app import celery

# create_app/make_celery live in app/__init__.py
from app import create_app, make_celery  # noqa: E402


flask_app = create_app(os.environ.get("APP_SETTINGS"))
make_celery(flask_app, celery)

# Import tasks only after ContextTask is installed.
import app.tasks  # noqa: F401, E402
