import os
from app import create_app, db, socketio
from app.models import User, Campaign, Recipient, SMTPServer, Suppression

# REMOVED: eventlet.monkey_patch() and all proxy logic.
# This file should only be for local Flask development.
# Server-level patching and configuration belong in wsgi.py and gunicorn.conf.py.

app = create_app()

@app.shell_context_processor
def make_shell_context():
    return {
        'db': db,
        'User': User,
        'Campaign': Campaign,
        'Recipient': Recipient,
        'SMTPServer': SMTPServer,
        'Suppression': Suppression
    }


if __name__ == '__main__':
    # Use socketio.run for local development which correctly handles eventlet.
    # The 'eventlet' worker must be installed: pip install eventlet
    socketio.run(app, debug=True, host='0.0.0.0', port=5000)
