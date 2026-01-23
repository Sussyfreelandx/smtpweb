"""
Application factory and extensions initialization.

This module initializes:
- Flask app (create_app)
- SQLAlchemy db
- LoginManager
- SocketIO with optional message_queue support (Redis recommended)
- Celery factory helper (make_celery)

Place this file at app/__init__.py so other modules may import:
    from app import create_app, db, socketio, celery, login
"""
import os
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_migrate import Migrate
from flask_socketio import SocketIO
from celery import Celery
from flask_wtf import CSRFProtect

# Expose extension objects for import across the project
db = SQLAlchemy()
migrate = Migrate()
login = LoginManager()
csrf = CSRFProtect()

# Initialize SocketIO at module level to prevent import errors (e.g. "NoneType has no attribute 'on'")
# It will be configured in create_app via init_app
socketio = SocketIO()

# Celery instance placeholder
# Note: For Celery to work with the factory pattern, imports often need to happen
# after create_app is called, or use the app.celery instance.
celery = None


def make_celery(app: Flask):
    """
    Create and configure a Celery object tied to the Flask application context.
    Returns a Celery instance that can be used to create tasks.
    """
    broker = app.config.get('CELERY_BROKER_URL') or os.environ.get('CELERY_BROKER_URL')
    backend = app.config.get('CELERY_RESULT_BACKEND') or os.environ.get('CELERY_RESULT_BACKEND')
    
    # Ensure broker is provided
    if not broker:
        # Fallback to a default if not set, or let it fail gracefully depending on usage
        broker = 'redis://localhost:6379/0'

    celery_obj = Celery(
        app.import_name,
        broker=broker,
        backend=backend or broker
    )
    celery_obj.conf.update(app.config)

    class ContextTask(celery_obj.Task):
        def __call__(self, *args, **kwargs):
            with app.app_context():
                return self.run(*args, **kwargs)

    celery_obj.Task = ContextTask
    return celery_obj


def create_app(config_object=None):
    """
    Flask application factory.

    config_object may be:
      - None (will use environment variables and default settings)
      - A config object, class, or dictionary
    """
    # We only globalize celery because we replace the instance completely.
    # socketio is initialized at module level and updated via init_app.
    global celery

    app = Flask(__name__, static_folder=None)

    # Basic sensible defaults - override via environment or config object
    app.config.setdefault('SECRET_KEY', os.environ.get('SECRET_KEY', 'dev-secret'))
    app.config.setdefault('SQLALCHEMY_DATABASE_URI', os.environ.get('DATABASE_URL', 'sqlite:///app.db'))
    app.config.setdefault('SQLALCHEMY_TRACK_MODIFICATIONS', False)
    
    # Celery Defaults
    app.config.setdefault('CELERY_BROKER_URL', os.environ.get('CELERY_BROKER_URL', 'redis://localhost:6379/1'))
    # Ensure BROKER_URL is set before trying to access it for RESULT_BACKEND default
    app.config.setdefault('CELERY_RESULT_BACKEND', os.environ.get('CELERY_RESULT_BACKEND', app.config.get('CELERY_BROKER_URL')))
    
    # Socket.IO message queue (for scaling across processes/workers)
    app.config.setdefault('SOCKETIO_MESSAGE_QUEUE', os.environ.get('SOCKETIO_MESSAGE_QUEUE', os.environ.get('REDIS_URL', 'redis://localhost:6379/0')))
    
    # Other defaults
    app.config.setdefault('WTF_CSRF_TIME_LIMIT', None)

    # Allow a config object to override settings
    if config_object:
        if isinstance(config_object, dict):
            app.config.from_mapping(config_object)
        else:
            # Handles strings (path to file) or objects/classes
            app.config.from_object(config_object)

    # Initialize extensions
    db.init_app(app)
    migrate.init_app(app, db)
    login.init_app(app)
    csrf.init_app(app)

    # Initialize SocketIO with message queue support via init_app
    message_queue = app.config.get('SOCKETIO_MESSAGE_QUEUE')
    socketio.init_app(
        app,
        cors_allowed_origins="*",
        message_queue=message_queue,
        logger=app.debug,
        engineio_logger=app.debug
    )

    # Initialize Celery instance for import elsewhere
    celery = make_celery(app)

    # Register blueprints
    # Import inside function to avoid circular imports
    from app.main import bp as main_bp
    from app.api import bp as api_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(api_bp, url_prefix='/api')

    # Example: configure login settings
    login.login_view = 'main.login'
    login.login_message = "Please sign in to access this page."

    # Attach extensions to app for external imports
    app.socketio = socketio
    app.celery = celery
    app.db = db

    # Import socket event handlers after socketio is created to register events
    try:
        import app.events  # noqa: F401
    except ImportError:
        # Log this but don't crash if events file doesn't exist yet
        app.logger.info("No app.events module found, skipping socket event registration.")
    except Exception:
        app.logger.exception("Failed to import socket events (app.events) during create_app.")

    return app
