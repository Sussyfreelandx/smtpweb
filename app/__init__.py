import os
import eventlet
# CRITICAL: Monkey patch standard libraries for async compatibility
eventlet.monkey_patch()

from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_socketio import SocketIO
from flask_session import Session
from celery import Celery
import redis
from config import config

# --- GLOBALLY ACCESSIBLE EXTENSIONS ---
db = SQLAlchemy()
migrate = Migrate()
login = LoginManager()
login.login_view = 'main.login'
login.login_message = 'Please log in to access this page.'
login.login_message_category = 'info'
csrf = CSRFProtect()
limiter = Limiter(key_func=get_remote_address)
socketio = SocketIO()
celery = Celery(__name__, broker=os.environ.get('CELERY_BROKER_URL', 'redis://localhost:6379/0'))
sess = Session()

IS_CELERY = 'celery' in os.sys.argv[0]

# --- APPLICATION FACTORY FUNCTION ---
def create_app(config_name=None):
    """Create and configure an instance of the Flask application."""
    if config_name is None:
        config_name = os.environ.get('FLASK_CONFIG', 'default')
        
    app = Flask(__name__)
    app.config.from_object(config[config_name])

    # --- Initialize Extensions ---
    db.init_app(app)
    migrate.init_app(app, db)
    login.init_app(app)
    csrf.init_app(app)
    limiter.init_app(app)
    
    # Configure and initialize Flask-Session
    redis_url = app.config['REDIS_URL']
    app.config['SESSION_REDIS'] = redis.from_url(redis_url)
    sess.init_app(app)
    
    # Initialize SocketIO
    socketio.init_app(app, message_queue=app.config['SOCKETIO_MESSAGE_QUEUE'], async_mode='eventlet')
    
    # Update Celery configuration
    celery.conf.update(app.config)

    # --- Register Blueprints ---
    from app.main import bp as main_bp
    app.register_blueprint(main_blueprint)

    from app.api import bp as api_bp
    app.register_blueprint(api_bp, url_prefix='/api/v1')

    # --- Register Error Handlers ---
    register_error_handlers(app)

    return app


def register_error_handlers(app):
    """Register HTTP error handlers for the application."""
    from flask import render_template

    def render_error(error):
        error_code = getattr(error, 'code', 500)
        return render_template(f'{error_code}.html', title=f"Error {error_code}"), error_code

    for errcode in [400, 403, 404, 429, 500]:
        app.register_error_handler(errcode, render_error)
