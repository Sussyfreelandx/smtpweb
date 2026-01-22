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
# CELERY BROKER CONFIGURATION - MUST BE FIRST
# ==========================================
_redis_url = os.environ.get('REDIS_URL', '')

# Process Redis URL for SSL
_broker_url = _redis_url
if _broker_url: 
    # Clean trailing slashes
    if _broker_url.endswith('/'):
        _broker_url = _broker_url.rstrip('/')
    
    # Convert to SSL for Render Redis
    if _broker_url.startswith('redis://') and os.environ.get('RENDER'):
        _broker_url = _broker_url.replace('redis://', 'rediss://', 1)

# Debug output
if _broker_url: 
    print(f"🔍 REDIS_URL found: {_redis_url[:40]}...")
    print(f"🔒 Broker URL (SSL): {_broker_url[:40]}...")

# SSL configuration for Redis
_ssl_config = {'ssl_cert_reqs': ssl.CERT_NONE} if _broker_url.startswith('rediss://') else None

# Create Celery with broker URL in constructor (CRITICAL FIX)
if _broker_url: 
    celery = Celery(
        'app',
        broker=_broker_url,
        backend=_broker_url,
        include=['app.tasks']
    )
    
    # Apply all configuration
    celery_config = {
        'broker_url': _broker_url,
        'result_backend': _broker_url,
        'broker_connection_retry_on_startup': True,
        'task_serializer': 'json',
        'accept_content': ['json'],
        'result_serializer': 'json',
        'timezone': 'UTC',
        'enable_utc': True,
        'task_track_started': True,
        'task_time_limit': 3600,
        'worker_prefetch_multiplier': 1,
    }
    
    # Add SSL config if using rediss://
    if _ssl_config:
        celery_config['broker_use_ssl'] = _ssl_config
        celery_config['redis_backend_use_ssl'] = _ssl_config
    
    celery.conf.update(celery_config)
    print(f"✅ Celery configured with Redis broker: {_broker_url[:40]}...")
else:
    print("⚠️ REDIS_URL not set! Celery will not work.")
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
socketio = SocketIO(cors_allowed_origins="*", async_mode='threading', logger=False, engineio_logger=False)
limiter = Limiter(key_func=get_remote_address, default_limits=[])
cache = Cache()


def create_app(config_name=None):
    if config_name is None:
        config_name = os.environ.get('FLASK_CONFIG', 'production')
    
    app = Flask(__name__)
    app.config.from_object(config[config_name])
    
    # Initialize extensions
    db.init_app(app)
    migrate.init_app(app, db)
    login.init_app(app)
    csrf.init_app(app)
    
    # Initialize SocketIO
    socketio.init_app(
        app,
        async_mode='threading',
        cors_allowed_origins="*"
    )
    
    # Initialize rate limiter (with fallback)
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
    
    # Celery context task class
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
    
    # Optional blueprints
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
    
    # Log proxy configuration
    proxy_host = os.environ.get('SMTP_PROXY_HOST')
    if proxy_host: 
        proxy_port = os.environ.get('SMTP_PROXY_PORT', '1080')
        proxy_user = os.environ.get('SMTP_PROXY_USER')
        print(f"🔌 SMTP Proxy Configured: {proxy_host}:{proxy_port} (Auth: {'Yes' if proxy_user else 'No'})")
    
    return app
