# The eventlet.monkey_patch() is now handled by the launcher.sh script.
# This ensures it's applied correctly before the app and workers are loaded.

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
    # This block is for local development and remains unchanged.
    socketio.run(app, debug=False, host='0.0.0.0', port=5000)
