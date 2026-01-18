import os
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_login import LoginManager
from celery import Celery
from app.config import Config # This will now work because app/config.py exists

# Initialize extensions
db = SQLAlchemy()
migrate = Migrate()
login = LoginManager()
login.login_view = 'main.login'
login.login_message = "Please log in to access this page."

# Initialize Celery
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
    
    # Make sure Celery config is updated from the app's config
    celery.conf.update(app.config)

    # Register the main blueprint
    from app.main import bp as main_bp
    app.register_blueprint(main_bp)

    # This is crucial: import models AFTER db is initialized
    with app.app_context():
        from . import models

    return app
