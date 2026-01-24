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
from celery import Celery

# Create extension instances
db = SQLAlchemy()
migrate = Migrate()
login = LoginManager()
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

login.login_view = "main.login"
login.login_message_category = "info"

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

def create_app(config_object=None):
    """Application factory."""
    app = Flask(__name__, instance_relative_config=False)

    # Configuration loading
    config_path = config_object or os.environ.get("APP_SETTINGS")
    if config_path:
        try:
            if isinstance(config_path, str) and os.path.exists(config_path):
                app.config.from_pyfile(config_path)
            else:
                app.config.from_object(config_path)
        except Exception:
            pass

    # Default Configuration
    app.config.setdefault("SQLALCHEMY_DATABASE_URI", os.environ.get("DATABASE_URL", "sqlite:///data.db"))
    app.config.setdefault("SQLALCHEMY_TRACK_MODIFICATIONS", False)
    app.config.setdefault("SECRET_KEY", os.environ.get("SECRET_KEY", "dev-secret-key"))
    app.config.setdefault("CELERY_BROKER_URL", os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/0"))
    app.config.setdefault("CELERY_RESULT_BACKEND", os.environ.get("CELERY_RESULT_BACKEND", "redis://localhost:6379/0"))
    app.config.setdefault("CACHE_TYPE", os.environ.get("CACHE_TYPE", "SimpleCache")) 
    app.config.setdefault("WTF_CSRF_ENABLED", True)

    # Initialize extensions
    db.init_app(app)
    migrate.init_app(app, db)
    login.init_app(app)
    # SocketIO init requires app to be passed here for some configurations
    socketio.init_app(app, cors_allowed_origins="*", async_mode='eventlet', message_queue=app.config.get('CELERY_BROKER_URL'))
    cache.init_app(app)
    limiter.init_app(app)

    if _BCRYPT_AVAILABLE and bcrypt:
        bcrypt.init_app(app)

    # Configure Celery
    make_celery(app, celery)

    # Register Blueprints - REMOVED TRY/EXCEPT BLOCKS
    # This ensures if there is an import error, the app fails to start immediately
    # instead of starting successfully but serving 404s.
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
