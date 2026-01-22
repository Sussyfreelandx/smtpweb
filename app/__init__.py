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

# ==========================================
# REDIS/CELERY CONFIGURATION - MUST BE FIRST
# ==========================================

def get_redis_url():
    """Get and process Redis URL for both web and worker."""
    redis_url = os.environ.get('REDIS_URL', '')
    
    if not redis_url:
        print("⚠️ WARNING: REDIS_URL environment variable is not set!")
        return None
    
    # Clean trailing slashes
    redis_url = redis_url.rstrip('/')
    
    # Log original URL (masked)
    masked_url = redis_url[:20] + "..." if len(redis_url) > 20 else redis_url
    print(f"🔍 Original REDIS_URL: {masked_url}")
    
    # CRITICAL: Convert to SSL for Render Redis
    # Render's internal Redis requires SSL (rediss://)
    if redis_url.startswith('redis://') and os.environ.get('RENDER'):
        redis_url = redis_url.replace('redis://', 'rediss://', 1)
        print(f"🔒 Converted to SSL: {redis_url[:25]}...")
    
    return redis_url


# Get the processed Redis URL
REDIS_URL = get_redis_url()

# Create Celery instance
if REDIS_URL:
    # SSL options for Redis
    ssl_options = {
        'ssl_cert_reqs': ssl.CERT_NONE
    } if REDIS_URL.startswith('rediss://') else None
    
    # Create Celery with broker set in constructor
    celery = Celery(
        'app',
        broker=REDIS_URL,
        backend=REDIS_URL
    )
    
    # Build configuration
    celery_conf = {
        'broker_url': REDIS_URL,
        'result_backend': REDIS_URL,
        'task_serializer': 'json',
        'accept_content': ['json'],
        'result_serializer': 'json',
        'timezone': 'UTC',
        'enable_utc': True,
        'broker_connection_retry_on_startup': True,
        'task_track_started': True,
        'task_acks_late': True,
        'worker_prefetch_multiplier': 1,
        'task_default_queue': 'celery',
        'task_routes': {
            'app.tasks.*': {'queue': 'celery'}
        },
        'include': ['app.tasks'],
    }
    
    # Add SSL configuration if using rediss://
    if ssl_options:
        celery_conf['broker_use_ssl'] = ssl_options
        celery_conf['redis_backend_use_ssl'] = ssl_options
    
    # Apply configuration
    celery.conf.update(celery_conf)
    
    print(f"✅ Celery configured successfully")
    print(f"   Broker: {REDIS_URL[:30]}...")
    print(f"   SSL: {'Enabled' if ssl_options else 'Disabled'}")
else:
    print("❌ Celery NOT configured - REDIS_URL is missing!")
    celery = Celery('app')


# ==========================================
# FLASK EXTENSIONS
# ==========================================

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
    
    # Initialize SocketIO with threading mode (no eventlet)
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
            'CACHE_TYPE': 'SimpleCache',
            'CACHE_DEFAULT_TIMEOUT': 300
        })
    except Exception as e:
        app.logger.warning(f"Cache init failed: {e}")
    
    # Initialize CORS
    CORS(app, resources={r"/api/*": {"origins": "*"}})
    
    # Configure Celery to use Flask app context
    class ContextTask(celery.Task):
        def __call__(self, *args, **kwargs):
            with app.app_context():
                return self.run(*args, **kwargs)
    
    celery.Task = ContextTask
    
    # Store celery instance on app for easy access
    app.celery = celery
    
    # Register blueprints
    from app.main import bp as main_bp
    app.register_blueprint(main_bp)
    
    from app.api import bp as api_bp
    app.register_blueprint(api_bp, url_prefix='/api/v1')
    
    # Optional blueprints - tracking
    try:
        from app.tracking import bp as tracking_bp
        app.register_blueprint(tracking_bp)
    except ImportError: 
        app.logger.info("Tracking blueprint not available")
    
    # Optional blueprints - webhooks
    try:
        from app.webhooks import bp as webhooks_bp
        app.register_blueprint(webhooks_bp, url_prefix='/webhooks')
    except ImportError: 
        app.logger.info("Webhooks blueprint not available")
    
    # Create upload folder if needed
    upload_folder = app.config.get('UPLOAD_FOLDER')
    if upload_folder and not os.path.exists(upload_folder):
        try:
            os.makedirs(upload_folder)
        except OSError:
            pass
    
    # Log SMTP proxy configuration
    proxy_host = os.environ.get('SMTP_PROXY_HOST')
    if proxy_host:
        proxy_port = os.environ.get('SMTP_PROXY_PORT', '1080')
        proxy_user = os.environ.get('SMTP_PROXY_USER')
        auth_status = "Yes" if proxy_user else "No"
        print(f"🔌 SMTP Proxy Configured: {proxy_host}:{proxy_port} (Auth: {auth_status})")
    
    return app
