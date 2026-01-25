import os
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_socketio import SocketIO
from flask_bcrypt import Bcrypt
from flask_migrate import Migrate
from flask_caching import Cache
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from config import config

# --- Initialize Extensions ---
db = SQLAlchemy()
login_manager = LoginManager()
login_manager.login_view = 'main.login'
socketio = SocketIO()
bcrypt = Bcrypt()
migrate = Migrate()
cache = Cache()
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["200 per day", "50 per hour"]
)

# This will be populated by the factory
celery = None

# --- Application Factory ---
def create_app(config_name=None):
    """
    Create and configure the Flask application.
    This is the Application Factory pattern.
    """
    if config_name is None:
        config_name = os.environ.get('FLASK_CONFIG', 'default')

    app = Flask(__name__)
    app.config.from_object(config[config_name])

    # --- Initialize Extensions with App ---
    db.init_app(app)
    login_manager.init_app(app)
    socketio.init_app(app, message_queue=app.config['SOCKETIO_MESSAGE_QUEUE'])
    bcrypt.init_app(app)
    migrate.init_app(app, db)
    cache.init_app(app)
    limiter.init_app(app)

    # Initialize Celery
    # We do this import here to avoid circular dependencies
    from celery_app import celery as celery_instance
    global celery
    celery = celery_instance
    celery.conf.update(app.config)

    # --- Register Blueprints ---
    # Main application routes
    from .main import bp as main_bp
    app.register_blueprint(main_bp)

    # API routes
    from .api import bp as api_bp
    app.register_blueprint(api_bp, url_prefix='/api/v1')
    
    # Tracking routes
    from .tracking import bp as tracking_bp
    app.register_blueprint(tracking_bp)

    # --- Import models here to ensure they are registered with SQLAlchemy ---
    with app.app_context():
        from . import models

    # --- Import SocketIO event handlers ---
    from . import events

    return app
