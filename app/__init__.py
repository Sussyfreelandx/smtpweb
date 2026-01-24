"""
Application factory and extension initialization.

This module exposes commonly-used extensions at package level so other modules
can import: from app import db, migrate, login, socketio, cache, celery, bcrypt, limiter, csrf
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

# Create extension instances (unbound). They will be initialized with create_app.
db = SQLAlchemy()
migrate = Migrate()
login = LoginManager()
socketio = SocketIO(cors_allowed_origins="*")
cache = Cache()  # default cache instance; configure via app.config
csrf = CSRFProtect()

# Bcrypt: optional import. If flask-bcrypt is not installed we provide a safe fallback
try:
    from flask_bcrypt import Bcrypt  # type: ignore
    bcrypt = Bcrypt()
    _BCRYPT_AVAILABLE = True
except Exception:
    bcrypt = None
    _BCRYPT_AVAILABLE = False

# Simple rate limiter
limiter = Limiter(key_func=get_remote_address, default_limits=["200 per minute"])

# Create a Celery instance placeholder. We'll configure it for app context inside create_app.
celery = Celery(__name__)

# Default login settings
login.login_view = "main.login"
login.login_message_category = "info"


def make_celery(app: Flask, celery_obj: Celery):
    """
    Configure a Celery object to work with the Flask app context.
    This updates celery.conf with app.config and creates a base Task that
    wraps task execution in app.app_context().
    """
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


def create_app(config_object=None):
    """
    Application factory.

    Pass in a config object or set environment variables for configuration.
    Returns a fully configured Flask app and also configures the shared 'celery' object.
    """
    app = Flask(__name__, instance_relative_config=False)

    # 1. CRITICAL: Set SECRET_KEY immediately to prevent session/flash errors
    # We set this BEFORE loading other configs to guarantee a fallback exists.
    app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key-change-this-in-prod")
    app.config["SECRET_KEY"] = app.secret_key

    # 2. Load configuration
    config_path = config_object or os.environ.get("APP_SETTINGS")
    if config_path:
        try:
            if isinstance(config_path, str) and os.path.exists(config_path):
                app.config.from_pyfile(config_path)
            else:
                app.config.from_object(config_path)
        except Exception:
            pass

    # 3. Set Defaults (if not overridden by config_path)
    app.config.setdefault("SQLALCHEMY_DATABASE_URI", os.environ.get("DATABASE_URL", "sqlite:///data.db"))
    app.config.setdefault("SQLALCHEMY_TRACK_MODIFICATIONS", False)
    app.config.setdefault("CELERY_BROKER_URL", os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/0"))
    app.config.setdefault("CELERY_RESULT_BACKEND", os.environ.get("CELERY_RESULT_BACKEND", "redis://localhost:6379/0"))
    app.config.setdefault("CACHE_TYPE", os.environ.get("CACHE_TYPE", "simple"))
    app.config.setdefault("WTF_CSRF_ENABLED", True)

    # 4. Initialize extensions
    db.init_app(app)
    migrate.init_app(app, db)
    login.init_app(app)
    socketio.init_app(app, cors_allowed_origins=app.config.get("CORS_ALLOWED_ORIGINS", "*"))
    cache.init_app(app)
    limiter.init_app(app)
    csrf.init_app(app)  # FIX: Initializes CSRF protection for forms

    # Initialize bcrypt if available
    if _BCRYPT_AVAILABLE and bcrypt:
        try:
            bcrypt.init_app(app)
        except Exception:
            pass

    # Configure celery
    make_celery(app, celery)

    # 5. Register Blueprints
    try:
        from app.main import bp as main_bp
        app.register_blueprint(main_bp)
    except Exception:
        pass

    try:
        from app.api import bp as api_bp
        # Exempt API routes from CSRF protection (they use API tokens)
        csrf.exempt(api_bp)
        app.register_blueprint(api_bp, url_prefix="/api")
    except Exception:
        pass

    try:
        from app.tracking import bp as tracking_bp
        # Exempt tracking routes from CSRF (they are public pixels/links)
        csrf.exempt(tracking_bp)
        app.register_blueprint(tracking_bp)
    except Exception:
        pass

    # Health check route
    @app.route("/healthz")
    def _health():
        return {"status": "ok", "version": "1.0.0"}

    return app
