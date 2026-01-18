import os
from dotenv import load_dotenv

# Load environment variables from a .env file for local development
basedir = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(basedir, '.env'))

class Config:
    """Base configuration."""
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'a-very-secret-key-that-you-should-change'
    
    # Database
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL') or \
        'sqlite:///' + os.path.join(basedir, 'app.db')
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Celery (for background tasks)
    CELERY_BROKER_URL = os.environ.get('REDIS_URL') or 'redis://localhost:6379/0'
    CELERY_RESULT_BACKEND = os.environ.get('REDIS_URL') or 'redis://localhost:6379/0'

    # App-specific settings
    ITEMS_PER_PAGE = 50
    # IMPORTANT: Set this in your Render environment variables to your app's public URL
    # e.g., https://paris-sender-web.onrender.com
    BASE_URL = os.environ.get('BASE_URL') or 'http://localhost:5000'

    # AI Configuration (can be set in .env or Render dashboard)
    OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY')
    LOCAL_AI_URL = os.environ.get('LOCAL_AI_URL') or 'http://localhost:11434/api/generate'
    LOCAL_AI_MODEL = os.environ.get('LOCAL_AI_MODEL') or 'llama3'
    
    # IMAP Configuration (for reply checking)
    IMAP_SERVER = os.environ.get('IMAP_SERVER')
    IMAP_PORT = os.environ.get('IMAP_PORT', 993)
    IMAP_USERNAME = os.environ.get('IMAP_USERNAME')
    IMAP_PASSWORD = os.environ.get('IMAP_PASSWORD')
