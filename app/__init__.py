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
from config import config, ProductionConfig # <-- Import config objects

# Create extension instances
db = SQLAlchemy()
migrate = Migrate()
login_manager = LoginManager()
csrf = CSRFProtect()
socketio = SocketIO(cors_allowed_origins="*", async_mode='eventlet')
cache = Cache()
limiter = Limiter(key_func=get_remote_address, default_limits=["200 per minute"])
celery = Celery(__name__)

# Bcrypt setup
try:
    from flask_bcrypt import Bcrypt
    bcrypt = Bcrypt()
    _BCRYPT_AVAILABLE = True
except ImportError:
    bcrypt = None
    _BCRYPT_AVAILABLE = False

login_manager.login_view = "main.login"
login_manager.login_message_category = "info"

def make_celery(app: Flask, celery_obj: Celery):
    """Configure Celery to use Flask app context."""
    broker = app.config.get("CELERY_BROKER_URL")
    backend = app.config.get("CELERY_RESULT_BACKEND")

    if broker:
        celery_obj.conf.broker_url = broker
    if backend:
        celery_obj.conf.result_backend = backend

    celery_obj.conf.update(app.config.get("CELERY", {}))

    class ContextTask(celery_obj.Task):
        def __call__(self, *args, **kwargs):
            with app.app_context():
                return self.run(*args, **kwargs)

    celery_obj.Task = ContextTask
    return celery_obj

def create_app(config_name=None):
    """Application factory."""
    app = Flask(__name__, instance_relative_config=False)

    # Determine which configuration to use
    if config_name is None:
        config_name = os.environ.get("APP_SETTINGS", "production").replace("config.", "")
    
    # Use ProductionConfig as a fallback default
    config_obj = config.get(config_name, ProductionConfig)
    app.config.from_object(config_obj)

    # Set secret key from environment or config
    if "SECRET_KEY" not in app.config:
        app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-key-change-this-in-prod")

    # Set other required configs if not present
    app.config.setdefault("SQLALCHEMY_DATABASE_URI", os.environ.get("DATABASE_URL", "sqlite:///data.db"))
    app.config.setdefault("SQLALCHEMY_TRACK_MODIFICATIONS", False)
    app.config.setdefault("WTF_CSRF_ENABLED", True)
    
    # Use the REDIS_URL from config for other services
    redis_url = app.config.get('REDIS_URL')
    app.config.setdefault("CELERY_BROKER_URL", redis_url)
    app.config.setdefault("CELERY_RESULT_BACKEND", redis_url)
    app.config.setdefault("CACHE_TYPE", "RedisCache" if redis_url else "SimpleCache")
    app.config.setdefault("CACHE_REDIS_URL", redis_url)

    # Initialize extensions with app
    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    csrf.init_app(app)
    
    # Initialize SocketIO with message queue
    socketio.init_app(app, message_queue=app.config.get('SOCKETIO_MESSAGE_QUEUE'))
    
    cache.init_app(app)
    limiter.init_app(app)

    if _BCRYPT_AVAILABLE and bcrypt:
        bcrypt.init_app(app)

    # Configure Celery
    make_celery(app, celery)

    # Register Blueprints
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
