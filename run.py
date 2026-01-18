from app import create_app, db
from app.models import User, Campaign, Recipient, Suppression

app = create_app()

@app.shell_context_processor
def make_shell_context():
    return {
        'db': db,
        'User': User,
        'Campaign': Campaign,
        'Recipient': Recipient,
        'Suppression': Suppression
    }

if __name__ == '__main__':
    app.run(debug=True)
