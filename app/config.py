import os
from datetime import timedelta

basedir = os.path.abspath(os.path.dirname(__file__))


class Config:
    # Basic Flask Config
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'dev-secret-key-change-this-in-production'
    
    # Database
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL') or 'sqlite:///' + os.path.join(basedir, 'app.db')
    if SQLALCHEMY_DATABASE_URI and SQLALCHEMY_DATABASE_URI.startswith('postgres://'):
        SQLALCHEMY_DATABASE_URI = SQLALCHEMY_DATABASE_URI.replace('postgres://', 'postgresql://', 1)
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_POOL_SIZE = 10
    SQLALCHEMY_MAX_OVERFLOW = 20
    
    # Celery
    CELERY_BROKER_URL = os.environ.get('REDIS_URL') or 'redis://localhost:6379/0'
    CELERY_RESULT_BACKEND = os.environ.get('REDIS_URL') or 'redis://localhost:6379/0'
    
    # Redis Cache
    CACHE_TYPE = 'redis'
    CACHE_REDIS_URL = os.environ.get('REDIS_URL') or 'redis://localhost:6379/1'
    CACHE_DEFAULT_TIMEOUT = 300
    
    # AI Configuration
    OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY')
    LOCAL_AI_URL = os.environ.get('LOCAL_AI_URL')
    
    # Session Configuration
    SESSION_TYPE = 'redis'
    PERMANENT_SESSION_LIFETIME = timedelta(days=7)
    
    # File Upload Configuration
    MAX_CONTENT_LENGTH = 50 * 1024 * 1024  # 50MB max
    UPLOAD_FOLDER = os.path.join(basedir, 'app', 'static', 'uploads')
    ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'pdf', 'csv', 'xlsx'}
    
    # Rate Limiting
    RATELIMIT_STORAGE_URL = os.environ.get('REDIS_URL') or 'redis://localhost:6379/2'
    RATELIMIT_DEFAULT = "200 per day"
    RATELIMIT_HEADERS_ENABLED = True
    
    # WebSocket Configuration
    SOCKETIO_MESSAGE_QUEUE = os.environ.get('REDIS_URL') or 'redis://localhost:6379/3'
    
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

    # --- CRITICAL FIX FOR RENDER EMAIL SENDING ---
    # This fixes the "Unable to build URLs" error in background tasks
    SERVER_NAME = 'paris-sender-web.onrender.com'
    PREFERRED_URL_SCHEME = 'https'


class DevelopmentConfig(Config):
    DEBUG = True
    SESSION_COOKIE_SECURE = False
    # Disable SERVER_NAME in local dev to prevent localhost issues
    SERVER_NAME = None


class ProductionConfig(Config):
    DEBUG = False


class TestingConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    SERVER_NAME = None


config = {
    'development': DevelopmentConfig,
    'production': ProductionConfig,
    'testing': TestingConfig,
    'default': DevelopmentConfig
}
