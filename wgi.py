from app import create_app, db
from app.models import User, Campaign, Recipient

app = create_app()
celery = app.extensions["celery"]

@app.shell_context_processor
def make_shell_context():
    """Provides a default context for the `flask shell` command."""
    return {'db': db, 'User': User, 'Campaign': Campaign, 'Recipient': Recipient}