"""
Application factory and extension initialization.
"""

import os
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_login import LoginManager
from flask_socketio import SocketIO
from flask_caching import Cache
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf.csrf import CSRFProtect
from celery import Celery

# Create extension instances
db = SQLAlchemy()
migrate = Migrate()
login = LoginManager()
csrf = CSRFProtect()
# async_mode='eventlet' is critical for Render
socketio = SocketIO(cors_allowed_origins="*", async_mode='eventlet')
cache = Cache()
limiter = Limiter(key_func=get_remote_address) # Default limits set in config
celery = Celery(__name__)

# Bcrypt setup
try:
    from flask_bcrypt import Bcrypt
    bcrypt = Bcrypt()
    _BCRYPT_AVAILABLE = True
except ImportError:
    bcrypt = None
    _BCRYPT_AVAILABLE = False

login.login_view = "main.login"
login.login_message_category = "info"

def make_celery(app: Flask, celery_obj: Celery):
    """Configure Celery to use Flask app context."""
    celery_obj.conf.broker_url = app.config['CELERY_BROKER_URL']
    celery_obj.conf.result_backend = app.config['CELERY_RESULT_BACKEND']
    celery_obj.conf.update(app.config.get("CELERY", {}))

    class ContextTask(celery_obj.Task):
        def __call__(self, *args, **kwargs):
            with app.app_context():
                return self.run(*args, **kwargs)

    celery_obj.Task = ContextTask
    return celery_obj

def create_app(config_object=None):
    """Application factory."""
    app = Flask(__name__, instance_relative_config=False)

    # 1. Determine config source (env var or direct object)
    config_source = os.environ.get("APP_SETTINGS", 'config.default')

    # Load configuration from config.py
    # This centralizes config management
    app.config.from_object(config_source)

    # Allow overriding with an explicit config object if provided
    if config_object:
        app.config.from_object(config_object)

    # SECRET_KEY is crucial, ensure it's set
    if not app.config.get("SECRET_KEY"):
        raise ValueError("SECRET_KEY is not set! Please set it in your config or environment.")

    # 2. Initialize extensions with app
    db.init_app(app)
    migrate.init_app(app, db)
    login.init_app(app)
    csrf.init_app(app)
    
    # SocketIO init uses the message queue from the config
    socketio.init_app(app, message_queue=app.config.get('SOCKETIO_MESSAGE_QUEUE'))
    
    cache.init_app(app)
    limiter.init_app(app)

    if _BCRYPT_AVAILABLE and bcrypt:
        bcrypt.init_app(app)

    # Configure Celery
    make_celery(app, celery)

    # 3. Register Blueprints
    with app.app_context():
        from app.main import bp as main_bp
        app.register_blueprint(main_bp)

        from app.api import bp as api_bp
        app.register_blueprint(api_bp, url_prefix="/api")

        from app.tracking import bp as tracking_bp
        app.register_blueprint(tracking_bp)

    @app.route("/healthz")
    def _health():
        return {"status": "ok", "version": "1.0.0"}

    return app
