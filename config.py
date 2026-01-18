import os
from dotenv import load_dotenv

# Load environment variables from .env file for local development
basedir = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(basedir, '.env'))

class Config:
    """
    Base configuration class. Contains default configuration and settings
    that are loaded from environment variables.
    """
    # Secret key for signing cookies, forms, and other security-related things.
    # Render will generate this for you if you set generateValue: true in render.yaml.
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'you-will-never-guess-this-secret'

    # Database configuration
    # This will be automatically set by Render from the 'fromDatabase' key.
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL') or \
        'sqlite:///' + os.path.join(basedir, 'app.db')
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Celery (background jobs) configuration
    # This will be automatically set by Render from the 'fromService' key.
    CELERY_BROKER_URL = os.environ.get('REDIS_URL')
    CELERY_RESULT_BACKEND = os.environ.get('REDIS_URL')

    # Add other configuration variables here as needed.
