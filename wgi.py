# This file is used by Gunicorn to run the application.
from app import create_app, celery

app = create_app()
