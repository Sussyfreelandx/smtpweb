from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_login import LoginManager
from celery import Celery
import logging
from logging.handlers import RotatingFileHandler
import os
import sys

# Add the root directory to sys.path to ensure config.py can be found
# This fixes "ModuleNotFoundError: No module named 'config'"
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

try:
    from config import Config
except ImportError:
    # Fallback if running directly from within app folder (rare but possible)
    from ..config import Config

# Initialize Extensions
db = SQLAlchemy()
migrate = Migrate()
login = LoginManager()
login.login_view = 'main.login'
login.login_message_category = 'info'
celery = Celery(__name__)

def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    # Initialize Extensions with App
    db.init_app(app)
    migrate.init_app(app, db)
    login.init_app(app)
    
    # Configure Celery
    celery.conf.update(app.config)
    celery.conf.broker_url = app.config['CELERY_BROKER_URL']
    celery.conf.result_backend = app.config['CELERY_RESULT_BACKEND']

    # Register Blueprints
    # 1. Main Routes (Dashboard, Settings, Campaign creation)
    from app.main.routes import bp as main_bp
    app.register_blueprint(main_bp)

    # 2. Tracking Routes (Open/Click/Unsubscribe)
    from app.tracking import bp as tracking_bp
    app.register_blueprint(tracking_bp)

    # Logging Configuration
    if not app.debug and not app.testing:
        if not os.path.exists('logs'):
            try:
                os.mkdir('logs')
            except OSError:
                pass # Use stream handler if file creation fails (e.g. read-only file systems)
        
        file_handler = RotatingFileHandler('logs/paris_sender.log', maxBytes=10240, backupCount=10)
        file_handler.setFormatter(logging.Formatter(
            '%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]'))
        file_handler.setLevel(logging.INFO)
        app.logger.addHandler(file_handler)
        
        app.logger.setLevel(logging.INFO)
        app.logger.info('Paris Sender Startup')

    return app
