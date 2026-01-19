import os

basedir = os.path.abspath(os.path.dirname(__file__))


class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'dev-secret-key-change-this-in-production'
    
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL') or 'sqlite:///' + os.path.join(basedir, 'app.db')
    
    # Fix for Heroku/Render postgres URLs
    if SQLALCHEMY_DATABASE_URI and SQLALCHEMY_DATABASE_URI.startswith('postgres://'):
        SQLALCHEMY_DATABASE_URI = SQLALCHEMY_DATABASE_URI.replace('postgres://', 'postgresql://', 1)
    
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    
    CELERY_BROKER_URL = os.environ.get('REDIS_URL') or 'redis://localhost:6379/0'
    CELERY_RESULT_BACKEND = os.environ.get('REDIS_URL') or 'redis://localhost:6379/0'
    
    OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY')
    LOCAL_AI_URL = os.environ.get('LOCAL_AI_URL')

    # --- FIX FOR BACKGROUND TASKS / URL GENERATION ---
    # This tells Flask what domain to use when generating links 
    # inside emails (where there is no active browser request).
    if os.environ.get('RENDER_EXTERNAL_HOSTNAME'):
        SERVER_NAME = os.environ.get('RENDER_EXTERNAL_HOSTNAME')
        PREFERRED_URL_SCHEME = 'https'
