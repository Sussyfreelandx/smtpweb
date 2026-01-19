import os
from dotenv import load_dotenv

# Base directory is the root of the project
basedir = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
load_dotenv(os.path.join(basedir, '.env'))

class Config:
    """
    Sets the configuration for the Flask application.
    """
    # Secret key for signing sessions, cookies, and tokens
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'a-super-secret-key-that-you-should-change'

    # Database configuration
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL') or \
        'sqlite:///' + os.path.join(basedir, 'app.db')
    
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Celery configuration
    CELERY_BROKER_URL = os.environ.get('REDIS_URL') or 'redis://localhost:6379/0'
    CELERY_RESULT_BACKEND = os.environ.get('REDIS_URL') or 'redis://localhost:6379/0'

    # AI Configuration
    OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY')
    OPENAI_MODEL = os.environ.get('OPENAI_MODEL', 'gpt-3.5-turbo')
    LOCAL_AI_URL = os.environ.get('LOCAL_AI_URL')
    LOCAL_AI_MODEL = os.environ.get('LOCAL_AI_MODEL', 'llama3')
    
    # File Uploads
    UPLOAD_FOLDER = os.path.join(basedir, 'uploads')
