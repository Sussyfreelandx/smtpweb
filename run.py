from app import create_app, db
from app.models import User, Campaign, Recipient

# The create_app function is the application factory.
app = create_app()

@app.shell_context_processor
def make_shell_context():
    """
    Makes a dictionary of items available in the 'flask shell' context.
    This is useful for debugging and database management.
    """
    return {'db': db, 'User': User, 'Campaign': Campaign, 'Recipient': Recipient}

if __name__ == '__main__':
    # This block is for local development only.
    # gunicorn will run the app in production using wsgi.py
    app.run(debug=True)
