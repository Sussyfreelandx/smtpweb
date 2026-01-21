import os
from app import create_app, db, socketio, celery
from app.models import User, Campaign, Recipient, SMTPServer, Suppression

# REMOVED: Global Smart Proxy Patch.
# The proxy logic is now securely handled inside app/core_logic/smtp_handler.py
# ensuring it only applies to SMTP connections and doesn't crash Redis/Celery.

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
