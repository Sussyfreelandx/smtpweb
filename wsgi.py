import os
from app import create_app, db, socketio, celery
from app.models import User, Campaign, Recipient, SMTPServer, Suppression

# ==========================================
#   WSGI APPLICATION ENTRY POINT
# ==========================================
# Note:  Eventlet has been removed to prevent blocking mainloop errors. 
# Flask-SocketIO will use simple-websocket or threading fallback. 

app = create_app()

@app.shell_context_processor
def make_shell_context():
    return {
        'db': db,
        'User': User,
        'Campaign': Campaign,
        'Recipient': Recipient,
        'SMTPServer': SMTPServer,
        'Suppression':  Suppression
    }

if __name__ == '__main__': 
    socketio.run(app, debug=False, host='0.0.0.0', port=5000)

remove any whitespace and replace code that will cause build error. don't touch the structure
