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
login.login_view = 'main.routes.login' # Point to the login function
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

    # Import and register the blueprint AFTER the app is configured
    from app.main.routes import bp as main_blueprint
    app.register_blueprint(main_blueprint)

    return app
