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
login.login_view = 'main.login'
login.login_message = 'Please log in to access this page.'
login.login_message_category = 'info'

# Initialize Celery
# The broker URL is set from the app config later
celery = Celery(__name__, broker=Config.CELERY_BROKER_URL)

def create_app(config_class=Config):
    """
    The application factory function.
    """
    app = Flask(__name__)
    app.config.from_object(config_class)

    # --- Initialize Flask extensions with the app ---
    db.init_app(app)
    migrate.init_app(app, db)
    login.init_app(app)

    # Update Celery config from the Flask app config
    celery.conf.update(
        broker_url=app.config['CELERY_BROKER_URL'],
        result_backend=app.config['CELERY_RESULT_BACKEND']
    )
    celery.conf.task_default_queue = 'default'

    # --- Register Blueprints ---
    from app.main import bp as main_bp
    app.register_blueprint(main_bp)

    # --- Configure Logging for Production ---
    if not app.debug and not app.testing:
        if not os.path.exists('logs'):
            os.mkdir('logs')
        # Log to a rotating file
        file_handler = RotatingFileHandler('logs/paris_sender_web.log', maxBytes=10240, backupCount=10)
        file_handler.setFormatter(logging.Formatter(
            '%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]'))
        file_handler.setLevel(logging.INFO)
        app.logger.addHandler(file_handler)

        app.logger.setLevel(logging.INFO)
        app.logger.info('Paris Sender Web Starting Up')

    return app

from app import models
