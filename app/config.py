import os

basedir = os.path.abspath(os.path.dirname(__file__))


class Config:
    # Secret Key
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'dev-secret-key-change-this-in-production'
    
    # Database Configuration
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL') or 'sqlite:///' + os.path.join(basedir, 'app.db')
    
    # Fix for Heroku/Render postgres URLs
    if SQLALCHEMY_DATABASE_URI and SQLALCHEMY_DATABASE_URI.startswith('postgres://'):
        SQLALCHEMY_DATABASE_URI = SQLALCHEMY_DATABASE_URI.replace('postgres://', 'postgresql://', 1)
    
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    
    # Celery / Redis
    CELERY_BROKER_URL = os.environ.get('REDIS_URL') or 'redis://localhost:6379/0'
    CELERY_RESULT_BACKEND = os.environ.get('REDIS_URL') or 'redis://localhost:6379/0'
    
    # AI Keys
    OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY')
    LOCAL_AI_URL = os.environ.get('LOCAL_AI_URL')

    # --- CRITICAL FIX FOR EMAIL SENDING ---
    # We hardcode the domain so the background email worker knows 
    # how to generate unsubscribe links without an active browser connection.
    SERVER_NAME = 'paris-sender-web.onrender.com'
    PREFERRED_URL_SCHEME = 'https'
