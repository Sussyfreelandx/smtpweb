import os
import ssl
from datetime import timedelta
from sqlalchemy.pool import NullPool

basedir = os.path.abspath(os.path.dirname(__file__))

class Config:
    # Basic Flask Config
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'dev-secret-key-change-this-in-production'
    
    # Database
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL') or 'sqlite:///' + os.path.join(basedir, 'app.db')
    if SQLALCHEMY_DATABASE_URI and SQLALCHEMY_DATABASE_URI.startswith('postgres://'):
        SQLALCHEMY_DATABASE_URI = SQLALCHEMY_DATABASE_URI.replace('postgres://', 'postgresql://', 1)
    
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    
    # Disable connection pool for compatibility with some environments
    SQLALCHEMY_ENGINE_OPTIONS = {
        'poolclass': NullPool,
        'pool_pre_ping': True
    }
    
    # --- REDIS CONFIGURATION ---
    _redis_url = os.environ.get('REDIS_URL') or 'redis://localhost:6379/0'
    
    # Clean up trailing slashes
    if _redis_url.endswith('/'):
        _redis_url = _redis_url.rstrip('/')
        
    # --- RENDER SSL FIX ---
    # Force rediss:// scheme and define shared SSL options for all Redis clients
    _redis_ssl_options = {}
    if os.environ.get('RENDER'):
        if _redis_url.startswith('redis://'):
            _redis_url = _redis_url.replace('redis://', 'rediss://', 1)
        _redis_ssl_options = {'ssl_cert_reqs': ssl.CERT_NONE}
    # -----------------------

    REDIS_URL = _redis_url
            
    # Celery
    CELERY_BROKER_URL = _redis_url
    CELERY_RESULT_BACKEND = _redis_url
    if os.environ.get('RENDER'):
        CELERY_BROKER_USE_SSL = _redis_ssl_options
        CELERY_REDIS_BACKEND_USE_SSL = _redis_ssl_options
    
    # Redis Cache
    CACHE_TYPE = 'RedisCache'
    CACHE_REDIS_URL = _redis_url
    # --- FIX: Added SSL options for Flask-Caching ---
    CACHE_OPTIONS = { 'redis_connect_options': _redis_ssl_options } if os.environ.get('RENDER') else {}
    CACHE_DEFAULT_TIMEOUT = 300
    
    # Rate Limiting - Use Redis
    RATELIMIT_STORAGE_URL = _redis_url
    # --- FIX: Added SSL options for Flask-Limiter ---
    RATELIMIT_STORAGE_OPTIONS = _redis_ssl_options
    RATELIMIT_DEFAULT = "200 per day"
    RATELIMIT_HEADERS_ENABLED = True
    
    # WebSocket Configuration
    SOCKETIO_MESSAGE_QUEUE = _redis_url
    
    # AI Configuration
    OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY')
    LOCAL_AI_URL = os.environ.get('LOCAL_AI_URL')
    
    # Session Configuration
    SESSION_TYPE = 'filesystem'
    PERMANENT_SESSION_LIFETIME = timedelta(days=7)
    
    # File Upload Configuration
    MAX_CONTENT_LENGTH = 50 * 1024 * 1024
    UPLOAD_FOLDER = os.path.join(basedir, 'app', 'static', 'uploads')
    ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'pdf', 'csv', 'xlsx'}
    
    # Security
    SESSION_COOKIE_SECURE = True
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    
    # Warmup Configuration
    WARMUP_SCHEDULE = [10, 25, 50, 100, 200, 400, 800, 1500]
    
    # API Configuration
    API_RATE_LIMIT = "100 per hour"
    API_KEY_EXPIRY_DAYS = 365
    
    # Email Builder
    EMAIL_TEMPLATES_FOLDER = os.path.join(basedir, 'app', 'static', 'email_templates')
    
    # Monitoring
    SENTRY_DSN = os.environ.get('SENTRY_DSN')
    
    # Feature Flags
    FEATURES = {
        'ai_enabled': True,
        'warmup_enabled': True,
        'api_enabled': True,
        'teams_enabled': True,
        'white_label_enabled': True,
        'sms_enabled': False,
        'webhooks_enabled': True,
    }


class DevelopmentConfig(Config):
    DEBUG = True
    SESSION_COOKIE_SECURE = False
    SERVER_NAME = None
    CACHE_TYPE = 'SimpleCache'
    SOCKETIO_MESSAGE_QUEUE = None # Don't use Redis for SocketIO in dev


class ProductionConfig(Config):
    DEBUG = False
    PREFERRED_URL_SCHEME = 'https'


class TestingConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    CACHE_TYPE = 'SimpleCache'
    SOCKETIO_MESSAGE_QUEUE = None


config = {
    'development': DevelopmentConfig,
    'production': ProductionConfig,
    'testing': TestingConfig,
    'default': ProductionConfig
}
