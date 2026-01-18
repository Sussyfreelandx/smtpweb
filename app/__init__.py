from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_login import LoginManager
from celery import Celery
from app.config import Config

# Initialize extensions without attaching them to an app yet
db = SQLAlchemy()
migrate = Migrate()
login = LoginManager()
login.login_view = 'main.login' # Point to the blueprint's login function
login.login_message = "Please log in to access this page."

celery = Celery(__name__, broker=Config.CELERY_BROKER_URL)

def create_app(config_class=Config):
    """The application factory."""
    app = Flask(__name__)
    app.config.from_object(config_class)

    # Now, initialize extensions with the app
    db.init_app(app)
    migrate.init_app(app, db)
    login.init_app(app)
    celery.conf.update(app.config)

    # --- THIS IS THE FIX ---
    # Import the blueprint from the routes file where it was created.
    from app.main.routes import bp as main_blueprint
    # Now register it. It already has all the routes attached.
    app.register_blueprint(main_blueprint)

    # It's good practice to import models within the app context
    with app.app_context():
        from . import models

    return app
