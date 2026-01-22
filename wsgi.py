"""
WSGI config for the application.
It exposes the WSGI callable as a module-level variable named ``application``.
"""

# IMPORTANT: Proxy patching logic has been removed from this file.
# The new, safer proxy implementation is now self-contained within
# app/core_logic/smtp_handler.py and does not require global socket patching.

from app import create_app, db
from app.models import User, Campaign, Recipient, SMTPServer, Suppression

# Create the Flask application instance
app = create_app()

@app.shell_context_processor
def make_shell_context():
    """
    Makes additional variables available in the Flask shell context
    for easier debugging.
    """
    return {
        'db': db,
        'User': User,
        'Campaign': Campaign,
        'Recipient': Recipient,
        'SMTPServer': SMTPServer,
        'Suppression': Suppression
    }

# This is the WSGI callable that Gunicorn will use.
application = app
