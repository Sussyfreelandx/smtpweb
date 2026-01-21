import sys
import os
import socket

# ==========================================
#   CRITICAL:   ENVIRONMENT SETUP
# ==========================================

# 1. Detect Celery
IS_CELERY = 'celery' in sys.argv[0] or (len(sys.argv) > 1 and 'celery' in sys.argv[1])

# 2. Detect if Gunicorn/Eventlet already patched the system
IS_ALREADY_PATCHED = 'eventlet' in str(socket.socket)

# 3. Apply Patching ONLY if needed
if not IS_CELERY and not IS_ALREADY_PATCHED:
    try:
        import eventlet
        eventlet.monkey_patch()
    except ImportError:
        pass

# ==========================================
#   STANDARD IMPORTS
# ==========================================
import logging
import socks
import smtplib
import ssl
from logging.handlers import RotatingFileHandler
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
from config import config

# ==========================================
#   SURGICAL PROXY CONFIGURATION
# ==========================================
PROXY_HOST = os.environ.get('SMTP_PROXY_HOST')
PROXY_PORT = int(os.environ.get('SMTP_PROXY_PORT', 1080))
PROXY_USER = os.environ.get('SMTP_PROXY_USER')
PROXY_PASS = os.environ.get('SMTP_PROXY_PASS')

if PROXY_HOST:  
    if PROXY_USER and PROXY_PASS:
        socks.set_default_proxy(socks.SOCKS5, PROXY_HOST, PROXY_PORT, username=PROXY_USER, password=PROXY_PASS)
    else:
        socks.set_default_proxy(socks.SOCKS5, PROXY_HOST, PROXY_PORT)
    socks.wrap_module(smtplib)

# ==========================================

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

# ==========================================
#   CELERY INSTANCE (LAZY INITIALIZATION)
# ==========================================
celery = None  # Will be initialized only when needed


# ==========================================
#   REDIS URL HELPER
# ==========================================
def get_clean_redis_url():
    """
    Get and clean the Redis URL for Render deployment.
    """
    redis_url = os.environ.get('REDIS_URL', '')
    
    if not redis_url:
        return None, False
    
    # Step 1: Strip whitespace and trailing slashes
    redis_url = redis_url.strip().rstrip('/')
    
    # Step 2: Detect if this is an internal Render Redis URL
    is_internal = (
        redis_url.startswith('redis://red-') and 
        '.render.com' not in redis_url and
        not redis_url.startswith('rediss://')
    )
    
    # Step 3: Determine SSL requirement
    use_ssl = not is_internal and (
        redis_url.startswith('rediss://') or 
        '.render.com' in redis_url
    )
    
    # Step 4: Convert URL scheme if needed for external connections
    if use_ssl and redis_url.startswith('redis://'):
        redis_url = redis_url.replace('redis://', 'rediss://', 1)
    
    return redis_url, use_ssl


def create_app(config_name=None):
    """Application factory pattern."""
    if config_name is None:
        config_name = os.environ.get('FLASK_CONFIG', 'default')
    
    app = Flask(__name__)
    app.config.from_object(config[config_name])
    
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
    
    async_mode = 'eventlet' if not IS_CELERY else 'threading'
    
    # Get clean Redis URL for SocketIO
    redis_url, _ = get_clean_redis_url()
    
    socketio.init_app(
        app,
        message_queue=redis_url,
        cors_allowed_origins="*",
        async_mode=async_mode
    )
    
    os.makedirs(app.config.get('UPLOAD_FOLDER', 'uploads'), exist_ok=True)
    os.makedirs(app.config.get('EMAIL_TEMPLATES_FOLDER', 'templates'), exist_ok=True)
    
    # --- BLUEPRINT REGISTRATION ---
    from app.main import bp as main_bp
    app.register_blueprint(main_bp)
    
    # CRITICAL: Register the Tracking Blueprint so 'tracking.unsubscribe' works
    from app.tracking import bp as tracking_bp
    app.register_blueprint(tracking_bp)
    
    from app.api import bp as api_bp
    app.register_blueprint(api_bp, url_prefix='/api/v1')
    
    from app.webhooks import bp as webhooks_bp
    app.register_blueprint(webhooks_bp, url_prefix='/webhooks')
    
    register_error_handlers(app)
    
    if not app.debug and not app.testing:
        setup_logging(app)
    
    register_cli_commands(app)
    register_context_processors(app)
    
    return app


def register_error_handlers(app):
    from flask import render_template, jsonify, request
    
    @app.errorhandler(400)
    def bad_request_error(error):
        if request.path.startswith('/api/'):
            return jsonify({'error':  'Bad Request', 'message': str(error)}), 400
        return render_template('400.html'), 400
    
    @app.errorhandler(403)
    def forbidden_error(error):
        if request.path.startswith('/api/'):
            return jsonify({'error': 'Forbidden', 'message':  str(error)}), 403
        return render_template('403.html'), 403
    
    @app.errorhandler(404)
    def not_found_error(error):
        if request.path.startswith('/api/'):
            return jsonify({'error': 'Not Found', 'message': str(error)}), 404
        return render_template('404.html'), 404
    
    @app.errorhandler(500)
    def internal_error(error):
        db.session.rollback()
        if request.path.startswith('/api/'):
            return jsonify({'error': 'Internal Server Error', 'message': 'An unexpected error occurred'}), 500
        return render_template('500.html'), 500
    
    @app.errorhandler(429)
    def ratelimit_error(error):
        if request.path.startswith('/api/'):
            return jsonify({'error':  'Too Many Requests', 'message':  'Rate limit exceeded'}), 429
        return render_template('429.html'), 429


def setup_logging(app):
    if not os.path.exists('logs'):
        os.mkdir('logs')
    
    file_handler = RotatingFileHandler('logs/paris_sender.log', maxBytes=10240000, backupCount=10)
    file_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]'))
    file_handler.setLevel(logging.INFO)
    app.logger.addHandler(file_handler)
    app.logger.setLevel(logging.INFO)
    app.logger.info('Paris Sender startup')


def register_cli_commands(app):
    @app.cli.command('init-db')
    def init_db():
        db.create_all()
        print('Database initialized.')
    
    @app.cli.command('create-admin')
    def create_admin():
        from app.models import User
        import click
        username = click.prompt('Admin username')
        email = click.prompt('Admin email')
        password = click.prompt('Admin password', hide_input=True)
        user = User(username=username, email=email)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        print(f'Admin user {username} created.')
    
    @app.cli.command('cleanup-old-data')
    def cleanup_old_data():
        from app.models import Recipient
        from datetime import datetime, timedelta
        cutoff = datetime.utcnow() - timedelta(days=90)
        old_recipients = Recipient.query.filter(Recipient.sent_at < cutoff).count()
        print(f'Found {old_recipients} recipients older than 90 days.')


def register_context_processors(app):
    @app.context_processor
    def inject_globals():
        from datetime import datetime
        return {
            'now': datetime.utcnow(),
            'app_name': 'Paris Sender',
            'app_version': '9.0.0',
            'features': app.config.get('FEATURES', {})
        }


# =========================================================
#   FIXED CELERY CONFIGURATION
# =========================================================
def make_celery(flask_app):
    """Create Celery instance with proper SSL handling for Render."""
    from celery import Celery
    
    # Use the centralized Redis URL helper
    redis_url, use_ssl = get_clean_redis_url()
    
    if not redis_url:
        print("WARNING: No REDIS_URL configured for Celery!")
        redis_url = 'redis://localhost:6379'
        use_ssl = False
    
    celery_app = Celery(
        flask_app.import_name,
        backend=redis_url,
        broker=redis_url
    )
    
    # Build configuration
    celery_config = {
        'broker_url': redis_url,
        'result_backend': redis_url,
        'broker_connection_retry_on_startup': True,
        'broker_transport_options': {
            'visibility_timeout': 3600,
            'socket_timeout': 30,
            'socket_connect_timeout': 30,
            'socket_keepalive': True,
            'health_check_interval': 10,
        },
        'task_serializer': 'json',
        'accept_content': ['json'],
        'result_serializer': 'json',
        'timezone': 'UTC',
        'enable_utc': True,
        'imports': ['app.tasks'] 
    }
    
    if use_ssl:
        celery_config['broker_use_ssl'] = {'ssl_cert_reqs': ssl.CERT_NONE}
        celery_config['redis_backend_use_ssl'] = {'ssl_cert_reqs': ssl.CERT_NONE}
    
    celery_app.conf.update(celery_config)
    
    class ContextTask(celery_app.Task):
        def __call__(self, *args, **kwargs):
            with flask_app.app_context():
                return self.run(*args, **kwargs)
    
    celery_app.Task = ContextTask
    
    # Ensure tasks are loaded
    try:
        import app.tasks
    except Exception as e:
        print(f"ERROR: Failed to import app.tasks: {e}")

    return celery_app


# =========================================================
#   APP INITIALIZATION
# =========================================================
app = create_app()

if IS_CELERY:
    celery = make_celery(app)
