import os
from dotenv import load_dotenv

# The base directory of the entire Flask application.
basedir = os.path.abspath(os.path.dirname(__name__))
load_dotenv(os.path.join(basedir, '.env'))

class Config:
    """
    Sets the configuration for the Flask application.
    This class reads configuration values from environment variables.
    """
    # Secret key for signing sessions and tokens. Render generates this.
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'a-very-secret-key'

    # Database configuration. Render provides DATABASE_URL.
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL') or \
        'sqlite:///' + os.path.join(basedir, 'app.db')
    
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Celery configuration. Render provides REDIS_URL.
    CELERY_BROKER_URL = os.environ.get('REDIS_URL')
    CELERY_RESULT_BACKEND = os.environ.get('REDIS_URL')
