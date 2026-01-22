from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect
from flask_socketio import SocketIO
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_caching import Cache
from flask_cors import CORS
from celery import Celery
from config import config
import os
import ssl

# Initialize extensions
db = SQLAlchemy()
migrate = Migrate()
login = LoginManager()
login.login_view = 'main.login'
login.login_message = 'Please log in to access this page.'
csrf = CSRFProtect()

# CRITICAL FIX: Use 'threading' mode instead of 'eventlet'
# This prevents the blocking mainloop errors
socketio = SocketIO(cors_allowed_origins="*", async_mode='threading')

limiter = Limiter(key_func=get_remote_address)
cache = Cache()

# Celery instance
celery = Celery(__name__)


def create_app(config_name=None):
    if config_name is None:
        config_name = os.environ.get('FLASK_CONFIG', 'production')
    
    app = Flask(__name__)
    app.config.from_object(config[config_name])
    
    # Initialize extensions with app
    db.init_app(app)
    migrate.init_app(app, db)
    login.init_app(app)
    csrf.init_app(app)
    
    # CRITICAL FIX: Initialize SocketIO with threading mode
    socketio.init_app(
        app,
        async_mode='threading',
        cors_allowed_origins="*",
        message_queue=app.config.get('SOCKETIO_MESSAGE_QUEUE')
    )
    
    # Initialize rate limiter with Redis if available
    try:
        if app.config.get('RATELIMIT_STORAGE_URL'):
            limiter.init_app(app)
        else:
            app.logger.warning("Rate limiter using in-memory storage")
            limiter.init_app(app)
    except Exception as e: 
        app.logger.warning(f"Rate limiter initialization failed: {e}")
    
    # Initialize cache
    try:
        cache_config = {
            'CACHE_TYPE':  app.config.get('CACHE_TYPE', 'simple'),
            'CACHE_DEFAULT_TIMEOUT': app.config.get('CACHE_DEFAULT_TIMEOUT', 300)
        }
        if app.config.get('CACHE_REDIS_URL'):
            cache_config['CACHE_REDIS_URL'] = app.config.get('CACHE_REDIS_URL')
        cache.init_app(app, config=cache_config)
    except Exception as e: 
        app.logger.warning(f"Cache initialization failed: {e}")
        cache.init_app(app, config={'CACHE_TYPE': 'simple'})
    
    # Initialize CORS
    CORS(app, resources={r"/api/*": {"origins": "*"}})
    
    # Configure Celery
    celery.conf.update(
        broker_url=app.config.get('CELERY_BROKER_URL'),
        result_backend=app.config.get('CELERY_RESULT_BACKEND'),
        task_serializer='json',
        accept_content=['json'],
        result_serializer='json',
        timezone='UTC',
        enable_utc=True,
        broker_connection_retry_on_startup=True
    )
    
    # Add SSL settings for Redis if on Render
    if os.environ.get('RENDER'):
        celery.conf.update(
            broker_use_ssl={'ssl_cert_reqs': ssl.CERT_NONE},
            redis_backend_use_ssl={'ssl_cert_reqs': ssl.CERT_NONE}
        )
    
    celery.conf.update(app.config)
    
    class ContextTask(celery.Task):
        def __call__(self, *args, **kwargs):
            with app.app_context():
                return self.run(*args, **kwargs)
    
    celery.Task = ContextTask
    
    # Register main blueprint (includes error handlers via @bp.app_errorhandler)
    from app.main import bp as main_bp
    app.register_blueprint(main_bp)
    
    # Register API blueprint
    from app.api import bp as api_bp
    app.register_blueprint(api_bp, url_prefix='/api/v1')
    
    # Register tracking blueprint (optional)
    try:
        from app.tracking import bp as tracking_bp
        app.register_blueprint(tracking_bp)
    except ImportError: 
        app.logger.info("Tracking blueprint not found, skipping")
    
    # Register webhooks blueprint (optional)
    try:
        from app.webhooks import bp as webhooks_bp
        app.register_blueprint(webhooks_bp, url_prefix='/webhooks')
    except ImportError: 
        app.logger.info("Webhooks blueprint not found, skipping")
    
    # Create upload folder if it doesn't exist
    upload_folder = app.config.get('UPLOAD_FOLDER')
    if upload_folder and not os.path.exists(upload_folder):
        try:
            os.makedirs(upload_folder)
        except OSError:
            app.logger.warning(f"Could not create upload folder: {upload_folder}")
    
    # Log proxy configuration
    proxy_host = os.environ.get('SMTP_PROXY_HOST')
    proxy_port = os.environ.get('SMTP_PROXY_PORT', '1080')
    proxy_user = os.environ.get('SMTP_PROXY_USER')
    
    if proxy_host:
        auth_status = "Yes" if proxy_user else "No"
        print(f"🔌 SMTP Proxy Configured: {proxy_host}:{proxy_port} (Auth: {auth_status})")
    
    return app
