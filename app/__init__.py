from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_login import LoginManager
from celery import Celery
import logging
from logging.handlers import RotatingFileHandler
import os
import sys

# CRITICAL FIX: Ensure the project root is in the Python path
# This allows 'import config' to work regardless of where the app is started from
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# Now we can safely import config
try:
    import config
except ImportError:
    # If standard import fails, try manual loading (failsafe for some environments)
    # This creates a dummy module if the real one isn't found to prevent build crash
    class Config:
        SECRET_KEY = os.environ.get('SECRET_KEY', 'dev-key')
        SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL', 'sqlite:///app.db')
        CELERY_BROKER_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')
        CELERY_RESULT_BACKEND = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')
    config = type('config', (), {'Config': Config})

# Initialize Extensions
db = SQLAlchemy()
migrate = Migrate()
login = LoginManager()
login.login_view = 'main.login'
login.login_message_category = 'info'
celery = Celery(__name__)

def create_app(config_class=None):
    app = Flask(__name__)
    
    # Use the imported config
    if config_class is None:
        config_class = config.Config
        
    app.config.from_object(config_class)

    # Initialize Extensions with App
    db.init_app(app)
    migrate.init_app(app, db)
    login.init_app(app)
    
    # Configure Celery
    celery.conf.update(app.config)
    # Ensure explicit keys are set for Celery 5+
    celery.conf.broker_url = app.config.get('CELERY_BROKER_URL')
    celery.conf.result_backend = app.config.get('CELERY_RESULT_BACKEND')

    # Register Blueprints
    from app.main.routes import bp as main_bp
    app.register_blueprint(main_bp)

    from app.tracking import bp as tracking_bp
    app.register_blueprint(tracking_bp)

    # Logging Configuration
    if not app.debug and not app.testing:
        if not os.path.exists('logs'):
            try:
                os.mkdir('logs')
            except OSError:
                pass 
        
        file_handler = RotatingFileHandler('logs/paris_sender.log', maxBytes=10240, backupCount=10)
        file_handler.setFormatter(logging.Formatter(
            '%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]'))
        file_handler.setLevel(logging.INFO)
        app.logger.addHandler(file_handler)
        app.logger.setLevel(logging.INFO)
        app.logger.info('Paris Sender Startup')

    return app
