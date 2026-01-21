# CRITICAL: Eventlet monkey patching must happen BEFORE any other imports
import eventlet
eventlet.monkey_patch()

import os
from app import create_app, db, socketio, celery
from app.models import User, Campaign, Recipient, SMTPServer, Suppression

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
    socketio.run(app, debug=False, host='0.0.0.0', port=5000)
