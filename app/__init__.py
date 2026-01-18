import logging
from logging.handlers import RotatingFileHandler
import os
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_login import LoginManager
from celery import Celery
from config import Config

# Initialize extensions
db = SQLAlchemy()
migrate = Migrate()
login = LoginManager()
login.login_view = 'main.login' # Tells Flask-Login which page to redirect to for login
login.login_message = 'Please log in to access this page.'

# Initialize Celery
celery = Celery(__name__, broker=Config.CELERY_BROKER_URL)

def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    # --- Initialize Flask extensions ---
    db.init_app(app)
    migrate.init_app(app, db)
    login.init_app(app)

    # Update Celery config from Flask config
    celery.conf.update(app.config)

    # --- Register Blueprints ---
    from app.main.routes import bp as main_bp
    app.register_blueprint(main_bp)
    
    # A blueprint for core logic that doesn't need a URL prefix
    from app.core_logic import bp as core_logic_bp
    app.register_blueprint(core_logic_bp)


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
