from app import create_app, db, celery
from app.models import User, Campaign, Recipient

app = create_app()

@app.shell_context_processor
def make_shell_context():
    return {'db': db, 'User': User, 'Campaign': Campaign, 'Recipient': Recipient, 'celery': celery}
