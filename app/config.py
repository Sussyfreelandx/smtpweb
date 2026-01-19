import os
from dotenv import load_dotenv

# The base directory of the entire Flask application.
basedir = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(basedir, '.env'))

class Config:
    """
    Sets the configuration for the Flask application.
    This class reads configuration values from environment variables.
    """
    # Secret key for signing sessions and tokens.
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'a-very-secret-key-change-this-in-prod'

    # Database configuration.
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL') or \
        'sqlite:///' + os.path.join(basedir, 'app.db')
    
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Celery configuration.
    CELERY_BROKER_URL = os.environ.get('REDIS_URL') or 'redis://localhost:6379/0'
    CELERY_RESULT_BACKEND = os.environ.get('REDIS_URL') or 'redis://localhost:6379/0'

    # Optional: Open/Local AI Config
    OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY')
    LOCAL_AI_URL = os.environ.get('LOCAL_AI_URL')
