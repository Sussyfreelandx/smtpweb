# This is the entry point for the Gunicorn web server.
from app import create_app, celery

app = create_app()
