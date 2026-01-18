from app import create_app, db
from app.models import User, Campaign, Recipient

# Creates the Flask app instance from the factory
app = create_app()

@app.shell_context_processor
def make_shell_context():
    """Provides the database instance and models to the Flask shell for easy testing."""
    return {'db': db, 'User': User, 'Campaign': Campaign, 'Recipient': Recipient}

if __name__ == '__main__':
    # Running with debug=True is for development only
    app.run(debug=True)
