import os
from dotenv import load_dotenv

# Load environment variables from a .env file for local development
basedir = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(basedir, '.env'))

class Config:
    """Base configuration."""
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'a-very-secret-key-that-you-should-change'
    
    # Database configuration for Render's PostgreSQL, with a fallback to local SQLite
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL') or \
        'sqlite:///' + os.path.join(basedir, 'app.db')
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Celery configuration for Render's Redis, with a fallback to local Redis
    CELERY_BROKER_URL = os.environ.get('REDIS_URL') or 'redis://localhost:6379/0'
    CELERY_RESULT_BACKEND = os.environ.get('REDIS_URL') or 'redis://localhost:6379/0'

    # The public URL of the server for generating tracking links
    SERVER_NAME = os.environ.get('RENDER_EXTERNAL_URL')
    PREFERRED_URL_SCHEME = 'https'
