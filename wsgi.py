from app import create_app, db
from app.models import User, Campaign, Recipient, SMTPServer, Suppression

# The application factory creates the app instance
app = create_app()

@app.shell_context_processor
def make_shell_context():
    """
    Makes a dictionary of items available in the 'flask shell' context.
    This is useful for debugging.
    """
    return {
        'db': db, 'User': User, 'Campaign': Campaign, 'Recipient': Recipient,
        'SMTPServer': SMTPServer, 'Suppression': Suppression
    }
