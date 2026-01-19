from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_login import LoginManager
from celery import Celery
from config import Config
import logging
from logging.handlers import RotatingFileHandler
import os

# Initialize Extensions
db = SQLAlchemy()
migrate = Migrate()
login = LoginManager()
login.login_view = 'main.login'
login.login_message_category = 'info'
celery = Celery(__name__, broker=Config.CELERY_BROKER_URL)

def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    # Initialize Extensions with App
    db.init_app(app)
    migrate.init_app(app, db)
    login.init_app(app)
    
    # Configure Celery
    celery.conf.update(app.config)

    # Register Blueprints
    # 1. Main Routes (Dashboard, Settings, Campaign creation)
    from app.main.routes import bp as main_bp
    app.register_blueprint(main_bp)

    # 2. Tracking Routes (Open/Click/Unsubscribe) - previously in a separate file
    from app.tracking import bp as tracking_bp
    app.register_blueprint(tracking_bp)

    # Logging Configuration to prevent silent 500 errors
    if not app.debug and not app.testing:
        if not os.path.exists('logs'):
            os.mkdir('logs')
        file_handler = RotatingFileHandler('logs/paris_sender.log', maxBytes=10240, backupCount=10)
        file_handler.setFormatter(logging.Formatter(
            '%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]'))
        file_handler.setLevel(logging.INFO)
        app.logger.addHandler(file_handler)
        app.logger.setLevel(logging.INFO)
        app.logger.info('Paris Sender Startup')

    return app
