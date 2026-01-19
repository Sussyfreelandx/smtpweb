import os
import sys

# Add the project root to Python path
sys. path.insert(0, os. path.dirname(os.path. abspath(__file__)))

from config import Config
from app import create_app, db
from app. models import User, Campaign, Recipient, SMTPServer, Suppression

app = create_app(Config)

@app.shell_context_processor
def make_shell_context():
    return {
        'db':  db,
        'User': User,
        'Campaign': Campaign,
        'Recipient': Recipient,
        'SMTPServer':  SMTPServer,
        'Suppression': Suppression
    }

if __name__ == '__main__':
    app.run(debug=True)
