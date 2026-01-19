from app import create_app, db
from app.models import User, Campaign, Recipient, SMTPServer, Suppression

app = create_app()

if __name__ == "__main__":
    app.run()
