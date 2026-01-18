from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_login import LoginManager
from celery import Celery
from .config import Config

# Initialize extensions without an app
db = SQLAlchemy()
migrate = Migrate()
login = LoginManager()
login.login_view = 'main.login' # The function name of your login route

celery = Celery(__name__, broker=Config.CELERY_BROKER_URL)

def create_app(config_class=Config):
    """The application factory."""
    app = Flask(__name__)
    app.config.from_object(config_class)

    # Initialize extensions with the app
    db.init_app(app)
    migrate.init_app(app, db)
    login.init_app(app)
    celery.conf.update(app.config)

    # --- SIMPLIFIED BLUEPRINT REGISTRATION ---
    # Import the blueprint that was created in routes.py
    from app.main.routes import bp as main_blueprint
    app.register_blueprint(main_blueprint)

    return app
