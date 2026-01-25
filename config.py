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
    
    # Disable connection pool for compatibility with threading
    SQLALCHEMY_ENGINE_OPTIONS = {
        'poolclass': NullPool,
        'pool_pre_ping': True
    }
    
    # --- REDIS CONFIGURATION ---
    REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')
            
    # Celery
    CELERY_BROKER_URL = REDIS_URL
    CELERY_RESULT_BACKEND = REDIS_URL
    
    CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
    CELERY_REDIS_BACKEND_HEALTH_CHECK_INTERVAL = 30
    
    # Render requires SSL for Redis
    if 'RENDER' in os.environ:
        CELERY_TASK_ALWAYS_EAGER = False
        CELERY_BROKER_USE_SSL = {'ssl_cert_reqs': ssl.CERT_NONE}
        CELERY_REDIS_BACKEND_USE_SSL = {'ssl_cert_reqs': ssl.CERT_NONE}
    else:
        # Use eager tasks locally for simpler debugging
        CELERY_TASK_ALWAYS_EAGER = True

    # Redis Cache
    CACHE_TYPE = 'RedisCache'
    CACHE_REDIS_URL = REDIS_URL
    CACHE_DEFAULT_TIMEOUT = 300
    
    # Rate Limiting - Use Redis
    RATELIMIT_STORAGE_URL = REDIS_URL
    RATELIMIT_STORAGE_OPTIONS = {}
    if 'RENDER' in os.environ:
        RATELIMIT_STORAGE_OPTIONS['ssl_cert_reqs'] = ssl.CERT_NONE
    RATELIMIT_DEFAULT = "200 per day"
    RATELIMIT_HEADERS_ENABLED = True
    
    # WebSocket Configuration
    SOCKETIO_MESSAGE_QUEUE = REDIS_URL
    
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


class ProductionConfig(Config):
    DEBUG = False
    PREFERRED_URL_SCHEME = 'https'


class TestingConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    CACHE_TYPE = 'SimpleCache'


# Add 'default' key back to point to ProductionConfig
config = {
    'development': DevelopmentConfig,
    'production': ProductionConfig,
    'testing': TestingConfig,
    'default': ProductionConfig
}
