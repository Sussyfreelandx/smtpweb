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


def get_redis_url():
    """Get Redis URL with SSL conversion for Render."""
    redis_url = os.environ.get('REDIS_URL', '')
    
    if not redis_url:
        return None
    
    # Clean trailing slashes
    redis_url = redis_url.rstrip('/')
    
    # Always convert to SSL on Render
    if os.environ.get('RENDER') and redis_url.startswith('redis://'):
        redis_url = redis_url.replace('redis://', 'rediss://', 1)
    
    return redis_url


# Get the processed Redis URL
REDIS_URL = get_redis_url()

# Debug output
if REDIS_URL:
    print(f"🔍 REDIS_URL (processed): {REDIS_URL[:50]}...")
else:
    print("⚠️ REDIS_URL not set!")

# Create Celery instance
celery = Celery('app')

if REDIS_URL:
    # SSL settings for Redis
    ssl_opts = {'ssl_cert_reqs': ssl.CERT_NONE} if REDIS_URL.startswith('rediss://') else {}
    
    celery.conf.update(
        broker_url=REDIS_URL,
        result_backend=REDIS_URL,
        broker_use_ssl=ssl_opts if ssl_opts else None,
        redis_backend_use_ssl=ssl_opts if ssl_opts else None,
        broker_connection_retry_on_startup=True,
        task_serializer='json',
        accept_content=['json'],
        result_serializer='json',
        timezone='UTC',
        enable_utc=True,
        task_track_started=True,
        task_acks_late=True,
        worker_prefetch_multiplier=1,
        task_routes={
            'app.tasks.*': {'queue': 'celery'},
        },
    )
    print(f"✅ Celery configured with broker: {REDIS_URL[:50]}...")


# Flask Extensions
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
    
    # Store Redis URL in app config
    app.config['REDIS_URL'] = REDIS_URL
    
    # Initialize extensions
    db.init_app(app)
    migrate.init_app(app, db)
    login.init_app(app)
    csrf.init_app(app)
    
    # Initialize SocketIO with threading mode
    socketio.init_app(
        app,
        async_mode='threading',
        cors_allowed_origins="*",
        logger=False,
        engineio_logger=False
    )
    
    # Initialize rate limiter
    try:
        limiter.init_app(app)
    except Exception as e:
        app.logger.warning(f"Rate limiter init failed: {e}")
    
    # Initialize cache
    try:
        cache.init_app(app, config={
            'CACHE_TYPE':  'SimpleCache',
            'CACHE_DEFAULT_TIMEOUT': 300
        })
    except Exception as e:
        app.logger.warning(f"Cache init failed: {e}")
    
    # Initialize CORS
    CORS(app, resources={r"/api/*": {"origins": "*"}})
    
    # Configure Celery with app context
    class ContextTask(celery.Task):
        def __call__(self, *args, **kwargs):
            with app.app_context():
                return self.run(*args, **kwargs)
    
    celery.Task = ContextTask
    
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
    
    # Log configuration
    proxy_host = os.environ.get('SMTP_PROXY_HOST')
    if proxy_host:
        proxy_port = os.environ.get('SMTP_PROXY_PORT', '1080')
        proxy_user = os.environ.get('SMTP_PROXY_USER')
        print(f"🔌 SMTP Proxy Configured: {proxy_host}:{proxy_port} (Auth: {'Yes' if proxy_user else 'No'})")
    
    return app
