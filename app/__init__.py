import os
import ssl
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


def get_celery_broker_url():
    """
    Get and process Redis URL for Celery broker. 
    This function ensures both web and worker use the same URL format.
    """
    redis_url = os.environ.get('REDIS_URL', '')
    
    if not redis_url:
        print("⚠️ WARNING: REDIS_URL is not set!")
        return None
    
    # Remove trailing slashes
    redis_url = redis_url.rstrip('/')
    
    # Log original
    print(f"🔍 REDIS_URL: {redis_url[:30]}...")
    
    # Convert to SSL for Render Redis
    if redis_url.startswith('redis://') and os.environ.get('RENDER'):
        redis_url = redis_url.replace('redis://', 'rediss://', 1)
        print(f"🔒 Converted to SSL: {redis_url[:35]}...")
    
    return redis_url


# Get broker URL
BROKER_URL = get_celery_broker_url()

# Create Celery instance
if BROKER_URL:
    celery = Celery(
        'app',
        broker=BROKER_URL,
        backend=BROKER_URL
    )
    
    # SSL configuration for rediss://
    if BROKER_URL.startswith('rediss://'):
        ssl_options = {'ssl_cert_reqs': ssl.CERT_NONE}
        celery.conf.update(
            broker_use_ssl=ssl_options,
            redis_backend_use_ssl=ssl_options,
        )
    
    # General Celery configuration
    celery.conf.update(
        broker_url=BROKER_URL,
        result_backend=BROKER_URL,
        task_serializer='json',
        accept_content=['json'],
        result_serializer='json',
        timezone='UTC',
        enable_utc=True,
        broker_connection_retry_on_startup=True,
        task_track_started=True,
        task_acks_late=True,
        worker_prefetch_multiplier=1,
        include=['app.tasks'],
    )
    
    print(f"✅ Celery configured with broker: {BROKER_URL[:35]}...")
else:
    celery = Celery('app')
    print("❌ Celery NOT configured - REDIS_URL missing!")


# Flask extensions
db = SQLAlchemy()
migrate = Migrate()
login = LoginManager()
login.login_view = 'main.login'
login.login_message = 'Please log in to access this page.'
login.login_message_category = 'info'
csrf = CSRFProtect()
socketio = SocketIO()
limiter = Limiter(key_func=get_remote_address, default_limits=[])
cache = Cache()


def create_app(config_name=None):
    """Application factory."""
    if config_name is None:
        config_name = os.environ.get('FLASK_CONFIG', 'production')
    
    app = Flask(__name__)
    app.config.from_object(config[config_name])
    
    # Initialize extensions
    db.init_app(app)
    migrate.init_app(app, db)
    login.init_app(app)
    csrf.init_app(app)
    
    # SocketIO with threading mode
    socketio.init_app(
        app,
        async_mode='threading',
        cors_allowed_origins="*",
        logger=False,
        engineio_logger=False
    )
    
    # Rate limiter
    try:
        limiter.init_app(app)
    except Exception as e:
        app.logger.warning(f"Rate limiter init failed: {e}")
    
    # Cache
    try:
        cache.init_app(app, config={
            'CACHE_TYPE': 'SimpleCache',
            'CACHE_DEFAULT_TIMEOUT': 300
        })
    except Exception as e:
        app.logger.warning(f"Cache init failed: {e}")
    
    # CORS
    CORS(app, resources={r"/api/*": {"origins": "*"}})
    
    # Celery context task
    class ContextTask(celery.Task):
        def __call__(self, *args, **kwargs):
            with app.app_context():
                return self.run(*args, **kwargs)
    
    celery.Task = ContextTask
    app.celery = celery
    
    # Register blueprints
    from app.main import bp as main_bp
    app.register_blueprint(main_bp)
    
    from app.api import bp as api_bp
    app.register_blueprint(api_bp, url_prefix='/api/v1')
    
    try:
        from app.tracking import bp as tracking_bp
        app.register_blueprint(tracking_bp)
    except ImportError: 
        pass
    
    try:
        from app.webhooks import bp as webhooks_bp
        app.register_blueprint(webhooks_bp, url_prefix='/webhooks')
    except ImportError: 
        pass
    
    # Create upload folder
    upload_folder = app.config.get('UPLOAD_FOLDER')
    if upload_folder and not os.path.exists(upload_folder):
        try:
            os.makedirs(upload_folder)
        except OSError:
            pass
    
    # Log SMTP proxy config
    proxy_host = os.environ.get('SMTP_PROXY_HOST')
    if proxy_host:
        proxy_port = os.environ.get('SMTP_PROXY_PORT', '1080')
        proxy_user = os.environ.get('SMTP_PROXY_USER')
        print(f"🔌 SMTP Proxy Configured: {proxy_host}:{proxy_port} (Auth: {'Yes' if proxy_user else 'No'})")
    
    return app
