import os

# The base directory of the entire Flask application.
basedir = os.path.abspath(os.path.dirname(__file__))

class Config:
    """
    Sets the configuration for the Flask application.
    This class reads configuration values from environment variables.
    """
    # Secret key for signing sessions and tokens. Render will generate this.
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'you-will-never-guess-this-secret'

    # Database configuration. Render provides DATABASE_URL.
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL') or \
        'sqlite:///' + os.path.join(basedir, 'app.db')
    
    # This is a SQLAlchemy setting that is often disabled to save resources.
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Celery configuration. Render provides REDIS_URL.
    CELERY_BROKER_URL = os.environ.get('REDIS_URL')
    CELERY_RESULT_BACKEND = os.environ.get('REDIS_URL')
