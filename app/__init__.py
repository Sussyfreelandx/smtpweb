import os
import logging
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect
from flask_socketio import SocketIO
from flask_caching import Cache
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from celery import Celery
from config import config
import ssl

# Initialize extensions
db = SQLAlchemy()
migrate = Migrate()
login = LoginManager()
login.login_view = 'main.login'
login.login_message_category = 'info'
csrf = CSRFProtect()
socketio = SocketIO()
cache = Cache()
limiter = Limiter(key_func=get_remote_address)

# Initialize Celery placeholder
celery = Celery(__name__)

def create_app(config_name=None):
    """Application factory pattern."""
    if config_name is None:
        config_name = os.environ.get('FLASK_CONFIG', 'default')
    
    app = Flask(__name__)
    app.config.from_object(config[config_name])
    
    # Render-specific configuration
    if os.environ.get('RENDER'):
        app.config['SERVER_NAME'] = 'paris-sender-web.onrender.com'
        app.config['PREFERRED_URL_SCHEME'] = 'https'

    # Initialize extensions with app
    db.init_app(app)
    migrate.init_app(app, db)
    login.init_app(app)
    csrf.init_app(app)
    cache.init_app(app)
    limiter.init_app(app)
    CORS(app, resources={r"/api/*": {"origins": "*"}})
    
    # Configure Celery
    init_celery(app, celery)
    
    # Configure SocketIO
    async_mode = 'eventlet' 
    redis_url = get_redis_url() # Use helper function
    
    socketio.init_app(
        app,
        message_queue=redis_url,
        cors_allowed_origins="*",
        async_mode=async_mode
    )
    
    # Create necessary folders
    os.makedirs(app.config.get('UPLOAD_FOLDER', 'app/static/uploads'), exist_ok=True)
    os.makedirs(app.config.get('EMAIL_TEMPLATES_FOLDER', 'app/static/email_templates'), exist_ok=True)
    
    # Register Blueprints
    from app.main import bp as main_bp
    app.register_blueprint(main_bp)
    
    from app.api import bp as api_bp
    app.register_blueprint(api_bp, url_prefix='/api/v1')
    
    from app.webhooks import bp as webhooks_bp
    app.register_blueprint(webhooks_bp, url_prefix='/webhooks')
    
    # Setup Logging
    if not app.debug and not app.testing:
        setup_logging(app)
        
    # Setup Sentry
    if app.config.get('SENTRY_DSN'):
        try:
            import sentry_sdk
            from sentry_sdk.integrations.flask import FlaskIntegration
            sentry_sdk.init(
                dsn=app.config['SENTRY_DSN'],
                integrations=[FlaskIntegration()],
                traces_sample_rate=0.1
            )
        except ImportError: 
            pass
            
    return app

def get_redis_url():
    """Helper to get clean Redis URL with SSL logic."""
    redis_url = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')
    if redis_url.startswith('redis://') and os.environ.get('RENDER'):
        # Force SSL for external Render connections
        redis_url = redis_url.replace('redis://', 'rediss://', 1)
    return redis_url

def init_celery(app, celery):
    """Configure the global Celery object with app config."""
    redis_url = get_redis_url()
    
    celery.conf.update(
        broker_url=redis_url,
        result_backend=redis_url,
        broker_connection_retry_on_startup=True,
        task_serializer='json',
        accept_content=['json'],
        result_serializer='json',
        timezone='UTC',
        enable_utc=True
    )
    
    # SSL Configuration for Celery (Render requirement)
    if redis_url.startswith('rediss://'):
        ssl_opts = {'ssl_cert_reqs': ssl.CERT_NONE}
        celery.conf.update(
            broker_use_ssl=ssl_opts,
            redis_backend_use_ssl=ssl_opts
        )

    class ContextTask(celery.Task):
        def __call__(self, *args, **kwargs):
            with app.app_context():
                return self.run(*args, **kwargs)

    celery.Task = ContextTask

def setup_logging(app):
    from logging.handlers import RotatingFileHandler
    if not os.path.exists('logs'):
        os.mkdir('logs')
    file_handler = RotatingFileHandler('logs/paris_sender.log', maxBytes=10240000, backupCount=10)
    file_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]'))
    file_handler.setLevel(logging.INFO)
    app.logger.addHandler(file_handler)
    app.logger.setLevel(logging.INFO)
    app.logger.info('Paris Sender startup')
