from app import create_app, db
from app.models import User, Campaign, Recipient, SMTPServer, Suppression, GlobalSettings

# Create the application instance
app = create_app()


@app.shell_context_processor
def make_shell_context():
    """Makes objects available in flask shell."""
    return {
        'db': db,
        'User': User,
        'Campaign': Campaign,
        'Recipient': Recipient,
        'SMTPServer': SMTPServer,
        'Suppression': Suppression,
        'GlobalSettings': GlobalSettings
    }


if __name__ == '__main__':
    app.run(debug=True)
