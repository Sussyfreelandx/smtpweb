"""
Application factory and extension initialization.

This module exposes commonly-used extensions at package level so other modules
can import: from app import db, migrate, login, socketio, cache, celery, bcrypt, limiter
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
from celery import Celery

# Create extension instances (unbound).
db = SQLAlchemy()
migrate = Migrate()
login = LoginManager()

# Use eventlet for best Socket.IO performance in production
# The 'launcher.sh' uses eventlet worker, so we should match that here.
socketio = SocketIO(cors_allowed_origins="*", async_mode='eventlet')

cache = Cache()
limiter = Limiter(key_func=get_remote_address, default_limits=["200 per minute"])
celery = Celery(__name__)

# Default login settings
login.login_view = "main.login"
login.login_message_category = "info"

# Optional Bcrypt
try:
    from flask_bcrypt import Bcrypt
    bcrypt = Bcrypt()
    _BCRYPT_AVAILABLE = True
except Exception:
    bcrypt = None
    _BCRYPT_AVAILABLE = False


def make_celery(app: Flask, celery_obj: Celery):
    """
    Configure a Celery object to work with the Flask app context.
    """
    # Load configuration from app config
    celery_obj.conf.update(app.config.get("CELERY", {}))
    
    # Ensure broker/backend are set if provided explicitly in config keys
    broker = app.config.get("CELERY_BROKER_URL")
    backend = app.config.get("CELERY_RESULT_BACKEND")

    if broker:
        celery_obj.conf.broker_url = broker
    if backend:
        celery_obj.conf.result_backend = backend

    class ContextTask(celery_obj.Task):
        def __call__(self, *args, **kwargs):
            with app.app_context():
                return self.run(*args, **kwargs)

    celery_obj.Task = ContextTask
    return celery_obj


def create_app(config_object=None):
    """
    Application factory.
    """
    app = Flask(__name__, instance_relative_config=False)

    # Load configuration
    config_path = config_object or os.environ.get("APP_SETTINGS")
    if config_path:
        try:
            if isinstance(config_path, str) and os.path.exists(config_path):
                app.config.from_pyfile(config_path)
            else:
                app.config.from_object(config_path)
        except Exception:
            pass

    # Defaults
    app.config.setdefault("SQLALCHEMY_DATABASE_URI", os.environ.get("DATABASE_URL", "sqlite:///data.db"))
    app.config.setdefault("SQLALCHEMY_TRACK_MODIFICATIONS", False)
    app.config.setdefault("SECRET_KEY", os.environ.get("SECRET_KEY", "dev-secret-key"))
    
    # Redis defaults
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    app.config.setdefault("CELERY_BROKER_URL", redis_url)
    app.config.setdefault("CELERY_RESULT_BACKEND", redis_url)
    
    # Cache defaults - prefer Redis if available
    if os.environ.get("REDIS_URL"):
        app.config.setdefault("CACHE_TYPE", "RedisCache")
        app.config.setdefault("CACHE_REDIS_URL", redis_url)
    else:
        app.config.setdefault("CACHE_TYPE", "SimpleCache")
        
    app.config.setdefault("WTF_CSRF_ENABLED", True)

    # Initialize extensions
    db.init_app(app)
    migrate.init_app(app, db)
    login.init_app(app)
    socketio.init_app(app, cors_allowed_origins=app.config.get("CORS_ALLOWED_ORIGINS", "*"), async_mode='eventlet')
    cache.init_app(app)
    limiter.init_app(app)

    if _BCRYPT_AVAILABLE and bcrypt:
        try:
            bcrypt.init_app(app)
        except Exception:
            pass

    # Configure Celery
    make_celery(app, celery)

    # Register blueprints
    try:
        from app.main import bp as main_bp
        app.register_blueprint(main_bp)
    except Exception:
        pass

    try:
        from app.api import bp as api_bp
        app.register_blueprint(api_bp, url_prefix="/api")
    except Exception:
        pass

    try:
        from app.tracking import bp as tracking_bp
        app.register_blueprint(tracking_bp)
    except Exception:
        pass

    @app.route("/healthz")
    def _health():
        return {"status": "ok", "version": "1.0.0"}

    return app
