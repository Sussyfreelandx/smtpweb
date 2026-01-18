import os
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_login import LoginManager
from celery import Celery
from .config import Config # This line imports the file you just created

# Initialize extensions, but don't configure them yet
db = SQLAlchemy()
migrate = Migrate()
login = LoginManager()
login.login_view = 'main.login' # The route to redirect to for login

# Celery setup
celery = Celery(__name__, broker=Config.CELERY_BROKER_URL)

def create_app(config_class=Config):
    """
    Application factory function.
    """
    app = Flask(__name__)
    app.config.from_object(config_class)

    # Initialize extensions with the app
    db.init_app(app)
    migrate.init_app(app, db)
    login.init_app(app)
    
    # Update Celery config with app config
    celery.conf.update(app.config)

    # Register blueprints
    from app.main import bp as main_bp
    app.register_blueprint(main_bp)

    # Import models so that they are registered with SQLAlchemy
    from app import models

    return app
