import logging
from logging.handlers import RotatingFileHandler
import os
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_login import LoginManager
from celery import Celery
from config import Config

# Initialize extensions without an app
db = SQLAlchemy()
migrate = Migrate()
login = LoginManager()
login.login_view = 'main.login' # Tells Flask-Login which view to redirect to for login
login.login_message = 'Please log in to access this page.'

# Initialize Celery
# The broker and backend are configured in Config, so we don't set them here.
celery = Celery(__name__, broker=Config.CELERY_BROKER_URL, backend=Config.CELERY_RESULT_BACKEND)

def create_app(config_class=Config):
    """
    Application factory function.
    """
    app = Flask(__name__)
    app.config.from_object(config_class)

    # --- Initialize Flask extensions with the app ---
    db.init_app(app)
    migrate.init_app(app, db)
    login.init_app(app)

    # Update Celery config from the Flask app config
    celery.conf.update(app.config)

    # --- Register Blueprints ---
    from app.main.routes import bp as main_bp
    app.register_blueprint(main_bp)

    # --- Register Error Handlers & Other App-wide setup ---
    from app.main import errors

    # --- Configure Logging ---
    if not app.debug and not app.testing:
        if not os.path.exists('logs'):
            os.mkdir('logs')
        file_handler = RotatingFileHandler('logs/paris_sender_web.log', maxBytes=10240, backupCount=10)
        file_handler.setFormatter(logging.Formatter(
            '%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]'))
        file_handler.setLevel(logging.INFO)
        app.logger.addHandler(file_handler)

        app.logger.setLevel(logging.INFO)
        app.logger.info('Paris Sender Web startup')

    return app

# Import models at the end to avoid circular dependencies
from app import models
