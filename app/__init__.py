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
# CELERY BROKER CONFIGURATION
# ==========================================
_redis_url = os.environ.get('REDIS_URL', '')

if _redis_url:
    print(f"🔍 REDIS_URL found: {_redis_url[:40]}...")
    
    # Clean trailing slashes
    if _redis_url.endswith('/'):
        _redis_url = _redis_url.rstrip('/')
    
    # Convert to SSL for Render Redis
    if _redis_url.startswith('redis://'):
        _redis_url = _redis_url.replace('redis://', 'rediss://', 1)
        print(f"🔒 Converted to SSL:  {_redis_url[:40]}...")
    
    # SSL configuration for Redis
    ssl_config = {'ssl_cert_reqs': ssl.CERT_NONE}
    
    # Create Celery with explicit broker and backend URLs
    celery = Celery(__name__)
    celery.conf.update(
        broker_url=_redis_url,
        result_backend=_redis_url,
        broker_use_ssl=ssl_config,
        redis_backend_use_ssl=ssl_config,
        broker_transport_options={'ssl_cert_reqs': ssl.CERT_NONE},
        broker_connection_retry_on_startup=True,
        task_serializer='json',
        accept_content=['json'],
        result_serializer='json',
        timezone='UTC',
        enable_utc=True,
        include=['app.tasks'],
    )
    print(f"✅ Celery configured with Redis broker: {_redis_url[:40]}...")
else:
    print("⚠️ REDIS_URL not set!")
    celery = Celery(__name__)

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
    
    # Initialize extensions
    db.init_app(app)
    migrate.init_app(app, db)
    login.init_app(app)
    csrf.init_app(app)
    
    socketio.init_app(
        app,
        async_mode='threading',
        cors_allowed_origins="*"
    )
    
    try:
        limiter.init_app(app)
    except Exception as e: 
        app.logger.warning(f"Rate limiter init failed: {e}")
    
    try:
        cache.init_app(app, config={'CACHE_TYPE': 'SimpleCache', 'CACHE_DEFAULT_TIMEOUT': 300})
    except Exception as e:
        app.logger.warning(f"Cache init failed: {e}")
    
    CORS(app, resources={r"/api/*": {"origins": "*"}})
    
    # Celery context
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
    
    # Log proxy config
    proxy_host = os.environ.get('SMTP_PROXY_HOST')
    if proxy_host:
        proxy_port = os.environ.get('SMTP_PROXY_PORT', '1080')
        proxy_user = os.environ.get('SMTP_PROXY_USER')
        print(f"🔌 SMTP Proxy Configured: {proxy_host}:{proxy_port} (Auth: {'Yes' if proxy_user else 'No'})")
    
    return app
