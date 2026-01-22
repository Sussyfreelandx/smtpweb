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
# Read Redis URL from environment BEFORE creating Celery instance
_redis_url = os.environ.get('REDIS_URL', '')

# Debug print for troubleshooting
if _redis_url:
    print(f"🔍 REDIS_URL found: {_redis_url[:40]}...")
else:
    print("⚠️ REDIS_URL not set, Celery will use default broker")

# Clean and convert to SSL for Render
if _redis_url:
    if _redis_url.endswith('/'):
        _redis_url = _redis_url.rstrip('/')
    if _redis_url.startswith('redis://') and os.environ.get('RENDER'):
        _redis_url = _redis_url.replace('redis://', 'rediss://', 1)
        print(f"🔒 Converted to SSL: {_redis_url[:40]}...")

# Create Celery with Redis broker if available
if _redis_url:
    celery = Celery(
        __name__,
        broker=_redis_url,
        backend=_redis_url,
        include=['app.tasks']
    )
    # Configure SSL for Render Redis
    celery.conf.update(
        broker_use_ssl={'ssl_cert_reqs': ssl.CERT_NONE},
        redis_backend_use_ssl={'ssl_cert_reqs': ssl.CERT_NONE},
        broker_connection_retry_on_startup=True,
    )
    print(f"✅ Celery configured with Redis broker")
else:
    celery = Celery(__name__)
    print("⚠️ Celery using default configuration")

# Configure Celery defaults
celery.conf.update(
    task_serializer='json',
    accept_content=['json'],
    result_serializer='json',
    timezone='UTC',
    enable_utc=True,
)

# ==========================================
# FLASK EXTENSIONS
# ==========================================
db = SQLAlchemy()
migrate = Migrate()
login = LoginManager()
login.login_view = 'main.login'
login.login_message = 'Please log in to access this page.'
csrf = CSRFProtect()
socketio = SocketIO(cors_allowed_origins="*", async_mode='threading')
limiter = Limiter(key_func=get_remote_address)
cache = Cache()


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
    
    socketio.init_app(
        app,
        async_mode='threading',
        cors_allowed_origins="*"
    )
    
    # Initialize rate limiter
    try:
        limiter.init_app(app)
    except Exception as e:
        app.logger.warning(f"Rate limiter initialization failed: {e}")
    
    # Initialize cache
    try:
        cache.init_app(app, config={
            'CACHE_TYPE': 'SimpleCache',
            'CACHE_DEFAULT_TIMEOUT': 300
        })
    except Exception as e:
        app.logger.warning(f"Cache initialization failed: {e}")
    
    CORS(app, resources={r"/api/*": {"origins": "*"}})
    
    # Set up Celery context task
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
    
    # Log proxy configuration
    proxy_host = os.environ.get('SMTP_PROXY_HOST')
    if proxy_host:
        proxy_port = os.environ.get('SMTP_PROXY_PORT', '1080')
        proxy_user = os.environ.get('SMTP_PROXY_USER')
        auth_status = "Yes" if proxy_user else "No"
        print(f"🔌 SMTP Proxy Configured: {proxy_host}:{proxy_port} (Auth: {auth_status})")
    
    return app
