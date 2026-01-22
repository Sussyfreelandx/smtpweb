# CRITICAL: Eventlet monkey patching MUST be the absolute first thing.
# This ensures it only runs in the web server process, not the Celery worker.
import eventlet
eventlet.monkey_patch()

# Now, import everything else
from app import create_app, db, socketio
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
