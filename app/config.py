import os

basedir = os.path.abspath(os.path.dirname(__file__))


class Config:
    """Configuration for the Flask application."""
    
    # Secret key for signing sessions
    SECRET_KEY = os.environ. get('SECRET_KEY') or 'dev-secret-key-please-change-in-production'
    
    # Database configuration
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL') or \
        'sqlite:///' + os.path.join(basedir, 'app.db')
    
    # Fix for PostgreSQL on Render (postgres: // -> postgresql://)
    if SQLALCHEMY_DATABASE_URI and SQLALCHEMY_DATABASE_URI.startswith('postgres://'):
        SQLALCHEMY_DATABASE_URI = SQLALCHEMY_DATABASE_URI.replace('postgres://', 'postgresql://', 1)
    
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    
    # Celery configuration
    CELERY_BROKER_URL = os. environ.get('REDIS_URL') or 'redis://localhost:6379/0'
    CELERY_RESULT_BACKEND = os.environ.get('REDIS_URL') or 'redis://localhost:6379/0'
    
    # AI Configuration (optional)
    OPENAI_API_KEY = os.environ. get('OPENAI_API_KEY')
    LOCAL_AI_URL = os.environ. get('LOCAL_AI_URL')
